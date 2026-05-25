#!/usr/bin/env python3
"""Interactive wizard for onboarding a new AlertTriage client.

Usage:
    python scripts/init_client.py
    python scripts/init_client.py --client-id acme-corp --non-interactive
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = _REPO_ROOT / "config"
CLIENT_DIR = CONFIG_DIR / "client_configs"
DATA_DIR = _REPO_ROOT / "data"

SIEM_TYPES = ["splunk", "elk", "webhook", "none"]
MODELS = ["claude-sonnet", "claude-opus", "claude-haiku", "gpt-4o", "gpt-4o-mini"]


def _prompt(label: str, default: str = "", *, required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"  {label}{suffix}: ").strip() or default
        if required and not value:
            print("    (required — please enter a value)")
            continue
        return value


def _choice(label: str, options: list[str], default: str) -> str:
    joined = " / ".join(f"[{o}]" if o == default else o for o in options)
    while True:
        raw = input(f"  {label} ({joined}): ").strip().lower() or default
        if raw in options:
            return raw
        print(f"    Must be one of: {', '.join(options)}")


def _slug(s: str) -> str:
    """Lowercase, hyphen-only client identifier."""
    return re.sub(r"[^a-z0-9\-]", "-", s.lower()).strip("-")


def wizard() -> dict[str, Any]:
    """Walk the operator through an interactive client setup."""
    print("\n=== AlertTriage v2 — New Client Setup ===\n")

    display_name = _prompt("Client display name", required=True)
    client_id = _prompt("Client ID (slug)", default=_slug(display_name), required=True)
    client_id = _slug(client_id)

    print()
    model = _choice("AI model", MODELS, default="claude-sonnet")
    tier = _choice("Service tier", ["standard", "premium", "enterprise"], default="standard")

    print()
    siem_type = _choice("SIEM type", SIEM_TYPES, default="splunk")
    siem: dict[str, Any] = {"type": siem_type}

    if siem_type == "splunk":
        siem["host"] = _prompt("Splunk host", required=True)
        siem["port"] = int(_prompt("Splunk REST port", default="8089"))
        siem["token"] = _prompt("Splunk token (or leave blank; set via env var)")
        siem["verify_ssl"] = _prompt("Verify SSL?", default="true").lower() == "true"
    elif siem_type == "elk":
        siem["host"] = _prompt("Elasticsearch host", required=True)
        siem["port"] = int(_prompt("ES port", default="9200"))
        siem["kibana_port"] = int(_prompt("Kibana port", default="5601"))
        siem["api_key"] = _prompt("API key (base64 id:secret)")
    elif siem_type == "webhook":
        siem["url"] = _prompt("Inbound webhook URL", required=True)

    print()
    daily_limit = _prompt("Daily cost limit USD (blank = no limit)", default="")
    monthly_limit = _prompt("Monthly cost limit USD (blank = no limit)", default="")

    return {
        "client_id": client_id,
        "display_name": display_name,
        "tier": tier,
        "model": model,
        "model_fallbacks": [],
        "siem": siem,
        "anonymize": True,
        "cost_limits": {
            "daily_usd": float(daily_limit) if daily_limit else None,
            "monthly_usd": float(monthly_limit) if monthly_limit else None,
        },
        "metadata": {
            "contact": _prompt("SOC contact email"),
            "timezone": _prompt("Timezone", default="UTC"),
        },
    }


def write_config(config: dict[str, Any]) -> Path:
    """Persist a client YAML and create the data directory. Returns the YAML path."""
    CLIENT_DIR.mkdir(parents=True, exist_ok=True)
    out = CLIENT_DIR / f"{config['client_id']}.yaml"
    if out.exists():
        overwrite = input(f"\n  {out} already exists. Overwrite? [y/N]: ").strip().lower()
        if overwrite != "y":
            print("  Aborted.")
            sys.exit(0)

    with open(out, "w", encoding="utf-8") as fh:
        yaml.dump(config, fh, allow_unicode=True, sort_keys=False)

    (DATA_DIR / config["client_id"]).mkdir(parents=True, exist_ok=True)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="AlertTriage client setup wizard")
    parser.add_argument("--client-id", help="Skip wizard; use this client ID")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()

    if args.non_interactive and not args.client_id:
        parser.error("--non-interactive requires --client-id")

    config = wizard()
    out = write_config(config)

    print(f"\n  Config written to: {out}")
    print(f"  Data directory:    {DATA_DIR / config['client_id']}/")
    print("\n  Run your first scan:")
    print(f"    python scripts/run_client.py --client-id {config['client_id']}\n")


if __name__ == "__main__":
    main()
