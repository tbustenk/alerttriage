#!/usr/bin/env python3
"""
Start a triage run for one client: fetch alerts → analyse → send results.

Usage:
    python scripts/run_client.py --client-id acme-corp
    python scripts/run_client.py --client-id acme-corp --limit 50 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from alerttriage.config.config_manager import ConfigManager
from alerttriage.src.core.analyzer import AlertAnalyzer
from alerttriage.src.logger import configure_logging, bind_client_context, get_logger

log = get_logger(__name__)


def _build_connector(siem_cfg: dict, client_id: str):
    siem_type = siem_cfg.get("type", "none")
    if siem_type == "splunk":
        from alerttriage.src.integrations.splunk_connector import SplunkConnector
        return SplunkConnector({**siem_cfg, "client_id": client_id})
    if siem_type == "elk":
        from alerttriage.src.integrations.elk_connector import ELKConnector
        return ELKConnector({**siem_cfg, "client_id": client_id})
    if siem_type == "webhook":
        from alerttriage.src.integrations.webhook_output import WebhookOutput
        return WebhookOutput({**siem_cfg, "client_id": client_id})
    return None


async def run(client_id: str, *, limit: int, dry_run: bool) -> None:
    config = ConfigManager(config_dir="alerttriage/config", client_id=client_id)
    configure_logging(level=config.log_level, json_output=config.json_logs)
    bind_client_context(client_id)

    client_cfg = config.get_client(client_id)
    siem_cfg = client_cfg.get("siem", {})
    connector = _build_connector(siem_cfg, client_id)

    if connector is None:
        log.warning("no_siem_configured", client=client_id)
        alerts = []
    else:
        ok = await connector.health_check()
        if not ok:
            log.error("siem_unreachable", client=client_id)
            sys.exit(1)
        alerts = await connector.fetch_alerts(limit=limit)
        log.info("alerts_fetched", count=len(alerts))

    if not alerts:
        print("No alerts to process.")
        return

    analyzer = AlertAnalyzer(config)
    results = await analyzer.analyze_batch(alerts, max_concurrency=5)

    for result in results:
        verdict = result.verdict.value
        conf = f"{result.confidence:.0%}"
        cost = f"${result.cost_usd:.4f}"
        print(f"  [{verdict:22s}] conf={conf} cost={cost}  {result.summary[:80]}")

    if connector and not dry_run:
        sent = sum(1 for r in results if await connector.send_result(r))
        log.info("results_sent", sent=sent, total=len(results))

    total_cost = sum(r.cost_usd for r in results)
    print(f"\nDone. {len(results)} alerts | total cost: ${total_cost:.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AlertTriage for a client")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and analyse but do not write results back to SIEM")
    args = parser.parse_args()
    asyncio.run(run(args.client_id, limit=args.limit, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
