r"""Windowed launcher for Quick Cull.

Double-clickable entry point: shows the splash immediately, loads the
application in the background and then runs the GUI with no console window
attached. Argument handling matches ``gui.py``/``culler.py``:

    gui.pyw                        -> blank launch, restores open tabs
    gui.pyw "D:\Photos\2024"       -> opens a folder
    gui.pyw "D:\Photos\DSC1.ARW"   -> opens the folder and selects the image
    gui.pyw "D:\Photos\ws.fpc-workspace" -> opens a specific workspace

Because a console is not available when this is launched through pythonw,
failures are written to ``culler_debug.log`` and surfaced in a message box.
"""

import os
import sys
import warnings
import traceback
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
LOG_FILE = APP_DIR / "culler_debug.log"

# pythonw gives us no usable stdio. Point the streams at devnull so third-party
# progress bars and print() calls degrade quietly instead of raising.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

sys.path.insert(0, str(APP_DIR))
os.chdir(APP_DIR)

# Suppress known upstream third-party FutureWarning (e.g. Keras/TF np.object warning)
warnings.filterwarnings("ignore", category=FutureWarning, module="keras.*")
warnings.filterwarnings("ignore", message=".*np\\.object.*", category=FutureWarning)


def log(message: str) -> None:
    """Append a line to the app debug log, ignoring any IO failure."""
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"[launcher] {message}\n")
    except OSError:
        pass


def show_error(title: str, message: str) -> None:
    """Show a fatal error to the user, since there is no console to print to."""
    log(f"{title}: {message}")
    try:
        import tkinter as tk
        from tkinter import messagebox as mb

        root = tk.Tk()
        root.withdraw()
        mb.showerror("Quick Cull", message)
        root.destroy()
    except Exception:
        pass


def parse_args(argv):
    """Split argv into (initial_path, workspace_path)."""
    initial_path = None
    workspace_path = None
    for arg in argv:
        text = str(arg)
        if text.startswith("-"):
            continue
        if text.lower().endswith((".fpc-workspace", ".db")):
            workspace_path = text
        else:
            initial_path = text
    return initial_path, workspace_path


MISSING_DEPS = (
    "Missing dependencies",
    "Run run.bat once to build the environment and install\n"
    "requirements.txt, then try again.",
)


def main() -> int:
    initial_path, workspace_path = parse_args(sys.argv[1:])

    # bootstrap only needs the standard library, so the splash can be painted
    # before the multi-second application imports start.
    try:
        from bootstrap import launch_gui
    except ImportError as exc:
        show_error(MISSING_DEPS[0], f"{exc}\n\n{MISSING_DEPS[1]}")
        return 1

    log(f"launching (initial_path={initial_path!r}, workspace={workspace_path!r})")

    try:
        app = launch_gui(initial_path=initial_path, workspace_path=workspace_path)
    except ImportError as exc:
        show_error(MISSING_DEPS[0], f"{exc}\n\n{MISSING_DEPS[1]}")
        return 1

    if app is None:
        log("startup cancelled from the splash screen")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        detail = traceback.format_exc()
        log(detail)
        show_error(
            "Startup failed",
            f"{detail.strip().splitlines()[-1]}\n\nFull details were written to:\n{LOG_FILE}"
        )
        sys.exit(1)
