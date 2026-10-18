(() => {
  const $ = (selector) => document.querySelector(selector);
  const fileInput = $('#fileInput');
  const dropZone = $('#dropZone');
  const selectedFilePanel = $('#selectedFile');
  const fileName = $('#fileName');
  const fileMeta = $('#fileMeta');
  const uploadStatus = $('#uploadStatus');
  const uploadLabel = $('#uploadLabel');
  const uploadPercent = $('#uploadPercent');
  const uploadBar = $('#uploadBar');
  const startButton = $('#startButton');
  const runPanel = $('#runPanel');
  const runTitle = $('#runTitle');
  const stageText = $('#stageText');
  const progressBar = $('#progressBar');
  const elapsedText = $('#elapsed');
  const resultPanel = $('#resultPanel');
  const errorPanel = $('#errorPanel');
  const cancelButton = $('#cancelButton');
  const againButton = $('#againButton');
  const toast = $('#toast');

  let uploadComplete = false;
  let uploadBusy = false;
  let jobRunning = false;
  let pollingTimer = null;
  let toastTimer = null;
  let lastStatus = null;

  function fmtBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return '0 MB';
    if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(bytes >= 100 * 1024 * 1024 ? 0 : 1)} MB`;
  }

  function fmtTime(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }

  function showToast(message, duration = 4000) {
    toast.textContent = message;
    toast.classList.add('visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('visible'), duration);
  }

  function setUploadProgress(percent, label = 'Uploading') {
    uploadStatus.classList.remove('hidden');
    uploadLabel.textContent = label;
    uploadPercent.textContent = `${Math.max(0, Math.min(100, Math.round(percent)))}%`;
    uploadBar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
  }

  function setFileDisplay(file, uploaded = false) {
    dropZone.classList.add('hidden');
    selectedFilePanel.classList.remove('hidden');
    fileName.textContent = file.name;
    fileMeta.textContent = `${uploaded ? 'Ready' : 'Selected'} · ${fmtBytes(file.size)}`;
    startButton.disabled = !uploaded || uploadBusy || jobRunning;
  }

  function resetFileDisplay() {
    uploadComplete = false;
    uploadBusy = false;
    fileInput.value = '';
    selectedFilePanel.classList.add('hidden');
    dropZone.classList.remove('hidden');
    uploadStatus.classList.add('hidden');
    uploadBar.style.width = '0%';
    startButton.disabled = true;
    startButton.textContent = 'Transcribe';
  }

  function setControlsLocked(locked) {
    fileInput.disabled = locked;
    $('#browseButton').disabled = locked;
    $('#removeFile').disabled = locked;
    document.querySelectorAll('.caption-option').forEach(button => { button.disabled = locked; });
    dropZone.setAttribute('aria-disabled', String(locked));
    dropZone.style.pointerEvents = locked ? 'none' : '';
  }

  function chooseFile(file) {
    if (!file || jobRunning || uploadBusy) return;
    const allowed = /\.(wav|flac|mp3|m4a|aac|ogg|opus|wma|mp4|mov|mkv|webm|avi|m4v|mpeg|mpg)$/i.test(file.name);
    if (!allowed) {
      showToast('Choose an audio or video file.');
      return;
    }
    uploadComplete = false;
    uploadBusy = true;
    setFileDisplay(file, false);
    setControlsLocked(true);
    startButton.disabled = true;
    setUploadProgress(0, 'Copying file locally');

    const request = new XMLHttpRequest();
    request.open('POST', `/api/upload?filename=${encodeURIComponent(file.name)}`);
    request.setRequestHeader('Content-Type', 'application/octet-stream');
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) setUploadProgress(event.loaded / event.total * 100);
    };
    request.onerror = () => {
      uploadBusy = false;
      setControlsLocked(false);
      resetFileDisplay();
      showToast('The file could not be copied. Try again.');
    };
    request.onload = () => {
      uploadBusy = false;
      setControlsLocked(false);
      let response;
      try { response = JSON.parse(request.responseText); } catch { response = {}; }
      if (request.status < 200 || request.status >= 300 || !response.ok) {
        resetFileDisplay();
        showToast(response.error || 'File upload failed.');
        return;
      }
      uploadComplete = true;
      setFileDisplay(file, true);
      setUploadProgress(100, 'Ready');
      setTimeout(() => uploadStatus.classList.add('hidden'), 650);
    };
    request.send(file);
  }

  dropZone.addEventListener('click', (event) => {
    if (jobRunning || uploadBusy || event.target.closest('button')) return;
    fileInput.click();
  });
  dropZone.addEventListener('keydown', (event) => {
    if ((event.key === 'Enter' || event.key === ' ') && !jobRunning && !uploadBusy) {
      event.preventDefault();
      fileInput.click();
    }
  });
  $('#browseButton').addEventListener('click', (event) => {
    event.stopPropagation();
    if (!jobRunning && !uploadBusy) fileInput.click();
  });
  fileInput.addEventListener('change', () => chooseFile(fileInput.files?.[0]));
  $('#removeFile').addEventListener('click', () => {
    if (jobRunning || uploadBusy) return;
    resetFileDisplay();
  });

  ['dragenter', 'dragover'].forEach(type => dropZone.addEventListener(type, event => {
    event.preventDefault();
    if (!jobRunning && !uploadBusy) dropZone.classList.add('dragging');
  }));
  ['dragleave', 'dragend', 'drop'].forEach(type => dropZone.addEventListener(type, event => {
    event.preventDefault();
    dropZone.classList.remove('dragging');
  }));
  dropZone.addEventListener('drop', event => {
    if (jobRunning || uploadBusy) return;
    const files = event.dataTransfer?.files;
    if (files?.length) chooseFile(files[0]);
  });
  window.addEventListener('dragover', event => event.preventDefault());
  window.addEventListener('drop', event => event.preventDefault());

  document.querySelectorAll('.caption-option').forEach(button => {
    button.addEventListener('click', () => {
      if (jobRunning) return;
      document.querySelectorAll('.caption-option').forEach(item => {
        const active = item === button;
        item.classList.toggle('active', active);
        item.setAttribute('aria-pressed', String(active));
      });
    });
  });

  function selectedCaptionMode() {
    return document.querySelector('.caption-option.active')?.dataset.mode || '2';
  }

  async function startTranscription() {
    if (!uploadComplete || jobRunning || uploadBusy) return;
    try {
      const response = await fetch('/api/transcribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ caption_mode: selectedCaptionMode() })
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || 'Could not start transcription.');
      jobRunning = true;
      setControlsLocked(true);
      startButton.disabled = true;
      startButton.textContent = 'Working…';
      runPanel.classList.remove('hidden');
      runPanel.classList.add('running');
      runTitle.textContent = 'Transcribing';
      resultPanel.classList.add('hidden');
      errorPanel.classList.add('hidden');
      cancelButton.classList.remove('hidden');
      againButton.classList.add('hidden');
      progressBar.style.width = '4%';
      runPanel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      beginPolling();
    } catch (error) {
      showToast(error.message || 'Could not start transcription.');
    }
  }

  function beginPolling() {
    if (pollingTimer) clearInterval(pollingTimer);
    refreshStatus();
    pollingTimer = setInterval(refreshStatus, 700);
  }

  async function refreshStatus() {
    try {
      const response = await fetch('/api/status', { cache: 'no-store' });
      const status = await response.json();
      lastStatus = status;
      renderStatus(status);
      if (status.status !== 'running' && pollingTimer) {
        clearInterval(pollingTimer);
        pollingTimer = null;
      }
    } catch {
      // Transient local polling failures should not interrupt the job.
    }
  }

  function renderStatus(status) {
    const running = status.status === 'running';
    if (['running', 'completed', 'error', 'cancelled'].includes(status.status)) {
      runPanel.classList.remove('hidden');
      runPanel.classList.toggle('running', running);
      runPanel.classList.toggle('indeterminate', running && /loading model/i.test(status.stage || ''));
      const progressElement = runPanel.querySelector('[role="progressbar"]');
      if (progressElement) progressElement.setAttribute('aria-valuenow', String(Math.max(0, Math.min(100, Number(status.progress) || 0))));
    }

    if (running) {
      jobRunning = true;
      setControlsLocked(true);
      progressBar.style.width = `${Math.max(4, Math.min(99, Number(status.progress) || 4))}%`;
      stageText.textContent = ({'Preparing audio':'Preparing', 'Loading model':'Loading model', 'Transcribing audio':'Transcribing', 'Formatting captions':'Finishing', 'Formatting captions':'Finishing', 'Finishing SRT':'Finishing', 'Finishing captions':'Finishing', 'Loading faster-whisper-small in CPU INT8 mode…':'Loading model'})[status.stage] || status.stage || 'Working';
      elapsedText.textContent = fmtTime(status.elapsed);
      runTitle.textContent = status.stage === 'Formatting captions' ? 'Finishing captions' : 'Transcribing';
      cancelButton.classList.remove('hidden');
      againButton.classList.add('hidden');
      resultPanel.classList.add('hidden');
      errorPanel.classList.add('hidden');
      return;
    }

    if (status.status === 'completed') {
      jobRunning = false;
      setControlsLocked(false);
      progressBar.style.width = '100%';
      stageText.textContent = 'Complete';
      elapsedText.textContent = fmtTime(status.elapsed);
      runTitle.textContent = 'SRT ready';
      $('#resultMeta').textContent = `${status.cue_count ?? '—'} captions · ${fmtTime(status.duration || 0)}`;
      resultPanel.classList.remove('hidden');
      errorPanel.classList.add('hidden');
      cancelButton.classList.add('hidden');
      againButton.classList.remove('hidden');
      startButton.textContent = 'Transcribe';
      return;
    }

    if (status.status === 'error' || status.status === 'cancelled') {
      jobRunning = false;
      setControlsLocked(false);
      runPanel.classList.remove('running', 'indeterminate');
      progressBar.style.width = '0%';
      stageText.textContent = status.status === 'cancelled' ? 'Cancelled' : 'Failed';
      elapsedText.textContent = fmtTime(status.elapsed);
      runTitle.textContent = status.status === 'cancelled' ? 'Cancelled' : 'Could not transcribe';
      errorPanel.textContent = status.error || (status.status === 'cancelled' ? 'Transcription cancelled.' : 'Unknown error.');
      errorPanel.classList.remove('hidden');
      resultPanel.classList.add('hidden');
      cancelButton.classList.add('hidden');
      againButton.classList.remove('hidden');
      startButton.textContent = 'Transcribe';
    }
  }

  startButton.addEventListener('click', startTranscription);
  cancelButton.addEventListener('click', async () => {
    if (!jobRunning) return;
    cancelButton.disabled = true;
    cancelButton.textContent = 'Stopping…';
    try { await fetch('/api/cancel', { method: 'POST' }); } catch { /* status polling reports final state */ }
    setTimeout(() => { cancelButton.disabled = false; cancelButton.textContent = 'Cancel'; }, 900);
  });
  againButton.addEventListener('click', () => {
    if (pollingTimer) clearInterval(pollingTimer);
    pollingTimer = null;
    runPanel.classList.add('hidden');
    runPanel.classList.remove('running', 'indeterminate');
    setControlsLocked(false);
    resetFileDisplay();
    resultPanel.classList.add('hidden');
    errorPanel.classList.add('hidden');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });

  refreshStatus().then(() => {
    if (lastStatus?.status === 'running') {
      jobRunning = true;
      setControlsLocked(true);
      startButton.disabled = true;
      startButton.textContent = 'Working…';
      runPanel.classList.remove('hidden');
      runPanel.classList.add('running', 'indeterminate');
      beginPolling();
    }
  });
})();
