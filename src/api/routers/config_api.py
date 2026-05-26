"""Config management API router — /config/..."""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from alerttriage.src.api.auth import require_api_key

router = APIRouter(prefix="/config", tags=["Configuration"])


def _versioning() -> Any:
    from alerttriage.src.api.app import _config_versioning  # type: ignore[attr-defined]
    if _config_versioning is None:
        raise HTTPException(status_code=503, detail="Config versioning not initialised")
    return _config_versioning


def _flags() -> Any:
    from alerttriage.src.api.app import _feature_flags  # type: ignore[attr-defined]
    if _feature_flags is None:
        raise HTTPException(status_code=503, detail="Feature flags not initialised")
    return _feature_flags


def _alert_types() -> Any:
    from alerttriage.src.api.app import _alert_type_settings  # type: ignore[attr-defined]
    if _alert_type_settings is None:
        raise HTTPException(status_code=503, detail="Alert type settings not initialised")
    return _alert_type_settings


def _get_config_dir() -> Any:
    from alerttriage.src.api.app import _CONFIG_DIR  # type: ignore[attr-defined]
    return _CONFIG_DIR


# ---------------------------------------------------------------------------
# Static single-segment routes must be defined BEFORE /{client_id} to avoid
# the parameterised catch-all shadowing them in FastAPI's route table.
# ---------------------------------------------------------------------------

@router.get("/clients", summary="List all configured client IDs")
async def list_clients(_key: str = Depends(require_api_key)) -> list[str]:
    from alerttriage.src.api.app import _analyzer  # type: ignore[attr-defined]
    if _analyzer is None:
        return []
    return _analyzer.config.list_clients()


# ---------------------------------------------------------------------------
# Client config
# ---------------------------------------------------------------------------

class ConfigUpdateRequest(BaseModel):
    config: dict[str, Any]
    comment: str = ""


