@echo off
setlocal
pushd "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m codex_session_repair %*
) else (
    python -m codex_session_repair %*
)
set "repair_exit=%errorlevel%"
popd
exit /b %repair_exit%
