from __future__ import annotations

import ctypes
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
WORKSPACE = ROOT / ".transcriber_work"
UPLOADS = WORKSPACE / "uploads"
JOBS = WORKSPACE / "jobs"
OUTPUTS = ROOT / "outputs"
WORKER = ROOT / "transcribe_local_whisper.py"
HOST = "127.0.0.1"
PORT = 8765
MIN_AVAILABLE_RAM_GB = 3.0
SUPPORTED_EXTENSIONS = {
    ".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma",
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg",
}
CHUNK_SIZE = 1024 * 1024

for folder in (UPLOADS, JOBS, OUTPUTS):
    folder.mkdir(parents=True, exist_ok=True)

LOCK = threading.RLock()
STATE = {
    "status": "ready",
    "stage": "Ready for your media",
    "progress": 0,
    "input_name": "",
    "input_size": 0,
    "output_name": "",
    "output_path": "",
    "output_size": 0,
    "duration": None,
    "cue_count": None,
    "language": "auto",
    "max_words": "3",
    "elapsed": 0.0,
    "started_at": None,
    "logs": ["Ready when you are."],
    "error": "",
    "job_id": "",
    "cancel_requested": False,
    "active_input": "",
    "process": None,
}



def available_ram_gb() -> float | None:
    """Return available physical RAM in GiB on Windows without dependencies."""
    if os.name == "nt":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        state = MEMORYSTATUSEX()
        state.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
                return state.ullAvailPhys / (1024 ** 3)
        except Exception:
            return None
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 ** 3)
    except Exception:
        return None


