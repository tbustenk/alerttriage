#!/usr/bin/env python3
"""
Migrate client configs between AlertTriage versions.

Usage:
    python scripts/migrate_config.py --from-version 1 --to-version 2
    python scripts/migrate_config.py --client-id acme-corp --from-version 1 --to-version 2
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path

import yaml

CLIENT_DIR = Path(__file__).resolve().parent.parent / "config" / "client_configs"


def _backup(path: Path) -> Path:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    bak = path.with_suffix(f".{ts}.bak.yaml")
    shutil.copy2(path, bak)
    return bak


def migrate_v1_to_v2(config: dict) -> dict:
    """
    v1 → v2 changes:
      - 'scanner' key renamed to 'siem'
      - 'api_token' moved under siem.token
      - 'budget' split into cost_limits.daily_usd / monthly_usd
    """
    if "scanner" in config and "siem" not in config:
        config["siem"] = config.pop("scanner")

    if "api_token" in config:
        config.setdefault("siem", {})["token"] = config.pop("api_token")

    if "budget" in config:
        budget = config.pop("budget")
        config["cost_limits"] = {
            "daily_usd": budget.get("daily"),
            "monthly_usd": budget.get("monthly"),
        }

    config.setdefault("anonymize", True)
    config.setdefault("model_fallbacks", [])
    return config


MIGRATIONS: dict[tuple[int, int], object] = {
    (1, 2): migrate_v1_to_v2,
}


def migrate_file(path: Path, *, from_version: int, to_version: int) -> None:
    key = (from_version, to_version)
    fn = MIGRATIONS.get(key)
    if fn is None:
        print(f"  No migration path {from_version}→{to_version} for {path.name}")
        return

    with open(path, encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}

    bak = _backup(path)
    config = fn(config)  # type: ignore[operator]

    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump(config, fh, allow_unicode=True, sort_keys=False)

    print(f"  Migrated {path.name}  (backup: {bak.name})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate AlertTriage client configs")
    parser.add_argument("--from-version", type=int, required=True)
    parser.add_argument("--to-version", type=int, required=True)
    parser.add_argument("--client-id", help="Migrate only this client; default: all")
    args = parser.parse_args()

    if args.client_id:
        targets = [CLIENT_DIR / f"{args.client_id}.yaml"]
    else:
        targets = list(CLIENT_DIR.glob("*.yaml"))
        targets = [t for t in targets if t.stem != "template"]

    if not targets:
        print("No client configs found.")
        return

    print(f"Migrating {len(targets)} config(s) from v{args.from_version} → v{args.to_version}…")
    for t in targets:
        migrate_file(t, from_version=args.from_version, to_version=args.to_version)
    print("Done.")


if __name__ == "__main__":
    main()
