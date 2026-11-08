# -*- mode: python ; coding: utf-8 -*-

import sys
sys.path.insert(0, SPECPATH)
from build_support import node_runtime_artifacts, ssl_runtime_data
node_binaries, node_data = node_runtime_artifacts()

a = Analysis(
    ['main.py'],
    pathex=['src'],
    binaries=node_binaries,
    datas=node_data + ssl_runtime_data(),
    hiddenimports=[],
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
    name='buidl-batch',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
