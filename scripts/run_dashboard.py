#!/usr/bin/env python3
"""Start the AlertTriage web dashboard.

Usage:
    python scripts/run_dashboard.py --client-id <id>
    python scripts/run_dashboard.py --client-id <id> --port 8080 --debug
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"

sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="AlertTriage Web Dashboard")
    parser.add_argument("--client-id", required=True, help="Client ID whose feedback.db to load")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5000, help="Port (default: 5000)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    os.environ["ALERTTRIAGE_CLIENT_ID"] = args.client_id

    spec = importlib.util.spec_from_file_location("dashboard.app", DASHBOARD / "app.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    db_path = ROOT / "data" / args.client_id / "feedback.db"
    print(f"\nAlertTriage Dashboard")
    print(f"  Client  : {args.client_id}")
    print(f"  Database: {db_path}")
    print(f"  URL     : http://{args.host}:{args.port}\n")

    mod.app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
