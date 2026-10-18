# Transcribe — Minimal local captions

1. Close the currently running app and its console window.
2. Extract the ZIP into `D:\PR\Transcriber`, overwriting the old files.
3. Run `run_transcriber_ui.bat`.

Choose a media file, select **2**, **3**, **4**, or **Full sentence**, then press **Transcribe**. The resulting `.srt` is saved beside the original media in `outputs` and can be downloaded from the app.

The app uses the existing model at `D:\DAVYAN\models\faster-whisper-small` with Faster-Whisper on CPU in INT8 mode. It does not download a model. Audio/video conversion uses FFmpeg if installed. The startup memory check stays in the background and blocks a job if less than 3 GB is available; this is a precaution, not a guarantee against all memory pressure.

The selected original is never modified. Temporary files are cleaned up when the job ends or is cancelled.
