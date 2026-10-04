"""
Build script for the Quick Cull Windows executable.

Usage (from project root):
    python build_scripts/build_exe.py           # Normal build (folder mode)
    python build_scripts/build_exe.py --onefile  # Single-file .exe (slower startup)
    python build_scripts/build_exe.py --console  # With console window for debugging
"""

import subprocess
import sys
import shutil
from pathlib import Path


APP_NAME = "QuickCull"
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
SPEC_FILE = SCRIPT_DIR / f"{APP_NAME}.spec"
DIST_DIR = ROOT / "dist" / APP_NAME


def check_prerequisites():
    """Verify required tools and assets exist."""
    errors = []

    # PyInstaller
    try:
        import PyInstaller
        print(f"  [OK] PyInstaller {PyInstaller.__version__}")
    except ImportError:
        errors.append("PyInstaller is not installed. Run: pip install pyinstaller")

    # Required source files
    for f in ["gui.py", "gui.pyw", "bootstrap.py", "culler.py"]:
        if not (ROOT / f).exists():
            errors.append(f"Missing source file: {f}")

    # App icon (window icon, splash art and the exe icon all come from it)
    for asset in ["quick_cull.ico", "quick_cull_icon.png", "quick_cull_splash_icon.png"]:
        if not (ROOT / "media" / asset).exists():
            errors.append(
                f"Missing icon asset: media/{asset} (run: python build_scripts/make_icon.py)"
            )

    # Models directory
    models_dir = ROOT / "lib" / "models"
    if not models_dir.exists():
        errors.append(f"Missing models directory: {models_dir}")
    else:
        model_files = list(models_dir.glob("*.pt"))
        if model_files:
            print(f"  [OK] Found {len(model_files)} YOLO model(s) in lib/models/")
        else:
            errors.append("No .pt model files found in lib/models/")

    # ExifTool
    exiftool = ROOT / "lib" / "exif-tools" / "exiftool.exe"
    if exiftool.exists():
        print(f"  [OK] ExifTool found at lib/exif-tools/exiftool.exe")
    else:
        print(f"  [WARN] ExifTool not found at lib/exif-tools/exiftool.exe (EXIF sync will use system exiftool)")

    if errors:
        print("\n  ERRORS:")
        for e in errors:
            print(f"    - {e}")
        return False

    return True


def build(onefile: bool = False, console: bool = False):
    """Run PyInstaller build."""
    print(f"\n=== {APP_NAME} - Windows Executable Build ===\n")

    print("[1/3] Checking prerequisites...")
    if not check_prerequisites():
        print("\nBuild aborted due to prerequisite errors.")
        sys.exit(1)

    print("\n[2/3] Building executable...")

    original_spec = SPEC_FILE.read_text(encoding="utf-8")
    modified_spec = original_spec

    if console:
        modified_spec = modified_spec.replace(
            "console=False,  # Windowed GUI mode (no terminal)",
            "console=True,  # Windowed GUI mode (no terminal)"
        )

    if onefile:
        modified_spec = modified_spec.replace(
            f"coll = COLLECT(\n    exe,\n    a.binaries,\n    a.zipfiles,\n    a.datas,\n    strip=False,\n    upx=False,\n    upx_exclude=[],\n    name=\"{APP_NAME}\",\n)",
            ""
        )
        modified_spec = modified_spec.replace(
            "exe = EXE(\n    pyz,\n    a.scripts,\n    [],\n    exclude_binaries=True,",
            "exe = EXE(\n    pyz,\n    a.scripts,\n    a.binaries,\n    a.zipfiles,\n    a.datas,\n    exclude_binaries=False,"
        )

    SPEC_FILE.write_text(modified_spec, encoding="utf-8")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        str(SPEC_FILE),
    ]

    print(f"  Running: {' '.join(cmd)}\n")
    result = subprocess.run(cmd, cwd=str(ROOT))

    SPEC_FILE.write_text(original_spec, encoding="utf-8")

    if onefile:
        build_exe = ROOT / "build" / APP_NAME / f"{APP_NAME}.exe"
        dist_exe = ROOT / "dist" / f"{APP_NAME}.exe"
        if build_exe.exists():
            shutil.copy2(build_exe, dist_exe)

    if result.returncode != 0:
        print(f"\n  BUILD FAILED (exit code {result.returncode})")
        sys.exit(result.returncode)

    print("\n[3/3] Verifying output...")
    if onefile:
        exe_path = ROOT / "dist" / f"{APP_NAME}.exe"
    else:
        exe_path = DIST_DIR / f"{APP_NAME}.exe"

    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"  [OK] Executable created: {exe_path}")
        print(f"  [OK] Size: {size_mb:.1f} MB")

        if not onefile:
            total_size = sum(f.stat().st_size for f in DIST_DIR.rglob("*") if f.is_file())
            total_mb = total_size / (1024 * 1024)
            print(f"  [OK] Total dist folder: {total_mb:.1f} MB")
    else:
        print(f"  FAILED: {exe_path} not found!")
        sys.exit(1)

    print(f"\n=== Build Complete ===")
    print(f"Output: {exe_path}")
    print(f"Run:    {exe_path}")


if __name__ == "__main__":
    onefile = "--onefile" in sys.argv
    console = "--console" in sys.argv
    build(onefile=onefile, console=console)
