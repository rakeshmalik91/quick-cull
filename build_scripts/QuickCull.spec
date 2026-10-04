# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Quick Cull.
Bundles GUI app with CustomTkinter assets, YOLO models, ExifTool, and the icon.

Usage:
    pyinstaller QuickCull.spec
"""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

python_dlls = [
    (Path(sys.exec_prefix) / "python3.dll", "."),
    (Path(sys.exec_prefix) / "python312.dll", "."),
]

# --- Data files to bundle ---

# CustomTkinter themes and assets
ctk_datas = collect_data_files("customtkinter")

# Application data: YOLO models + ExifTool + app icon
app_datas = [
    ("../lib/models", "lib/models"),
    ("../lib/exif-tools", "lib/exif-tools"),
    ("../media/quick_cull.ico", "media"),
    ("../media/quick_cull_icon.png", "media"),
    ("../media/quick_cull_splash_icon.png", "media"),
]

all_datas = ctk_datas + app_datas

# --- Hidden imports ---
# Modules that PyInstaller cannot auto-detect from dynamic imports
hidden_imports = [
    "bootstrap",
    "customtkinter",
    "PIL._tkinter_finder",
    "PIL.Image",
    "PIL.ImageTk",
    "pillow_heif",
    "rawpy",
    "cv2",
    "numpy",
    "rich",
    "rich.console",
    "rich.table",
    "ultralytics",
    "mediapipe",
] + collect_submodules("culler")

# --- Analysis ---
# gui.pyw is the launcher: it paints the splash before importing the app.
a = Analysis(
    ["../gui.pyw"],
    pathex=[".."],
    binaries=python_dlls,
    datas=all_datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "scipy",
        "pandas",
        "notebook",
        "jupyter",
        "IPython",
        "pytest",
        "hypothesis",
        "sphinx",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="QuickCull",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # Windowed GUI mode (no terminal)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    icon="../media/quick_cull.ico",
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="QuickCull",
)