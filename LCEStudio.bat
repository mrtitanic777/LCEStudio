@echo off
rem LCE Studio -- desktop save editor
pushd "%~dp0"
start "" pythonw "%~dp0LCEStudio.py" %*
if errorlevel 1 python "%~dp0LCEStudio.py" %*
popd
