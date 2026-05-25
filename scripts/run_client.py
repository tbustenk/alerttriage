#!/usr/bin/env python3
"""Start a triage run for one client: fetch alerts → analyse → send results.

Usage:
    python scripts/run_client.py --client-id acme-corp
    python scripts/run_client.py --client-id acme-corp --limit 50 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

# Make ``alerttriage.*`` importable when this script is run directly from
# the repo (without ``pip install -e .``). We need the *parent* of the repo
# on sys.path, because the package dotted name starts with ``alerttriage.``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PARENT = _REPO_ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from alerttriage.config.config_manager import ConfigError, ConfigManager  # noqa: E402
from alerttriage.src.core.analyzer import AlertAnalyzer  # noqa: E402
from alerttriage.src.cost_controller import CostLimitExceededError  # noqa: E402
from alerttriage.src.integrations.siem_base import SIEMConnector  # noqa: E402
from alerttriage.src.logger import bind_client_context, configure_logging, get_logger  # noqa: E402

log = get_logger(__name__)


def _build_connector(siem_cfg: dict[str, Any], client_id: str) -> SIEMConnector | None:
    """Construct a SIEM connector from config, or ``None`` if type is unknown."""
    siem_type = (siem_cfg or {}).get("type", "none")
    cfg = {**siem_cfg, "client_id": client_id}
    if siem_type == "splunk":
        from alerttriage.src.integrations.splunk_connector import SplunkConnector

        return SplunkConnector(cfg)
    if siem_type == "elk":
        from alerttriage.src.integrations.elk_connector import ELKConnector

        return ELKConnector(cfg)
    if siem_type == "webhook":
        from alerttriage.src.integrations.webhook_output import WebhookOutput

        return WebhookOutput(cfg)
    if siem_type not in ("none", None, ""):
        log.warning("unknown_siem_type", type=siem_type)
    return None


async def run(client_id: str, *, limit: int, dry_run: bool, config_dir: Path) -> int:
    """Execute one triage cycle for ``client_id``. Returns a process exit code."""
    try:
        config = ConfigManager(config_dir=config_dir, client_id=client_id)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(level=config.log_level, json_output=config.json_logs)
    bind_client_context(client_id)

    client_cfg = config.get_client(client_id)
    connector = _build_connector(client_cfg.get("siem", {}), client_id)

    try:
        if connector is None:
            log.warning("no_siem_configured", client=client_id)
            alerts = []
        else:
            ok = await connector.health_check()
            if not ok:
                log.error("siem_unreachable", client=client_id)
                return 1
            alerts = await connector.fetch_alerts(limit=limit)
            log.info("alerts_fetched", count=len(alerts))

        if not alerts:
            print("No alerts to process.")
            return 0

        analyzer = AlertAnalyzer(config)
        try:
            results = await analyzer.analyze_batch(alerts)
        except CostLimitExceededError as exc:
            log.error("cost_limit_hit", error=str(exc))
            print(f"Cost limit reached: {exc}", file=sys.stderr)
            return 3

        for result in results:
            print(
                f"  [{result.verdict.value:22s}] "
                f"conf={result.confidence:.0%} cost=${result.cost_usd:.4f}  "
                f"{result.summary[:80]}"
            )

        if connector is not None and not dry_run:
            sent = 0
            for result in results:
                if await connector.send_result(result):
                    sent += 1
            log.info("results_sent", sent=sent, total=len(results))

        total_cost = sum(r.cost_usd for r in results)
        print(f"\nDone. {len(results)} alerts | total cost: ${total_cost:.4f}")
        return 0
    finally:
        if connector is not None and hasattr(connector, "close"):
            try:
                await connector.close()
            except Exception as exc:  # noqa: BLE001 — shutdown noise must not crash
                log.warning("connector_close_failed", error=str(exc))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AlertTriage for a client")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and analyse but do not write results back to SIEM",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=_REPO_ROOT / "config",
        help="Directory containing default_config.yaml and client_configs/",
    )
    args = parser.parse_args()
    code = asyncio.run(
        run(args.client_id, limit=args.limit, dry_run=args.dry_run, config_dir=args.config_dir)
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
