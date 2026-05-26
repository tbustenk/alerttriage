"""Comprehensive health monitoring and metrics for AlertTriage.

Tracks:
  - Component health: database(s), feedback system, API self-check
  - Performance: request latency percentiles (p50/p95/p99), throughput
  - Accuracy: per-client AI vs. analyst agreement rate
  - Cost: daily / monthly AI API spend (from CostController state files)

Alert thresholds (configurable):
  - Accuracy drops below ``accuracy_threshold`` (default 70 %)
  - P95 latency exceeds ``latency_threshold_ms`` (default 1 000 ms)

Outputs:
  - :meth:`check_health` — structured :class:`HealthStatus`
  - :meth:`get_dashboard` — full JSON dict for ``GET /dashboard/health``
  - :meth:`prometheus_metrics` — Prometheus text-format string for ``GET /metrics``
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

from alerttriage.src.logger import get_logger

if TYPE_CHECKING:
    from alerttriage.config.config_manager import ConfigManager

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ComponentHealth:
    name: str
    healthy: bool
    latency_ms: float | None = None
    details: str = ""


@dataclass
class AlertThreshold:
    metric: str
    threshold: float
    current: float
    breached: bool
    message: str


@dataclass
class HealthStatus:
    status: str  # healthy | degraded | unhealthy
    components: list[ComponentHealth]
    thresholds: list[AlertThreshold]
    uptime_seconds: float
    timestamp: datetime


# ---------------------------------------------------------------------------
# Rolling metrics store
# ---------------------------------------------------------------------------


class MetricsStore:
    """In-memory rolling window for latency and throughput metrics."""

    def __init__(self, window_sec: int = 3600) -> None:
        self._window = window_sec
        self._latencies: deque[tuple[float, float]] = deque()  # (ts, ms)
        self._lock = Lock()
        self._alerts_total: int = 0
        self._alerts_today: int = 0
        self._today_anchor: float = time.time()

    def record(self, latency_ms: float) -> None:
        now = time.time()
        with self._lock:
            if now - self._today_anchor >= 86400:
                self._alerts_today = 0
                self._today_anchor = now
            self._alerts_total += 1
            self._alerts_today += 1
            cutoff = now - self._window
            while self._latencies and self._latencies[0][0] < cutoff:
                self._latencies.popleft()
            self._latencies.append((now, latency_ms))

    def percentile(self, pct: float) -> float:
        with self._lock:
            if not self._latencies:
                return 0.0
            vals = sorted(v for _, v in self._latencies)
        idx = max(0, min(int(len(vals) * pct / 100), len(vals) - 1))
        return vals[idx]

    @property
    def alerts_total(self) -> int:
        return self._alerts_total

    @property
    def alerts_today(self) -> int:
        return self._alerts_today


# ---------------------------------------------------------------------------
# HealthMonitor
# ---------------------------------------------------------------------------


class HealthMonitor:
    """Central health and observability hub for AlertTriage.

    Args:
        config: Validated :class:`ConfigManager`.
        accuracy_threshold: Minimum acceptable overall accuracy (0–1).
        latency_threshold_ms: P95 latency threshold in milliseconds.
        notification_config: Optional dict passed to :class:`Notifier`.
    """

    def __init__(
        self,
        config: ConfigManager,
        *,
        accuracy_threshold: float = 0.70,
        latency_threshold_ms: float = 1000.0,
        notification_config: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self._accuracy_threshold = accuracy_threshold
        self._latency_threshold_ms = latency_threshold_ms
        self._start_time = time.monotonic()
        self.metrics = MetricsStore()
        self._last_health: HealthStatus | None = None
        self._notifier = None
        if notification_config:
            from alerttriage.src.monitoring.notifier import Notifier

            self._notifier = Notifier(notification_config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._start_time

    def record_request(self, latency_ms: float) -> None:
        """Record one completed analysis (call from the API on every /analyze)."""
        self.metrics.record(latency_ms)

    async def check_health(
        self,
        accuracy_by_client: dict[str, float] | None = None,
    ) -> HealthStatus:
        """Run all component checks and evaluate alert thresholds.

        Args:
            accuracy_by_client: Map of client_id → accuracy fraction.
                When provided, accuracy thresholds are checked per client.
        """
        components: list[ComponentHealth] = []
        components.extend(await self._check_databases())
        components.append(await self._check_feedback_module())
        components.append(ComponentHealth(name="api", healthy=True, latency_ms=0.0))

        thresholds = self._check_thresholds(accuracy_by_client)

        if any(not c.healthy for c in components):
            overall = "unhealthy"
        elif any(t.breached for t in thresholds):
            overall = "degraded"
        else:
            overall = "healthy"

        status = HealthStatus(
            status=overall,
            components=components,
            thresholds=thresholds,
            uptime_seconds=self.uptime_seconds,
            timestamp=datetime.now(timezone.utc),
        )
        self._last_health = status

        if self._notifier:
            await self._maybe_notify(status)

        return status

    async def get_dashboard(
        self,
        accuracy_by_client: dict[str, float] | None = None,
        cost_by_client: dict[str, dict[str, float]] | None = None,
    ) -> dict[str, Any]:
        """Return a full JSON dashboard dict for ``GET /dashboard/health``."""
        health = await self.check_health(accuracy_by_client=accuracy_by_client)
        m = self.metrics

        return {
            "status": health.status,
            "version": "2.0.0",
            "uptime_seconds": round(health.uptime_seconds, 2),
            "timestamp": health.timestamp.isoformat(),
            "performance": {
                "alerts_total": m.alerts_total,
                "alerts_today": m.alerts_today,
                "latency_p50_ms": round(m.percentile(50), 2),
                "latency_p95_ms": round(m.percentile(95), 2),
                "latency_p99_ms": round(m.percentile(99), 2),
            },
            "accuracy": {
                client_id: round(acc, 4)
                for client_id, acc in (accuracy_by_client or {}).items()
            },
            "cost": cost_by_client or {},
            "components": [
                {
                    "name": c.name,
                    "healthy": c.healthy,
                    "latency_ms": c.latency_ms,
                    "details": c.details,
                }
                for c in health.components
            ],
            "thresholds": [
                {
                    "metric": t.metric,
                    "threshold": t.threshold,
                    "current": round(t.current, 4),
                    "breached": t.breached,
                    "message": t.message,
                }
                for t in health.thresholds
            ],
        }

    def prometheus_metrics(
        self,
        accuracy_by_client: dict[str, float] | None = None,
        cost_by_client: dict[str, dict[str, float]] | None = None,
    ) -> str:
        """Render current metrics in Prometheus text exposition format."""
        m = self.metrics
        health = self._last_health
        lines: list[str] = []

        def _g(name: str, value: float, help_text: str, labels: str = "") -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            if labels:
                lines.append(f"{name}{{{labels}}} {value}")
            else:
                lines.append(f"{name} {value}")

        def _c(name: str, value: float, help_text: str) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {value}")

        _g("alerttriage_uptime_seconds", round(self.uptime_seconds, 2), "Seconds since process start")
        _c("alerttriage_alerts_analyzed_total", m.alerts_total, "Total alerts analyzed since process start")
        _g("alerttriage_alerts_analyzed_today", m.alerts_today, "Alerts analyzed today (UTC day)")
        _g("alerttriage_latency_p50_ms", round(m.percentile(50), 2), "P50 analysis latency in milliseconds")
        _g("alerttriage_latency_p95_ms", round(m.percentile(95), 2), "P95 analysis latency in milliseconds")
        _g("alerttriage_latency_p99_ms", round(m.percentile(99), 2), "P99 analysis latency in milliseconds")

        if accuracy_by_client:
            lines.append("# HELP alerttriage_accuracy Analyst-agreement accuracy by client (0–1)")
            lines.append("# TYPE alerttriage_accuracy gauge")
            for cid, acc in accuracy_by_client.items():
                lines.append(f'alerttriage_accuracy{{client_id="{cid}"}} {round(acc, 4)}')

        if cost_by_client:
            lines.append("# HELP alerttriage_cost_today_usd AI API spend today in USD")
            lines.append("# TYPE alerttriage_cost_today_usd gauge")
            for cid, spend in cost_by_client.items():
                lines.append(f'alerttriage_cost_today_usd{{client_id="{cid}"}} {spend.get("daily_usd", 0):.6f}')
            lines.append("# HELP alerttriage_cost_month_usd AI API spend this month in USD")
            lines.append("# TYPE alerttriage_cost_month_usd gauge")
            for cid, spend in cost_by_client.items():
                lines.append(f'alerttriage_cost_month_usd{{client_id="{cid}"}} {spend.get("monthly_usd", 0):.6f}')

        if health:
            lines.append("# HELP alerttriage_component_healthy Component health status (1=healthy, 0=unhealthy)")
            lines.append("# TYPE alerttriage_component_healthy gauge")
            for c in health.components:
                lines.append(f'alerttriage_component_healthy{{component="{c.name}"}} {1 if c.healthy else 0}')

        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # Internal health checks
    # ------------------------------------------------------------------

    async def _check_databases(self) -> list[ComponentHealth]:
        results: list[ComponentHealth] = []
        data_dir: Path = self.config.data_dir

        clients = self.config.list_clients()
        if not clients:
            results.append(ComponentHealth(name="db:default", healthy=True, details="no clients configured"))
            return results

        for client_id in clients:
            db_path = data_dir / client_id / "feedback.db"
            if not db_path.exists():
                results.append(ComponentHealth(
                    name=f"db:{client_id}",
                    healthy=True,
                    details="not yet created",
                ))
                continue
            start = time.monotonic()
            try:
                conn = sqlite3.connect(str(db_path), timeout=2.0)
                conn.execute("SELECT 1")
                conn.close()
                latency = (time.monotonic() - start) * 1000
                results.append(ComponentHealth(
                    name=f"db:{client_id}",
                    healthy=True,
                    latency_ms=round(latency, 2),
                ))
            except Exception as exc:  # noqa: BLE001
                results.append(ComponentHealth(
                    name=f"db:{client_id}",
                    healthy=False,
                    details=str(exc)[:200],
                ))
        return results

    async def _check_feedback_module(self) -> ComponentHealth:
        try:
            from alerttriage.src.feedback.feedback_system import FeedbackSystem  # noqa: F401
            return ComponentHealth(name="feedback_system", healthy=True)
        except Exception as exc:  # noqa: BLE001
            return ComponentHealth(name="feedback_system", healthy=False, details=str(exc)[:200])

    def _check_thresholds(
        self,
        accuracy_by_client: dict[str, float] | None,
    ) -> list[AlertThreshold]:
        thresholds: list[AlertThreshold] = []
        m = self.metrics

        p95 = m.percentile(95)
        thresholds.append(AlertThreshold(
            metric="api_latency_p95_ms",
            threshold=self._latency_threshold_ms,
            current=p95,
            breached=p95 > self._latency_threshold_ms,
            message=(
                f"P95 latency {p95:.0f}ms exceeds threshold of "
                f"{self._latency_threshold_ms:.0f}ms"
            ),
        ))

        if accuracy_by_client:
            for client_id, acc in accuracy_by_client.items():
                thresholds.append(AlertThreshold(
                    metric=f"accuracy:{client_id}",
                    threshold=self._accuracy_threshold,
                    current=acc,
                    breached=acc < self._accuracy_threshold and acc > 0,
                    message=(
                        f"Client '{client_id}' accuracy {acc:.0%} is below "
                        f"threshold of {self._accuracy_threshold:.0%}"
                    ),
                ))

        return thresholds

    async def _maybe_notify(self, health: HealthStatus) -> None:
        if not self._notifier:
            return

        issues = []
        if health.status != "healthy":
            issues.append(f"Overall system status: {health.status.upper()}")
        for component in health.components:
            if not component.healthy:
                issues.append(f"Component '{component.name}' is DOWN: {component.details}")
        for t in health.thresholds:
            if t.breached:
                issues.append(t.message)

        if not issues:
            return

        await self._notifier.send_alert(
            subject=f"AlertTriage Health Alert — {health.status.upper()}",
            body="\n".join(f"• {issue}" for issue in issues),
        )
