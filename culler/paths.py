import sys
from pathlib import Path
from typing import Optional, Union

APP_NAME = "QuickCull"


def get_project_root() -> Path:
    """Returns the root directory of the application/project."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = get_project_root()

# Project library paths (always resolved relative to project root only)
LIB_DIR = PROJECT_ROOT / "lib"
MODELS_DIR = LIB_DIR / "models"
EXIFTOOL_DIR = LIB_DIR / "exif-tools"

# Default workspace and dataset configuration
DEFAULT_WORKSPACE_NAME = "default.fpc-workspace"
DEFAULT_WORKSPACE_PATH = PROJECT_ROOT / DEFAULT_WORKSPACE_NAME
DEFAULT_DATASET_DIR = PROJECT_ROOT / "_DATASET"

# Aliases for backward compatibility
WORKSPACE_PATH = DEFAULT_WORKSPACE_PATH
DB_PATH = DEFAULT_WORKSPACE_PATH
DATASET_DIR = DEFAULT_DATASET_DIR
APP_DATA_DIR = PROJECT_ROOT


def get_lib_dir() -> Path:
    """Returns the library directory (always in project path)."""
    return LIB_DIR


def get_models_dir() -> Path:
    """Returns the models directory (always in project path)."""
    return MODELS_DIR


def get_exiftool_dir() -> Path:
    """Returns the exiftool directory (always in project path)."""
    return EXIFTOOL_DIR


def get_default_workspace_path() -> Path:
    """Returns the default workspace file path."""
    return DEFAULT_WORKSPACE_PATH


def get_dataset_dir_for_workspace(workspace_path: Optional[Union[str, Path]] = None) -> Path:
    """
    Returns the _DATASET directory associated with the specified workspace file.
    If a custom workspace path is provided, looks for _DATASET in that workspace's directory.
    Otherwise, returns the project default _DATASET directory.
    """
    if workspace_path:
        w_path = Path(workspace_path)
        if w_path.suffix in (".fpc-workspace", ".db"):
            return w_path.parent / "_DATASET"
        elif not w_path.suffix:
            return w_path / "_DATASET"
        else:
            return w_path.parent / "_DATASET"
    return DEFAULT_DATASET_DIR


def resolve_workspace_path(workspace_path: Optional[Union[str, Path]] = None) -> Path:
    """
    Resolves the workspace database path.
    If none provided, returns DEFAULT_WORKSPACE_PATH (migrating legacy culler.db if found).
    """
    if workspace_path:
        p = Path(workspace_path).resolve()
        if p.is_dir():
            return p / DEFAULT_WORKSPACE_NAME
        return p

    # Default workspace: check if legacy culler.db exists and default.fpc-workspace doesn't
    if not DEFAULT_WORKSPACE_PATH.exists():
        legacy_db = PROJECT_ROOT / "culler.db"
        if legacy_db.exists():
            try:
                legacy_db.rename(DEFAULT_WORKSPACE_PATH)
            except Exception:
                pass

    return DEFAULT_WORKSPACE_PATH
