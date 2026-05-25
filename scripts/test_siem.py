#!/usr/bin/env python3
"""SIEM connectivity tester for AlertTriage.

Probes your SIEM's connectivity, validates credentials, and optionally
does a schema discovery and sample fetch — all without touching production
alert data unless you add --fetch.

Usage
-----
    python scripts/test_siem.py --config config/siem_splunk_template.yaml
    python scripts/test_siem.py --config config/siem_elk_template.yaml
    python scripts/test_siem.py --config config/siem_splunk_template.yaml --fetch
    python scripts/test_siem.py --config config/siem_elk_template.yaml --fields
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

# Allow running from the repo root or from scripts/
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import yaml

# ── ANSI colour helpers ───────────────────────────────────────────────────────
if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7
        )
    except Exception:
        pass

GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"


def _ok(msg: str)   -> None: print(f"  {GREEN}✓{RESET}  {msg}")
def _fail(msg: str) -> None: print(f"  {RED}✗{RESET}  {msg}")
def _warn(msg: str) -> None: print(f"  {YELLOW}⚠{RESET}  {msg}")
def _info(msg: str) -> None: print(f"  {CYAN}·{RESET}  {msg}")
def _header(msg: str) -> None: print(f"\n{BOLD}{msg}{RESET}")


# ── Config loading ────────────────────────────────────────────────────────────

def _load_config(path: Path) -> dict:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    siem_block = raw.get("siem", {})
    # Pull client_id up into siem config so connectors can resolve env vars
    if "client_id" not in siem_block and "client_id" in raw:
        siem_block["client_id"] = raw["client_id"]
    return siem_block


def _detect_siem_type(config: dict, path: Path) -> str:
    if config.get("type"):
        return str(config["type"]).lower()
    name = path.stem.lower()
    if "splunk" in name:
        return "splunk"
    if "elk" in name or "elastic" in name:
        return "elk"
    return ""


# ── Connector factory ─────────────────────────────────────────────────────────

def _make_connector(siem_type: str, config: dict):
    if siem_type == "splunk":
        from alerttriage.src.integrations.splunk_connector import SplunkConnector
        return SplunkConnector(config)
    if siem_type == "elk":
        from alerttriage.src.integrations.elk_connector import ELKConnector
        return ELKConnector(config)
    raise SystemExit(
        f"{RED}Unknown SIEM type '{siem_type}'. "
        f"Expected 'splunk' or 'elk'.{RESET}"
    )


# ── Test runner ───────────────────────────────────────────────────────────────

async def _run(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    if not config_path.exists():
        _fail(f"Config file not found: {config_path}")
        return 2

    _header(f"AlertTriage SIEM Connectivity Test — {config_path.name}")

    # Load config
    try:
        siem_cfg = _load_config(config_path)
    except Exception as exc:
        _fail(f"Failed to parse config: {exc}")
        return 2

    siem_type = _detect_siem_type(siem_cfg, config_path)
    if not siem_type:
        _fail(
            "Cannot determine SIEM type. Add  type: splunk  or  type: elk  "
            "to the 'siem:' block, or use a filename containing 'splunk'/'elk'."
        )
        return 2

    _info(f"SIEM type  : {siem_type.upper()}")
    _info(f"Host       : {siem_cfg.get('host', '(not set)')}")
    _info(f"Port       : {siem_cfg.get('port', '(default)')}")
    _info(f"Client ID  : {siem_cfg.get('client_id', '(not set)')}")
    _info(f"SSL verify : {siem_cfg.get('verify_ssl', True)}")

    # Validate config
    _header("1. Config validation")
    try:
        if siem_type == "splunk":
            from alerttriage.src.integrations.splunk_connector import SplunkConnector
            SplunkConnector.validate_config(siem_cfg)
        else:
            from alerttriage.src.integrations.elk_connector import ELKConnector
            ELKConnector.validate_config(siem_cfg)
        _ok("Config looks good — required keys present, auth env var found")
    except Exception as exc:
        _fail(f"Config invalid: {exc}")
        return 2

    connector = _make_connector(siem_type, siem_cfg)
    exit_code = 0

    # Health check
    _header("2. Connectivity & auth")
    t0 = time.monotonic()
    try:
        healthy = await connector.health_check()
        elapsed = int((time.monotonic() - t0) * 1000)
        if healthy:
            _ok(f"Health check passed ({elapsed} ms)")
        else:
            _fail(f"Health check returned False ({elapsed} ms)")
            _warn("Check host/port, firewall rules, and credentials.")
            exit_code = 1
    except Exception as exc:
        _fail(f"Health check raised: {exc}")
        exit_code = 1

    # Field discovery
    if args.fields or args.all:
        _header("3. Field discovery (schema)")
        index = siem_cfg.get("index", "notable" if siem_type == "splunk" else ".siem-signals-*")
        try:
            t0 = time.monotonic()
            fields = await connector.get_field_names(index)
            elapsed = int((time.monotonic() - t0) * 1000)
            _ok(f"Discovered {len(fields)} fields in '{index}' ({elapsed} ms)")
            preview = fields[:20]
            _info("First 20 fields: " + ", ".join(preview) + ("…" if len(fields) > 20 else ""))
        except Exception as exc:
            _fail(f"Field discovery failed: {exc}")
            exit_code = max(exit_code, 1)

    # Sample fetch
    if args.fetch or args.all:
        _header("4. Alert fetch (sample — limit 5)")
        try:
            t0 = time.monotonic()
            alerts = await connector.fetch_alerts(limit=5)
            elapsed = int((time.monotonic() - t0) * 1000)
            _ok(f"Fetched {len(alerts)} alert(s) ({elapsed} ms)")
            for i, alert in enumerate(alerts, 1):
                _info(
                    f"  [{i}] {alert.severity.value.upper():8s}  "
                    f"{alert.rule_name}  —  {alert.title[:60]}"
                )
        except Exception as exc:
            _fail(f"Alert fetch failed: {exc}")
            exit_code = max(exit_code, 1)

    await connector.close()

    # Summary
    _header("Result")
    if exit_code == 0:
        print(f"  {GREEN}{BOLD}All checks passed.{RESET} AlertTriage can connect to your SIEM.\n")
    else:
        print(f"  {RED}{BOLD}One or more checks failed.{RESET} See output above for details.\n")

    return exit_code


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test AlertTriage connectivity to a SIEM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config", "-c",
        required=True,
        metavar="PATH",
        help="Path to a SIEM config YAML file (see config/siem_*_template.yaml).",
    )
    parser.add_argument(
        "--fields",
        action="store_true",
        help="Also run field/schema discovery.",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Also fetch up to 5 real alerts (read-only).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all checks: health, fields, and fetch.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