def ffmpeg_executable() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [
        Path(r"C:\ffmpeg-9.0.2\bin\ffmpeg.exe"),
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def add_log(message: str) -> None:
    message = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", message).strip()
    if not message:
        return
    # Avoid flooding the UI with TQDM's animated progress rows.
    if "Loading weights:" in message and len(message) > 160:
        message = "Loading model weights…"
    if len(message) > 240:
        message = message[:237] + "…"
    with LOCK:
        STATE["logs"].append(message)
        STATE["logs"] = STATE["logs"][-9:]


def snapshot() -> dict:
    with LOCK:
        payload = {k: v for k, v in STATE.items() if k not in {"process", "active_input", "cancel_requested", "started_at"}}
        if STATE["started_at"] is not None and STATE["status"] == "running":
            payload["elapsed"] = max(0.0, time.monotonic() - STATE["started_at"])
        else:
            payload["elapsed"] = float(STATE.get("elapsed", 0.0) or 0.0)
        return payload


def cleanup_path(path: Path | None) -> None:
    if not path:
        return
    try:
        if path.exists() and path.is_file():
            path.unlink()
    except OSError:
        pass


def set_stage(stage: str, progress: int | None = None) -> None:
    with LOCK:
        STATE["stage"] = stage
        if progress is not None:
            STATE["progress"] = max(0, min(100, progress))


def is_cancelled() -> bool:
    with LOCK:
        return bool(STATE["cancel_requested"])


def job_thread(job_id: str, input_path: Path, original_name: str, caption_mode: str) -> None:
    job_dir = JOBS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    normalized_audio = job_dir / "media_16k_mono.wav"
    worker_srt: Path | None = None
    ffmpeg_proc: subprocess.Popen | None = None
    worker_proc: subprocess.Popen | None = None
    try:
        if not WORKER.is_file():
            raise RuntimeError(f"Worker script not found: {WORKER}")
        model_dir = Path(r"D:\DAVYAN\models\faster-whisper-small")
        if not model_dir.is_dir():
            raise RuntimeError(f"Model folder not found: {model_dir}")
        missing_model_files = [name for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt") if not (model_dir / name).is_file()]
        if missing_model_files:
            raise RuntimeError("The faster-whisper-small folder is missing: " + ", ".join(missing_model_files))

        executable = ffmpeg_executable()
        if executable:
            set_stage("Preparing audio", 8)
            add_log("Preparing mono 16 kHz audio locally…")
            cmd = [executable, "-y", "-i", str(input_path), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(normalized_audio)]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            ffmpeg_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", creationflags=flags)
            with LOCK:
                STATE["process"] = ffmpeg_proc
            _, stderr_text = ffmpeg_proc.communicate()
            with LOCK:
                STATE["process"] = None
            if is_cancelled():
                raise InterruptedError("Cancelled by user.")
            if ffmpeg_proc.returncode != 0 or not normalized_audio.is_file():
                detail = (stderr_text or "FFmpeg did not produce an audio file.").strip()[-1800:]
                raise RuntimeError("Could not extract audio from this media file. " + detail)
            audio_for_worker = normalized_audio
        elif input_path.suffix.lower() in {".wav", ".flac"}:
            audio_for_worker = input_path
            add_log("FFmpeg not detected; using the WAV/FLAC file directly.")
        else:
            raise RuntimeError("FFmpeg is required for this file type. Install FFmpeg or add ffmpeg.exe to PATH.")

        if is_cancelled():
            raise InterruptedError("Cancelled by user.")

        set_stage("Loading model", 12)
        add_log("Starting the local Python worker. The browser UI stays responsive.")
        input_lines = f'{str(audio_for_worker)}\n{caption_mode}\n'
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["TOKENIZERS_PARALLELISM"] = "false"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        worker_proc = subprocess.Popen(
            [sys.executable, "-u", str(WORKER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            cwd=str(ROOT),
            creationflags=flags,
        )
        with LOCK:
            STATE["process"] = worker_proc
        assert worker_proc.stdin is not None
        worker_proc.stdin.write(input_lines)
        worker_proc.stdin.close()
        assert worker_proc.stdout is not None

        for raw_line in worker_proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            add_log(line)
            if line.startswith("PROGRESS|"):
                parts = line.split("|", 2)
                if len(parts) == 3:
                    try:
                        value = int(parts[1])
                    except ValueError:
                        value = None
                    set_stage(parts[2], value)
            elif "Audio duration:" in line:
                match = re.search(r"Audio duration:\s*([0-9.]+)\s*seconds", line)
                if match:
                    with LOCK:
                        STATE["duration"] = float(match.group(1))
                set_stage("Preparing audio", 8)
            elif "Loading faster-whisper-small" in line:
                set_stage("Loading model", 12)
            elif "Model loaded" in line or "Transcribing with word timestamps" in line:
                set_stage("Transcribing audio", 32)
            elif "Detected language:" in line:
                add_log(line)
            elif "Grouping captions" in line:
                set_stage("Formatting captions", 93)
            elif "SRT saved to:" in line:
                match = re.search(r"SRT saved to:\s*(.+)$", line)
                if match:
                    worker_srt = Path(match.group(1).strip().strip('"'))
            elif "Created " in line and "timed subtitle cues" in line:
                match = re.search(r"Created\s+(\d+)\s+timed subtitle cues", line)
                if match:
                    with LOCK:
                        STATE["cue_count"] = int(match.group(1))
            elif "ERROR while transcribing" in line or line.startswith("ERROR:"):
                set_stage("Error", 0)

        return_code = worker_proc.wait()
        with LOCK:
            STATE["process"] = None
        if is_cancelled():
            raise InterruptedError("Cancelled by user.")
        if return_code != 0:
            tail = "\n".join(snapshot().get("logs", [])[-4:])
            raise RuntimeError(tail or f"Transcription worker exited with code {return_code}.")
        if not worker_srt or not worker_srt.is_file():
            # The worker normally prints the SRT path; also check the converted file's neighbour.
            possible = audio_for_worker.with_suffix(".srt")
            if possible.is_file():
                worker_srt = possible
            else:
                raise RuntimeError("The worker finished but no SRT file was found.")

        set_stage("Finishing captions", 92)
        output_stem = Path(original_name).stem
        output_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", output_stem).strip(" .") or "transcript"
        output_path = OUTPUTS / f"{output_stem}.srt"
        shutil.copy2(worker_srt, output_path)
        count = STATE.get("cue_count")
        try:
            out_size = output_path.stat().st_size
        except OSError:
            out_size = 0
        with LOCK:
            if STATE["started_at"] is not None:
                STATE["elapsed"] = max(0.0, time.monotonic() - STATE["started_at"])
            STATE.update({
                "status": "completed",
                "stage": "Captions ready",
                "progress": 100,
                "output_name": output_path.name,
                "output_path": str(output_path),
                "output_size": out_size,
                "cue_count": count,
                "error": "",
            })
        add_log(f"Saved SRT to {output_path}")
    except InterruptedError:
        with LOCK:
            if STATE["started_at"] is not None:
                STATE["elapsed"] = max(0.0, time.monotonic() - STATE["started_at"])
            STATE["status"] = "cancelled"
            STATE["stage"] = "Cancelled"
            STATE["progress"] = 0
            STATE["error"] = ""
            STATE["process"] = None
        add_log("Job cancelled. Temporary audio will be removed.")
    except Exception as exc:
        with LOCK:
            if STATE["started_at"] is not None:
                STATE["elapsed"] = max(0.0, time.monotonic() - STATE["started_at"])
            STATE["status"] = "error"
            STATE["stage"] = "Needs attention"
            STATE["progress"] = 0
            STATE["error"] = f"{type(exc).__name__}: {exc}"
            STATE["process"] = None
        add_log(f"ERROR: {type(exc).__name__}: {exc}")
    finally:
        with LOCK:
            STATE["process"] = None
            active = Path(STATE["active_input"]) if STATE["active_input"] else None
            STATE["active_input"] = ""
        cleanup_path(active)
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except OSError:
            pass
        # Avoid leaving an uploaded copy if the user cancels or an exception occurs.
        cleanup_path(input_path)


class Handler(BaseHTTPRequestHandler):
    server_version = "TranscribeLocal/1.0"

    def log_message(self, fmt: str, *args) -> None:
        # Keep the console quiet; real app events are shown in the UI.
        return

    def send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/api/status":
            self.send_json(snapshot())
            return
        if path == "/api/download":
            state = snapshot()
            output_path = Path(state["output_path"]) if state.get("output_path") else None
            if state.get("status") != "completed" or not output_path or not output_path.is_file():
                self.send_json({"error": "No completed SRT is available yet."}, 404)
                return
            data_len = output_path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/x-subrip; charset=utf-8")
            self.send_header("Content-Length", str(data_len))
            self.send_header("Content-Disposition", f"attachment; filename=\"captions.srt\"; filename*=UTF-8''{quote(output_path.name)}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with output_path.open("rb") as f:
                shutil.copyfileobj(f, self.wfile)
            return
        if path in {"/", "/index.html"}:
            file_path = STATIC / "index.html"
        elif path in {"/assets/app.css", "/assets/app.js"}:
            file_path = STATIC / path.rsplit("/", 1)[-1]
        else:
            self.send_error(404, "Not found")
            return
        if not file_path.is_file():
            self.send_error(404, "UI asset not found")
            return
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        if file_path.suffix.lower() in {".html", ".css", ".js"}:
            content_type = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}[file_path.suffix.lower()] + "; charset=utf-8"
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/api/upload":
            self.handle_upload(parsed.query)
            return
        if parsed.path == "/api/transcribe":
            self.handle_transcribe()
            return
        if parsed.path == "/api/cancel":
            self.handle_cancel()
            return
        self.send_json({"error": "Not found"}, 404)

    def handle_upload(self, query: str) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            self.send_json({"error": "The selected file is empty."}, 400)
            return
        params = parse_qs(query)
        raw_name = params.get("filename", ["media"])[0]
        # Treat the browser-provided filename as metadata, never as a filesystem path.
        name = Path(raw_name.replace("\\", "/")).name
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            self.send_json({"error": f"Unsupported file type: {suffix or '(no extension)'}"}, 415)
            return
        with LOCK:
            if STATE["status"] == "running":
                self.send_json({"error": "A transcription is already running."}, 409)
                return
            previous = Path(STATE["active_input"]) if STATE["active_input"] else None
        cleanup_path(previous)
        upload_path = UPLOADS / f"{uuid.uuid4().hex}{suffix}"
        received = 0
        try:
            with upload_path.open("wb") as f:
                while received < length:
                    block = self.rfile.read(min(CHUNK_SIZE, length - received))
                    if not block:
                        raise ConnectionError("Upload ended before the full file was received.")
                    f.write(block)
                    received += len(block)
            with LOCK:
                STATE.update({
                    "status": "ready",
                    "stage": "Media ready",
                    "progress": 0,
                    "input_name": name,
                    "input_size": received,
                    "output_name": "",
                    "output_path": "",
                    "output_size": 0,
                    "duration": None,
                    "cue_count": None,
                    "error": "",
                    "logs": [f"Media ready: {name}"],
                    "active_input": str(upload_path),
                    "cancel_requested": False,
                    "job_id": "",
                    "started_at": None,
                })
            self.send_json({"ok": True, "name": name, "size": received})
        except Exception as exc:
            cleanup_path(upload_path)
            self.send_json({"error": f"Upload failed: {type(exc).__name__}: {exc}"}, 500)

    def handle_transcribe(self) -> None:
        try:
            body_len = int(self.headers.get("Content-Length", "0") or 0)
            payload = json.loads(self.rfile.read(body_len).decode("utf-8"))
        except Exception:
            self.send_json({"error": "Could not read transcription settings."}, 400)
            return
        caption_mode = str(payload.get("caption_mode", payload.get("max_words", 3))).lower()
        if caption_mode not in {"2", "3", "4", "sentence"}:
            self.send_json({"error": "Choose 2, 3, 4 words, or a full sentence."}, 400)
            return
        free_ram = available_ram_gb()
        if free_ram is not None and free_ram < MIN_AVAILABLE_RAM_GB:
            self.send_json({
                "error": "Not enough free memory to start safely. Close a few apps and try again."
            }, 409)
            return
        if free_ram is None:
            self.send_json({
                "error": "Could not check available memory. Restart the app and try again."
            }, 503)
            return
        with LOCK:
            if STATE["status"] == "running":
                self.send_json({"error": "A transcription is already running."}, 409)
                return
            active = Path(STATE["active_input"]) if STATE["active_input"] else None
            name = STATE["input_name"]
            if not active or not active.is_file():
                self.send_json({"error": "Choose or upload a media file first."}, 400)
                return
            job_id = uuid.uuid4().hex[:10]
            STATE.update({
                "status": "running",
                "stage": "Preparing audio",
                "progress": 5,
                "language": "auto",
                "max_words": caption_mode,
                "elapsed": 0,
                "started_at": time.monotonic(),
                "logs": ["Job started."],
                "error": "",
                "job_id": job_id,
                "cancel_requested": False,
                "output_name": "",
                "output_path": "",
                "cue_count": None,
                "duration": None,
            })
        thread = threading.Thread(target=job_thread, args=(job_id, active, name, caption_mode), daemon=True)
        thread.start()
        self.send_json({"ok": True, "job_id": job_id}, 202)

    def handle_cancel(self) -> None:
        with LOCK:
            if STATE["status"] != "running":
                self.send_json({"error": "There is no active job to cancel."}, 409)
                return
            STATE["cancel_requested"] = True
            proc = STATE.get("process")
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        add_log("Cancel requested…")
        self.send_json({"ok": True})


def main() -> int:
    if not WORKER.is_file():
        print(f"Missing worker script: {WORKER}")
        print("Place transcribe_local_whisper.py beside run_transcriber_ui.bat.")
        return 1
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        try:
            server = ThreadingHTTPServer((HOST, PORT + 1), Handler)
        except OSError as exc:
            print(f"Could not open local web server: {exc}")
            return 1
    server.daemon_threads = True
    url = f"http://{HOST}:{server.server_port}"
    print("TRANSCRIBE local UI")
    print(f"Open: {url}")
    print("Only this PC can connect. Close this window to stop the app.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping TRANSCRIBE…")
    finally:
        with LOCK:
            proc = STATE.get("process")
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
