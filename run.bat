@echo off
setlocal
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
    echo Creating venv...
    python -m venv .venv || exit /b 1
    .venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1
)
rem Upgrade ViZDoom's bundled OpenAL (1.21) to ours (1.24+): newer builds
rem automatically follow Windows default-device changes, so sound effects
rem move to your headset along with everything else.
copy /y third_party\OpenAL32.dll .venv\Lib\site-packages\vizdoom\OpenAL32.dll >nul 2>&1
.venv\Scripts\python.exe -m mspaintdoom.main %*
