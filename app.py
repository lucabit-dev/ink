"""
Vercel entrypoint: re-export the FastAPI application from `web.app`.

Local development can still use: `uvicorn web.app:app` from the project root.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.app import app  # noqa: E402
