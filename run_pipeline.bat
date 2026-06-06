@echo off
:: safe_jarvis 파트 A — dual-agent 파이프라인 런처
:: python 이 PATH 에 없어도 Windows py 런처(py.exe)로 실행한다.
::
:: 사용 예:
::   run_pipeline.bat --task "로그인 버그 고쳐줘" --project "C:\projects\my-project"
::   run_pipeline.bat --task "..." --project <경로> --dry-run
::   run_pipeline.bat --only claude_plan --project <경로>

setlocal

:: py 런처 사용 시도 → 없으면 python 으로 폴백
where py >nul 2>&1
if %errorlevel% == 0 (
    set PYTHON=py -3.11
) else (
    set PYTHON=python
)

set SCRIPT=%~dp0actions\dual_agent_pipeline.py

%PYTHON% "%SCRIPT%" %*
endlocal