@router.get("/{client_id}", summary="Get client configuration")
async def get_client_config(
    client_id: str,
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Return the current validated configuration for a client."""
    from alerttriage.src.api.app import _analyzer  # type: ignore[attr-defined]
    if _analyzer is None:
        raise HTTPException(status_code=503, detail="Analyzer not initialised")
    config = _analyzer.config
    try:
        return config.get_client(client_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/{client_id}", summary="Update client configuration")
async def update_client_config(
    client_id: str,
    body: ConfigUpdateRequest,
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """
    Update a client's YAML config.  The payload must be a valid client config dict.

    A snapshot is automatically saved before applying the change so you can
    roll back with ``POST /config/{client_id}/rollback/{version_id}``.
    """
    from alerttriage.config.config_manager import ClientConfig, ConfigError

    # Validate with Pydantic before touching disk
    try:
        validated = ClientConfig(**body.config)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid config: {exc}")

    # Snapshot current state before overwriting
    config_dir = _get_config_dir()
    client_cfg_path = config_dir / "client_configs" / f"{client_id}.yaml"
    if client_cfg_path.exists():
        current = yaml.safe_load(client_cfg_path.read_text(encoding="utf-8")) or {}
        _versioning().snapshot(client_id, current, comment=f"Auto-snapshot before update: {body.comment}")

    # Write new config
    client_cfg_path.parent.mkdir(parents=True, exist_ok=True)
    client_cfg_path.write_text(
        yaml.safe_dump(body.config, default_flow_style=False),
        encoding="utf-8",
    )

    # Invalidate cache
    from alerttriage.src.api.app import _analyzer  # type: ignore[attr-defined]
    if _analyzer and hasattr(_analyzer.config, "_client_cache"):
        _analyzer.config._client_cache.pop(client_id, None)

    return {"status": "updated", "client_id": client_id}


# ---------------------------------------------------------------------------
# Config versioning
# ---------------------------------------------------------------------------

@router.get("/{client_id}/versions", summary="List config version history")
async def list_versions(
    client_id: str,
    _key: str = Depends(require_api_key),
) -> list[dict[str, Any]]:
    """Return all config snapshots for a client, newest first."""
    versions = _versioning().history(client_id)
    return [
        {
            "version_id": v.version_id,
            "created_at": v.created_at,
            "comment": v.comment,
            "is_current": v.is_current,
        }
        for v in versions
    ]


@router.get("/{client_id}/versions/{version_id}", summary="Get a specific config snapshot")
async def get_version(
    client_id: str,
    version_id: str,
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    try:
        return _versioning().get_snapshot(client_id, version_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post(
    "/{client_id}/rollback/{version_id}",
    summary="Roll back to a previous config version",
)
async def rollback_config(
    client_id: str,
    version_id: str,
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Restore a previous config snapshot as the active config."""
    try:
        config = _versioning().rollback(client_id, version_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # Invalidate cache
    from alerttriage.src.api.app import _analyzer  # type: ignore[attr-defined]
    if _analyzer and hasattr(_analyzer.config, "_client_cache"):
        _analyzer.config._client_cache.pop(client_id, None)

    return {"status": "rolled_back", "client_id": client_id, "version_id": version_id}


@router.post("/{client_id}/snapshot", status_code=201, summary="Manually create a config snapshot")
async def create_snapshot(
    client_id: str,
    comment: str = "",
    _key: str = Depends(require_api_key),
) -> dict[str, str]:
    from alerttriage.src.api.app import _analyzer  # type: ignore[attr-defined]
    if _analyzer is None:
        raise HTTPException(status_code=503, detail="Analyzer not initialised")
    config = _analyzer.config.get_client(client_id)
    version_id = _versioning().snapshot(client_id, config, comment=comment)
    return {"status": "created", "version_id": version_id}


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

@router.get("/features/global", summary="List all feature flags with global defaults")
async def list_global_flags(_key: str = Depends(require_api_key)) -> dict[str, Any]:
    return _flags().list_flags()


@router.get("/features/{client_id}", summary="List effective feature flags for a client")
async def list_client_flags(
    client_id: str,
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    return _flags().list_flags(client_id=client_id)


@router.put("/features/{client_id}", summary="Update feature flags for a client")
async def update_client_flags(
    client_id: str,
    flags: dict[str, Any],
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """Update per-client feature flag overrides in the client config YAML."""
    config_dir = _get_config_dir()
    path = config_dir / "client_configs" / f"{client_id}.yaml"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No config for client '{client_id}'")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw["features"] = flags
    path.write_text(yaml.safe_dump(raw, default_flow_style=False), encoding="utf-8")
    return {"status": "updated", "client_id": client_id, "features": flags}


# ---------------------------------------------------------------------------
# Alert-type settings
# ---------------------------------------------------------------------------

@router.get("/alert-types/all", summary="List all configured alert-type rules")
async def list_alert_types(_key: str = Depends(require_api_key)) -> list[str]:
    return _alert_types().list_rules()


@router.get("/alert-types/{rule_name}", summary="Get settings for an alert type")
async def get_alert_type(
    rule_name: str,
    client_id: str | None = None,
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    return _alert_types().get(rule_name, client_id=client_id)


@router.put("/alert-types/{rule_name}", summary="Update alert-type settings")
async def update_alert_type(
    rule_name: str,
    settings: dict[str, Any],
    client_id: str | None = None,
    _key: str = Depends(require_api_key),
) -> dict[str, Any]:
    """
    Update per-alert-type settings.

    When ``client_id`` is provided, the change is stored in the client config
    YAML and only affects that client.  Without ``client_id``, the global
    ``alert_types.yaml`` is updated.
    """
    _alert_types().update_rule(rule_name, settings, client_id=client_id)
    return {"status": "updated", "rule": rule_name, "client_id": client_id}


# ---------------------------------------------------------------------------
# Hot-reload
# ---------------------------------------------------------------------------

@router.post("/reload", summary="Trigger hot-reload of all config files")
async def trigger_reload(_key: str = Depends(require_api_key)) -> dict[str, str]:
    """Force a reload of all config files without restarting the API."""
    from alerttriage.src.api.app import _analyzer, _feature_flags, _alert_type_settings  # type: ignore[attr-defined]

    if _analyzer and hasattr(_analyzer.config, "_client_cache"):
        _analyzer.config._client_cache.clear()
    if _feature_flags:
        _feature_flags.reload()
    if _alert_type_settings:
        _alert_type_settings.reload()

    return {"status": "reloaded"}
