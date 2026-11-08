# -*- mode: python ; coding: utf-8 -*-

import os
import shutil
import sys
import zipfile
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


# Some portable/uv Python distributions keep Tcl/Tk under the interpreter
# directory. Stage the runtime inside the workspace before Analysis so
# PyInstaller's tkinter hook can inspect and bundle it reliably.
python_tcl = Path(sys.base_prefix) / "tcl"
staged_tk = Path("build") / "tk-runtime"
if (python_tcl / "tcl8.6").is_dir() and (python_tcl / "tk8.6").is_dir():
    shutil.copytree(python_tcl / "tcl8.6", staged_tk / "tcl8.6", dirs_exist_ok=True)
    shutil.copytree(python_tcl / "tk8.6", staged_tk / "tk8.6", dirs_exist_ok=True)
    os.environ["TCL_LIBRARY"] = str((staged_tk / "tcl8.6").resolve())
    os.environ["TK_LIBRARY"] = str((staged_tk / "tk8.6").resolve())
else:
    # Python 3.14 for Windows ships Tcl/Tk 9 as compressed standard-library
    # archives. Extract them only into the disposable build directory so both
    # tkinter and PyInstaller's hook can discover the runtime normally.
    tcl_archives = sorted(python_tcl.glob("libtcl*.zip"))
    tk_archives = sorted(python_tcl.glob("libtk*.zip"))
    if tcl_archives and tk_archives:
        staged_tk.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(tcl_archives[-1]) as archive:
            archive.extractall(staged_tk)
        with zipfile.ZipFile(tk_archives[-1]) as archive:
            archive.extractall(staged_tk)
        os.environ["TCL_LIBRARY"] = str((staged_tk / "tcl_library").resolve())
        os.environ["TK_LIBRARY"] = str((staged_tk / "tk_library").resolve())

customtkinter_data = collect_data_files("customtkinter")
sys.path.insert(0, SPECPATH)
from build_support import node_runtime_artifacts, ssl_runtime_data
node_binaries, node_data = node_runtime_artifacts()
customtkinter_data.extend(node_data)
customtkinter_data.extend(ssl_runtime_data())

a = Analysis(
    ["gui.py"],
    pathex=["."],
    binaries=node_binaries,
    datas=customtkinter_data,
    hiddenimports=["customtkinter"],
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
    name="buidl-gui",
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
