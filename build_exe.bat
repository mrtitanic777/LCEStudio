@echo off
rem Rebuild the standalone LCEStudio.exe (onefile, windowed).
pushd "%~dp0"
python -m PyInstaller --noconfirm --clean --name LCEStudio --onefile --windowed ^
  --add-data "lce/lzxc.exe;lce" ^
  --add-data "lce/view3d/textures/terrain.png;lce/view3d/textures" ^
  --collect-submodules lce ^
  --collect-all pyglet ^
  --paths . LCEStudio.py
copy /Y dist\LCEStudio.exe LCEStudio.exe
echo Done. LCEStudio.exe rebuilt.
popd
