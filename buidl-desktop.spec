# -*- mode: python ; coding: utf-8 -*-
import sys
from PyInstaller.utils.hooks import collect_all
sys.path.insert(0, SPECPATH)
from build_support import ssl_runtime_data
data, binaries, hidden = collect_all('webview')
data += ssl_runtime_data() + [('desktop_ui', 'desktop_ui')]
a = Analysis(['desktop.py'], pathex=[SPECPATH], binaries=binaries,
    datas=data, hiddenimports=hidden + ['webview.platforms.edgechromium', 'av'],
    excludes=['tkinter', 'customtkinter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='BUIDL-2.0.0-Teste', console=False,
    debug=False, strip=False, upx=False, icon='desktop_ui/buidl.ico')
