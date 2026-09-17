#!/usr/bin/env python
"""Reproducible PyInstaller build for ATC Pauser.

Run:  python build.py
Output: dist/ATC Pauser/ATC Pauser.exe  (a --onedir folder build)

Why --onedir and not --onefile:
    A --onefile exe unpacks itself into %TEMP% and runs from there at every
    launch. That runtime self-extraction is the behavior generic/ML antivirus
    heuristics flag hardest on PyInstaller apps (and this app also calls
    NtSuspendProcess, which reads as "hacktool" to those models). --onedir
    extracts nothing at runtime, which typically drops the false-positive
    count sharply. The trade-off: it ships as a folder - the exe must stay
    next to its _internal folder - so we distribute the whole folder in the zip.

Not enabled here (each needs tooling this project does not assume):
    - Authenticode code signing (the real fix) needs a paid certificate.
    - Rebuilding the bootloader from source needs a C/C++ compiler.
    UPX is intentionally OFF; packing raises detections, not lowers them.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "ATC Pauser"


def main() -> int:
    # Clean previous output so stale files never ship.
    for d in ("build", "dist"):
        shutil.rmtree(ROOT / d, ignore_errors=True)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",              # no console window
        "--onedir",                # folder build: no %TEMP% self-extraction
        "--noupx",                 # never pack; packing raises AV detections
        "--name", NAME,
        "--icon", str(ROOT / "icon.ico"),
        "--version-file", str(ROOT / "version_info.txt"),
        "--collect-all", "SimConnect",
        str(ROOT / "atc_pauser.py"),
    ]
    print(">>", " ".join(args))
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    exe = ROOT / "dist" / NAME / f"{NAME}.exe"
    print("\nBuilt:" if exe.exists() else "\nBUILD FAILED, missing:", exe)
    return 0 if exe.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
