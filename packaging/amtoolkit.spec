# PyInstaller build for the single-file Windows executable.
#   pip install -r requirements-build.txt
#   pyinstaller --clean --noconfirm packaging/amtoolkit.spec
# Output: dist/AccessManagerToolkit.exe (dist/AccessManagerToolkit on Linux/macOS)
import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "amtoolkit" / "__init__.py").read_text()).group(1)

version_info = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)
    nums = tuple(int(n) for n in re.findall(r"\d+", VERSION)[:3]) + (0,)
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("FileDescription", "Access Manager Toolkit"),
                StringStruct("ProductName", "Access Manager Toolkit"),
                StringStruct("FileVersion", VERSION),
                StringStruct("ProductVersion", VERSION),
                StringStruct("OriginalFilename", "AccessManagerToolkit.exe"),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "amtoolkit" / "static"), "amtoolkit/static")],
    hiddenimports=["waitress"],
    excludes=["tkinter", "unittest", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="AccessManagerToolkit",
    console=True,  # the console window shows the URL and lets users stop the toolkit with Ctrl+C
    upx=False,     # UPX-compressed executables are flagged by antivirus far more often
    version=version_info,
)
