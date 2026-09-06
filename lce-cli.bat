@echo off
pushd "%~dp0"
python -m lce %*
popd
