# -*- mode: python ; coding: utf-8 -*-
import glob
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = [('lce/lzxc.exe', 'lce'), ('lce/view3d/textures/terrain.png', 'lce/view3d/textures')]
binaries = []
hiddenimports = []
hiddenimports += collect_submodules('lce')
tmp_ret = collect_all('pyglet')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# vendored converter engine: its 3 DLLs + runtime templates (loaded by explicit
# path via _res.base(), which resolves to <_MEIPASS>/lce/converter when frozen)
for _dll in ('xcompress64.dll', 'chm_lzx.dll', 'LZXDecompression.dll'):
    datas.append(('lce/converter/%s' % _dll, 'lce/converter'))
for _tpl in glob.glob('lce/converter/templates/*'):
    datas.append((_tpl, 'lce/converter/templates'))


a = Analysis(
    ['LCEStudio.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LCEStudio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
