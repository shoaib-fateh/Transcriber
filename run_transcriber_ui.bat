@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PYTHON%" (
  for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYTHON_FOUND set "PYTHON_FOUND=%%P"
  if defined PYTHON_FOUND set "PYTHON=%PYTHON_FOUND%"
)

if not exist "%PYTHON%" (
  echo Python was not found. Edit this file and set PYTHON to your python.exe path.
  pause
  exit /b 1
)

if not exist "%~dp0transcribe_local_whisper.py" (
  echo Missing transcribe_local_whisper.py
  echo Extract the complete package into D:\PR\Transcriber.
  pause
  exit /b 1
)

"%PYTHON%" -c "import faster_whisper" >nul 2>&1
if errorlevel 1 (
  echo Installing Faster-Whisper once. An internet connection is required for this first setup...
  "%PYTHON%" -m pip install faster-whisper
  if errorlevel 1 (
    echo.
    echo Installation failed. Check the internet connection and try again.
    pause
    exit /b 1
  )
)

"%PYTHON%" "%~dp0webapp\app.py"
if errorlevel 1 pause
