"""Resource base for the vendored converter. Resolves the folder holding the
converter's DLLs + templates both in dev (next to these modules) and inside a
PyInstaller onefile (extracted under sys._MEIPASS)."""
import sys
from pathlib import Path


def base():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "lce" / "converter"
    return Path(__file__).resolve().parent
