# -*- mode: python ; coding: utf-8 -*-
# Build:  pyinstaller packaging/techrag.spec --noconfirm   -> dist/SayisalTasarimUzmani/SayisalTasarimUzmani.exe
#         set TECHRAG_ONEFILE=1 && pyinstaller packaging/techrag.spec  -> dist/SayisalTasarimUzmani-portable.exe
# File names are ASCII (safe in paths, zip archives and GitHub release assets); Windows shows the product name
# "Sayısal Tasarım Uzmanı" from the version resource (Task Manager, file properties).
import os
import re

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

NAME = "SayisalTasarimUzmani"
TITLE = "Sayısal Tasarım Uzmanı"
with open(os.path.join(ROOT, "techrag", "__init__.py"), encoding="utf-8") as fh:
    VERSION = re.search(r'__version__ = "([^"]+)"', fh.read()).group(1)
nums = tuple(int(x) for x in re.findall(r"\d+", VERSION)[:4]) + (0,) * 4
VERSION_FILE = os.path.join(ROOT, "build", "version_info.txt")
os.makedirs(os.path.dirname(VERSION_FILE), exist_ok=True)
with open(VERSION_FILE, "w", encoding="utf-8-sig") as fh:  # BOM: PyInstaller honours it
    fh.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums[:4]}, prodvers={nums[:4]}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041f04b0', [
      StringStruct('FileDescription', '{TITLE}'),
      StringStruct('ProductName', '{TITLE}'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('ProductVersion', '{VERSION}'),
      StringStruct('InternalName', '{NAME}'),
      StringStruct('OriginalFilename', '{NAME}.exe')])]),
    VarFileInfo([VarStruct('Translation', [0x041f, 1200])])
  ]
)
""")

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=f"{NAME}-portable", console=CONSOLE,
              upx=False, runtime_tmpdir=None, version=VERSION_FILE)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, console=CONSOLE, upx=False, version=VERSION_FILE)
    coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)
