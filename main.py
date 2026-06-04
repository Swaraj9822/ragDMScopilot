"""Root import shim for local Uvicorn runs.

Run with:
    uvicorn main:app --reload
"""

from pathlib import Path
import sys


SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from rag_system.api import app  # noqa: E402,F401
