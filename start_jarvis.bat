@echo off
:: safe_jarvis — Jarvis 음성 비서 시작 스크립트 (파트 B)
:: requirements.txt 먼저 설치 필요: py -3.11 -m pip install -r requirements.txt

setlocal

where py >nul 2>&1
if %errorlevel% == 0 (
    set PYTHON=py -3.11
) else (
    set PYTHON=python
)

set SCRIPT=%~dp0main.py

echo [safe_jarvis] Starting JARVIS...
%PYTHON% "%SCRIPT%" %*
endlocal
