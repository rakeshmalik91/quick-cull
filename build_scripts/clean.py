"""
Clean script for Quick Cull build artifacts.

Removes PyInstaller output and cache directories.
"""

import shutil
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent


def clean():
    dirs_to_remove = [
        ROOT / "build",
        ROOT / "dist",
        ROOT / "build_onefile",
        ROOT / "dist_onefile",
    ]

    removed = []
    for d in dirs_to_remove:
        if d.exists():
            shutil.rmtree(d)
            removed.append(d)

    pycache = ROOT / "__pycache__"
    if pycache.exists():
        shutil.rmtree(pycache)
        removed.append(pycache)

    if removed:
        print("Removed:")
        for d in removed:
            print(f"  - {d}")
    else:
        print("Nothing to clean.")


if __name__ == "__main__":
    clean()
