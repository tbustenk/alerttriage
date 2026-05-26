"""AlertTriage REST API — FastAPI application.

Core endpoints
--------------
POST   /analyze              Submit an alert for AI analysis
GET    /results/{alert_id}   Retrieve a cached analysis result
POST   /feedback             Record analyst feedback
GET    /stats/{client_id}    Accuracy and rule-level statistics
GET    /hints                Learned prompt hints for a client
POST   /context              Store environment context for future analyses
GET    /health               System health check (no auth required)
GET    /dashboard/health     Full health dashboard (auth required)
GET    /metrics              Prometheus text-format metrics

Extension routers (mounted automatically)
-----------------------------------------
/analytics/*   Advanced analytics and ROI reporting
/webhooks/*    Webhook registration and event delivery
/config/*      Config management, versioning, feature flags
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from alerttriage.src.api.auth import require_api_key
from alerttriage.src.api.models import (
    AnalysisResponse,
    AnalyzeRequest,
    ContextRequest,
    FeedbackRequest,
    HealthComponentStatus,
    HealthResponse,
    HintsResponse,
    RuleStatsItem,
    StatsResponse,
)
from alerttriage.src.api.rate_limit import limiter, request_key
from alerttriage.src.core.alert_models import Alert, AlertContext, AlertSource
from alerttriage.src.core.analyzer import AlertAnalyzer
from alerttriage.src.core.result_models import AnalysisResult
from alerttriage.src.feedback.feedback_system import FeedbackSystem
from alerttriage.src.logger import configure_logging, get_logger
from alerttriage.src.monitoring.health_monitor import HealthMonitor

log = get_logger(__name__)

VERSION = "2.0.0"

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_DIR = Path(os.environ.get("ALERTTRIAGE_CONFIG_DIR", str(_REPO_ROOT / "config")))

# ---------------------------------------------------------------------------
# Module-level singletons (populated in lifespan)
# ---------------------------------------------------------------------------

_analyzer: AlertAnalyzer | None = None
_monitor: HealthMonitor | None = None
_result_store: dict[str, AnalysisResult] = {}       # alert_id → result (LRU-capped)
_context_store: dict[str, list[dict[str, Any]]] = {}  # client_id → list of context items
_feedback_systems: dict[str, FeedbackSystem] = {}

# Extension singletons
_analytics_engine: Any = None
_webhook_manager: Any = None
_report_scheduler: Any = None
_config_versioning: Any = None
_feature_flags: Any = None
_alert_type_settings: Any = None
_config_watcher: Any = None
_reports_dir: Path = Path("reports")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_analyzer() -> AlertAnalyzer:
    if _analyzer is None:
        raise HTTPException(status_code=503, detail="Analyzer not initialised")
    return _analyzer


def _get_monitor() -> HealthMonitor:
    if _monitor is None:
        raise HTTPException(status_code=503, detail="Health monitor not initialised")
    return _monitor


def _feedback_system(client_id: str) -> FeedbackSystem:
    if client_id not in _feedback_systems:
        _feedback_systems[client_id] = FeedbackSystem(
            _get_analyzer().config.data_dir, client_id
        )
    return _feedback_systems[client_id]


def _result_to_response(result: AnalysisResult) -> AnalysisResponse:
    return AnalysisResponse(
        id=result.id,
        alert_id=result.alert_id,
        client_id=result.client_id,
        model_id=result.model_id,
        verdict=result.verdict.value,
        confidence=result.confidence,
        summary=result.summary,
        reasoning=result.reasoning,
        risk_factors=[rf.model_dump() for rf in result.risk_factors],
        recommended_actions=[ra.model_dump() for ra in result.recommended_actions],
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        timestamp=result.timestamp,
    )


def _cache_result(result: AnalysisResult, *, max_size: int = 10_000) -> None:
    """Store a result and evict the oldest entry when the cache is full."""
    _result_store[result.alert_id] = result
    if len(_result_store) > max_size:
        oldest_key = next(iter(_result_store))
        del _result_store[oldest_key]


def _build_notification_config() -> dict[str, Any] | None:
    slack_url = os.environ.get("ALERTTRIAGE_SLACK_WEBHOOK_URL")
    email_to = os.environ.get("ALERTTRIAGE_ALERT_EMAIL_TO")
    if not slack_url and not email_to:
        return None
    cfg: dict[str, Any] = {}
    if slack_url:
        cfg["slack_webhook_url"] = slack_url
    if email_to:
        cfg["email"] = {
            "to_addr": email_to,
            "from_addr": os.environ.get("ALERTTRIAGE_ALERT_EMAIL_FROM", "alerttriage@localhost"),
            "smtp_host": os.environ.get("ALERTTRIAGE_SMTP_HOST", "localhost"),
            "smtp_port": int(os.environ.get("ALERTTRIAGE_SMTP_PORT", "587")),
            "use_tls": os.environ.get("ALERTTRIAGE_SMTP_TLS", "true").lower() in ("1", "true"),
            "username": os.environ.get("ALERTTRIAGE_SMTP_USER"),
            "password": os.environ.get("ALERTTRIAGE_SMTP_PASS"),
        }
    return cfg


def _build_smtp_config() -> dict[str, Any] | None:
    smtp_host = os.environ.get("ALERTTRIAGE_SMTP_HOST")
    if not smtp_host:
        return None
    return {
        "host": smtp_host,
        "port": int(os.environ.get("ALERTTRIAGE_SMTP_PORT", "587")),
        "use_tls": os.environ.get("ALERTTRIAGE_SMTP_TLS", "true").lower() in ("1", "true"),
        "username": os.environ.get("ALERTTRIAGE_SMTP_USER"),
        "password": os.environ.get("ALERTTRIAGE_SMTP_PASS"),
        "from_addr": os.environ.get("ALERTTRIAGE_ALERT_EMAIL_FROM", "alerttriage@localhost"),
    }


async def _on_config_change(changed_path: Path) -> None:
    log.info("config_changed", path=str(changed_path))
    if _feature_flags:
        _feature_flags.reload()
    if _alert_type_settings:
        _alert_type_settings.reload()


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    global _analyzer, _monitor
    global _analytics_engine, _webhook_manager, _report_scheduler
    global _config_versioning, _feature_flags, _alert_type_settings
    global _config_watcher, _reports_dir

    configure_logging(
        level=os.environ.get("ALERTTRIAGE_LOG_LEVEL", "INFO"),
        json_output=os.environ.get("ALERTTRIAGE_JSON_LOGS", "").lower() in ("1", "true"),
    )

    from alerttriage.config.config_manager import ConfigManager

    config = ConfigManager(config_dir=_CONFIG_DIR)
    _analyzer = AlertAnalyzer(config)

    _monitor = HealthMonitor(
        config,
        accuracy_threshold=float(os.environ.get("ALERTTRIAGE_ACCURACY_THRESHOLD", "0.70")),
        latency_threshold_ms=float(os.environ.get("ALERTTRIAGE_LATENCY_THRESHOLD_MS", "1000")),
        notification_config=_build_notification_config(),
    )

    data_dir = _analyzer.config.data_dir
    _reports_dir = Path(os.environ.get("ALERTTRIAGE_REPORTS_DIR", str(_REPO_ROOT / "reports")))
    _reports_dir.mkdir(parents=True, exist_ok=True)

    from alerttriage.src.analytics.engine import AnalyticsEngine
    _analytics_engine = AnalyticsEngine(data_dir, config)

    from alerttriage.src.config_ext.versioning import ConfigVersioning
    _config_versioning = ConfigVersioning(data_dir, _CONFIG_DIR)

    from alerttriage.src.config_ext.feature_flags import FeatureFlagManager
    _feature_flags = FeatureFlagManager(_CONFIG_DIR, config)

    from alerttriage.src.config_ext.alert_type_settings import AlertTypeSettingsManager
    _alert_type_settings = AlertTypeSettingsManager(_CONFIG_DIR, config)

    from alerttriage.src.webhooks.manager import WebhookManager
    _webhook_manager = WebhookManager(data_dir)

    from alerttriage.src.analytics.scheduler import ReportScheduler
    _report_scheduler = ReportScheduler(
        data_dir=data_dir,
        config=config,
        reports_dir=_reports_dir,
        smtp_config=_build_smtp_config(),
    )

    from alerttriage.src.config_ext.hot_reload import ConfigWatcher
    _config_watcher = ConfigWatcher(config, _CONFIG_DIR, on_change=_on_config_change)

    _scheduler_task = asyncio.create_task(_report_scheduler.run())
    _watcher_task = asyncio.create_task(_config_watcher.run())

    from alerttriage.src.api.auth import VALID_KEYS

    if not VALID_KEYS:
        log.warning(
            "api_open_access",
            detail="ALERTTRIAGE_API_KEYS not set — all requests accepted without authentication",
        )

    log.info("api_started", config_dir=str(_CONFIG_DIR), version=VERSION)
    yield
    log.info("api_stopping")
    _scheduler_task.cancel()
    _watcher_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(_scheduler_task, _watcher_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AlertTriage API",
    description=(
        "AI-powered security alert triage. "
        "Submit alerts, receive AI verdicts, record analyst feedback, "
        "and track model accuracy over time."
    ),
    version=VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALERTTRIAGE_CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from alerttriage.src.api.security import RequestSizeLimitMiddleware, SecurityHeadersMiddleware  # noqa: E402
from alerttriage.src.api.audit import AuditMiddleware  # noqa: E402
from alerttriage.src.performance.profiling import TimingMiddleware  # noqa: E402

# Pure-ASGI middleware — wrap outermost first (executed last on request path)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestSizeLimitMiddleware)
app.add_middleware(AuditMiddleware)
app.add_middleware(  # type: ignore[call-arg]
    TimingMiddleware,
    slow_threshold_ms=float(os.environ.get("ALERTTRIAGE_SLOW_REQUEST_MS", "500")),
)

# Extension routers
from alerttriage.src.api.routers.analytics import router as _analytics_router  # noqa: E402
from alerttriage.src.api.routers.config_api import router as _config_router  # noqa: E402
from alerttriage.src.api.routers.webhooks import router as _webhooks_router  # noqa: E402

app.include_router(_analytics_router)
app.include_router(_webhooks_router)
app.include_router(_config_router)


# ---------------------------------------------------------------------------
# Analysis endpoints
# ---------------------------------------------------------------------------


@app.post(
    "/analyze",
    response_model=AnalysisResponse,
    status_code=status.HTTP_200_OK,
    summary="Submit an alert for AI triage analysis",
    tags=["Analysis"],
)
async def analyze_alert(
    request: Request,
    body: AnalyzeRequest,
    _key: str = Depends(require_api_key),
) -> AnalysisResponse:
    """
    Run an alert through the full triage pipeline:

    1. Budget check (client cost limits)
    2. PII anonymisation
    3. Prompt enhancement with learned feedback hints
    4. AI analysis (Claude / GPT with fallback)
    5. Cost recording

    Returns a verdict, confidence score, reasoning, and recommended actions.
    """
    limiter.check(request_key(request))

    analyzer = _get_analyzer()
    monitor = _get_monitor()

    # Build context, injecting any stored client-level context.
    ctx_data = body.context
    stored_ctx = _context_store.get(body.client_id, [])
    for item in stored_ctx:
        ctx_type = item.get("context_type", "")
        data = item.get("data", {})
        if ctx_type == "threat_intel":
            ctx_data.setdefault("threat_intel", {}).update(data)
        elif ctx_type == "network_ranges":
            ctx_data.setdefault("network_info", {}).update(data)
        elif ctx_type == "known_services":
            ctx_data.setdefault("host_info", {}).update(data)

    try:
        alert = Alert(
            client_id=body.client_id,
            source=AlertSource(body.source),
            rule_name=body.rule_name,
            severity=body.severity,  # type: ignore[arg-type]
            title=body.title,
            description=body.description,
            raw_payload=body.raw_payload,
            context=AlertContext(
                host_info=ctx_data.get("host_info", {}),
                user_info=ctx_data.get("user_info", {}),
                network_info=ctx_data.get("network_info", {}),
                threat_intel=ctx_data.get("threat_intel", {}),
                related_alerts=ctx_data.get("related_alerts", []),
                raw_logs=ctx_data.get("raw_logs", []),
            ),
            tags=body.tags,
            source_alert_id=body.source_alert_id,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    t0 = time.monotonic()
    try:
        result = await analyzer.analyze(alert, dry_run=body.dry_run)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}") from exc

    latency_ms = (time.monotonic() - t0) * 1000
    monitor.record_request(latency_ms)
    _cache_result(result)

    if _webhook_manager:
        from alerttriage.src.webhooks.events import make_alert_analyzed_event
        _webhook_manager.emit(make_alert_analyzed_event(result.client_id, result))

    return _result_to_response(result)


@app.get(
    "/results/{alert_id}",
    response_model=AnalysisResponse,
    summary="Retrieve a cached analysis result",
    tags=["Analysis"],
)
async def get_result(
    alert_id: str,
    _key: str = Depends(require_api_key),
) -> AnalysisResponse:
    """
    Retrieve a previously computed analysis result by its alert ID.

    Results are cached in-memory (up to 10 000 entries, LRU eviction).
    """
    result = _result_store.get(alert_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No result found for alert_id={alert_id!r}. "
                   "Results are held in memory and lost on restart.",
        )
    return _result_to_response(result)


# ---------------------------------------------------------------------------
# Feedback endpoints
# ---------------------------------------------------------------------------


@app.post(
    "/feedback",
    status_code=status.HTTP_201_CREATED,
    summary="Record analyst feedback for a triaged alert",
    tags=["Feedback"],
)
async def record_feedback(
    request: Request,
    body: FeedbackRequest,
    _key: str = Depends(require_api_key),
) -> dict[str, str]:
    """
    Persist an analyst's verdict for a previously triaged alert.

    Accepted ``analyst_verdict`` values:
    - ``true_positive``
    - ``false_positive``
    - ``escalated``
    - ``closed``

    After recording, the prompt-hint cache for this client is invalidated so
    the learning engine will rebuild hints on the next analysis call.
    """
    limiter.check(request_key(request))

    from alerttriage.src.core.alert_models import FeedbackRecord

    metadata = {**body.metadata, "rule_name": body.rule_name}
    record = FeedbackRecord(
        alert_id=body.alert_id,
        analysis_id=body.analysis_id,
        client_id=body.client_id,
        analyst_id=body.analyst_id,
        analyst_verdict=body.analyst_verdict,
        analyst_notes=body.analyst_notes,
        ai_verdict_was_correct=body.ai_verdict_was_correct,
        metadata=metadata,
    )

    _feedback_system(body.client_id).record(record)
    _get_analyzer().invalidate_prompt_cache(body.client_id)

    if _webhook_manager:
        from alerttriage.src.webhooks.events import make_feedback_event
        _webhook_manager.emit(make_feedback_event(body.client_id, record))

    log.info("feedback_recorded", alert_id=body.alert_id, client=body.client_id)
    return {"status": "recorded", "id": record.id}


# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------


@app.get(
    "/stats/{client_id}",
    response_model=StatsResponse,
    summary="Accuracy statistics for a client",
    tags=["Analytics"],
)
async def get_stats(
    client_id: str,
    _key: str = Depends(require_api_key),
) -> StatsResponse:
    """
    Return overall accuracy and per-rule statistics derived from analyst feedback.

    Requires at least one ``POST /feedback`` call for this client before
    meaningful numbers are returned.
    """
    fs = _feedback_system(client_id)
    accuracy = fs.accuracy()
    rule_stats = fs.rule_stats()

    rules = [
        RuleStatsItem(
            rule_name=r.rule_name,
            total=r.total,
            correct=r.correct,
            false_positives=r.false_positives,
            fp_rate=r.false_positives / r.total if r.total else 0.0,
            accuracy=r.correct / r.total if r.total else 0.0,
            latest_ts=r.latest_ts,
        )
        for r in rule_stats
    ]

    return StatsResponse(
        client_id=client_id,
        overall_accuracy=accuracy,
        total_analyses=sum(r.total for r in rule_stats),
        rule_stats=sorted(rules, key=lambda x: x.fp_rate, reverse=True),
    )


@app.get(
    "/hints",
    response_model=HintsResponse,
    summary="Learned prompt hints for a client",
    tags=["Analytics"],
)
async def get_hints(
    client_id: str = Query(..., description="Client ID to fetch hints for"),
    _key: str = Depends(require_api_key),
) -> HintsResponse:
    """
    Return the natural-language hints currently derived from analyst feedback
    for this client.

    Hints are regenerated whenever feedback is recorded (cache invalidation).
    An empty list means no rules have crossed the false-positive threshold yet.
    """
    analyzer = _get_analyzer()
    config = analyzer.config

    from alerttriage.src.feedback.learning_engine import LearningEngine

    fs = _feedback_system(client_id)
    learning_cfg = config.get_learning(client_id)
    engine = LearningEngine(
        fs,
        fp_rate_threshold=learning_cfg.fp_rate_threshold,
        min_sample_size=learning_cfg.min_sample_size,
    )
    raw = engine.generate_prompt_hints(max_hints=learning_cfg.max_hints)
    hints = [line[2:] for line in raw.splitlines() if line.startswith("- ")]

    return HintsResponse(
        client_id=client_id,
        hints=hints,
        generated_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Context endpoint
# ---------------------------------------------------------------------------


@app.post(
    "/context",
    status_code=status.HTTP_201_CREATED,
    summary="Store environment context for a client",
    tags=["Configuration"],
)
async def add_context(
    body: ContextRequest,
    _key: str = Depends(require_api_key),
) -> dict[str, str]:
    """
    Store environment context (network ranges, known services, threat intel)
    that will be automatically injected into future ``POST /analyze`` calls
    for this client.

    Submitting the same ``context_type`` again replaces the previous entry.

    Supported ``context_type`` values (others are stored but not auto-injected):
    - ``network_ranges`` → injected as ``context.network_info``
    - ``known_services`` → injected as ``context.host_info``
    - ``threat_intel``   → injected as ``context.threat_intel``
    """
    store = _context_store.setdefault(body.client_id, [])
    _context_store[body.client_id] = [
        c for c in store if c.get("context_type") != body.context_type
    ]
    _context_store[body.client_id].append(body.model_dump())

    log.info("context_stored", client=body.client_id, context_type=body.context_type)
    return {
        "status": "stored",
        "client_id": body.client_id,
        "context_type": body.context_type,
    }


# ---------------------------------------------------------------------------
# Health / observability endpoints
# ---------------------------------------------------------------------------


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="System health check",
    tags=["Operations"],
)
async def health_check() -> HealthResponse:
    """
    Returns health status for all system components.

    This endpoint is intentionally **unauthenticated** so load balancers and
    container orchestrators can probe it without API keys.
    """
    monitor = _get_monitor()
    accuracy_by_client = {cid: fs.accuracy() for cid, fs in _feedback_systems.items()}
    health = await monitor.check_health(accuracy_by_client=accuracy_by_client)

    return HealthResponse(
        status=health.status,
        version=VERSION,
        uptime_seconds=round(health.uptime_seconds, 2),
        components=[
            HealthComponentStatus(
                name=c.name,
                healthy=c.healthy,
                latency_ms=c.latency_ms,
                details=c.details,
            )
            for c in health.components
        ],
        timestamp=health.timestamp,
    )


@app.get(
    "/dashboard/health",
    summary="Full health and performance dashboard",
    tags=["Operations"],
)
async def health_dashboard(
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """
    Returns comprehensive health, performance, accuracy, and cost metrics.

    Suitable for feeding a Grafana JSON datasource or an ops dashboard.
    """
    monitor = _get_monitor()
    analyzer = _get_analyzer()

    accuracy_by_client = {cid: fs.accuracy() for cid, fs in _feedback_systems.items()}
    cost_by_client = {
        cid: analyzer.cost_controller.get_spend(cid)
        for cid in analyzer.config.list_clients()
    }

    return await monitor.get_dashboard(
        accuracy_by_client=accuracy_by_client,
        cost_by_client=cost_by_client,
    )


@app.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="Prometheus metrics",
    tags=["Operations"],
)
async def prometheus_metrics() -> str:
    """
    Exports metrics in Prometheus text exposition format.

    Configure your Prometheus scrape target to hit this endpoint.
    No authentication required (standard practice for metrics endpoints).
    """
    monitor = _get_monitor()
    analyzer = _get_analyzer()

    accuracy_by_client = {cid: fs.accuracy() for cid, fs in _feedback_systems.items()}
    cost_by_client = {
        cid: analyzer.cost_controller.get_spend(cid)
        for cid in analyzer.config.list_clients()
    }

    return monitor.prometheus_metrics(
        accuracy_by_client=accuracy_by_client,
        cost_by_client=cost_by_client,
    )


__all__ = ["app"]
