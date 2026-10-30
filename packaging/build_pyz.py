#!/usr/bin/env python3
"""Builds dist/AccessManagerToolkit.pyz: one file containing the toolkit and all its dependencies.

    python packaging/build_pyz.py

Run it with:  python AccessManagerToolkit.pyz   (needs Python 3.11+, nothing else)

Compiled speed-up modules (MarkupSafe, charset-normalizer) are removed so the same file runs on
Windows, macOS and Linux; both libraries fall back to their pure-Python implementations.
"""
import shutil
import subprocess
import sys
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "pyz"
OUTPUT = ROOT / "dist" / "AccessManagerToolkit.pyz"

MAIN = '''import sys

if sys.version_info < (3, 11):
    sys.exit("Access Manager Toolkit needs Python 3.11 or newer (found %d.%d)." % sys.version_info[:2])

from amtoolkit.cli import main

sys.exit(main())
'''


def main():
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-compile", "--disable-pip-version-check",
                    "--target", str(BUILD), "-r", str(ROOT / "requirements.txt")], check=True)
    for compiled in [*BUILD.rglob("*.so"), *BUILD.rglob("*.pyd")]:
        compiled.unlink()
    shutil.rmtree(BUILD / "bin", ignore_errors=True)
    shutil.copytree(ROOT / "amtoolkit", BUILD / "amtoolkit",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (BUILD / "__main__.py").write_text(MAIN, "utf-8")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    zipapp.create_archive(BUILD, OUTPUT, interpreter="/usr/bin/env python3", compressed=True)
    print(f"Built {OUTPUT} ({OUTPUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
