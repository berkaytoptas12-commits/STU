# -*- mode: python ; coding: utf-8 -*-
# Build:  pyinstaller packaging/techrag.spec --noconfirm              -> dist/TechRAG/TechRAG.exe (one folder)
#         set TECHRAG_ONEFILE=1 && pyinstaller packaging/techrag.spec  -> dist/TechRAG-portable.exe (single file)
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ONEFILE = os.environ.get("TECHRAG_ONEFILE") == "1"
CONSOLE = os.environ.get("TECHRAG_CONSOLE") == "1"   # debug builds

datas = [
    (os.path.join(ROOT, "techrag", "web"), os.path.join("techrag", "web")),
    (os.path.join(ROOT, "techrag", "resources"), os.path.join("techrag", "resources")),
]
hiddenimports = (
    collect_submodules("techrag")
    + collect_submodules("uvicorn")
    + ["multipart", "python_multipart", "anyio._backends._asyncio"]
)
for pkg in ("webview", "truststore"):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

a = Analysis(
    [os.path.join(ROOT, "packaging", "entry.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "torch", "transformers", "sentence_transformers", "IPython", "pandas",
              "cryptography", "PIL", "scipy", "sklearn", "playwright", "pytest",
              "PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="TechRAG-portable", console=CONSOLE,
              upx=False, runtime_tmpdir=None)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TechRAG", console=CONSOLE, upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="TechRAG", upx=False)
