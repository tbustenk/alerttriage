#!/usr/bin/env python3
"""Start the AlertTriage REST API server.

Usage:
    python scripts/run_api.py
    python scripts/run_api.py --port 8080 --host 0.0.0.0
    python scripts/run_api.py --reload   # development hot-reload
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make ``alerttriage.*`` importable when run directly from the repo.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PARENT = _REPO_ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

import uvicorn  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="AlertTriage REST API server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable hot-reload (development only)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of worker processes (default: 1; use >1 only without --reload)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="Uvicorn log level (default: info)",
    )
    args = parser.parse_args()

    uvicorn.run(
        "alerttriage.src.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=None if args.reload else args.workers,
        log_level=args.log_level,
        access_log=True,
    )


if __name__ == "__main__":
    main()
