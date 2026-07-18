@echo off
setlocal
set MSPAINTDOOM_DEBUG=1
cd /d %~dp0
call run.bat %*
