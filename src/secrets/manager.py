"""Secrets manager — unified secret resolution with pluggable backends.

Resolution priority (highest first):
1. In-process cache (avoids redundant lookups within one process lifetime)
2. Environment variable (always available, used in dev and Docker)
3. AWS Secrets Manager (production; needs ``boto3`` and IAM role)
4. HashiCorp Vault (alternative; needs ``hvac`` and ``VAULT_TOKEN``)
5. Default value supplied by caller

Usage::

    from alerttriage.src.secrets.manager import get_secret

    api_key = get_secret("ALERTTRIAGE_API_KEYS", default="")
"""

from __future__ import annotations

import json
import os
from typing import Any

from alerttriage.src.logger import get_logger

log = get_logger(__name__)

_cache: dict[str, str] = {}


def get_secret(name: str, *, default: str | None = None) -> str | None:
    """Retrieve a secret value from the highest-priority available source.

    Args:
        name:    Secret name / environment variable name.
        default: Fallback value when no source provides the secret.

    Returns:
        The secret string, or *default* if not found anywhere.
    """
    if name in _cache:
        return _cache[name]

    # 1. Environment variable
    val = os.environ.get(name)
    if val is not None:
        _cache[name] = val
        return val

    # 2. AWS Secrets Manager
    aws_secret_id = os.environ.get("ALERTTRIAGE_AWS_SECRET_ID")
    if aws_secret_id:
        val = _get_from_aws(aws_secret_id, name)
        if val is not None:
            _cache[name] = val
            return val

    # 3. HashiCorp Vault
    if os.environ.get("VAULT_ADDR"):
        val = _get_from_vault(name)
        if val is not None:
            _cache[name] = val
            return val

    log.debug("secret_not_found", name=name, using_default=default is not None)
    return default


def clear_cache() -> None:
    """Flush the in-process secret cache.

    Call when secrets may have been rotated and you need fresh values.
    """
    _cache.clear()


def prefetch(names: list[str]) -> None:
    """Eagerly fetch multiple secrets in one round-trip (AWS SM only).

    Useful at startup to reduce per-request latency for frequently used secrets.
    """
    aws_secret_id = os.environ.get("ALERTTRIAGE_AWS_SECRET_ID")
    if not aws_secret_id:
        return
    try:
        import boto3

        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=aws_secret_id)
        data: dict[str, Any] = json.loads(response.get("SecretString", "{}"))
        for name in names:
            if name in data and name not in _cache:
                _cache[name] = str(data[name])
    except Exception as exc:  # noqa: BLE001
        log.warning("secret_prefetch_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------


def _get_from_aws(secret_id: str, key: str) -> str | None:
    try:
        import boto3

        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=secret_id)
        data: dict[str, Any] = json.loads(response.get("SecretString", "{}"))
        return str(data[key]) if key in data else None
    except ImportError:
        log.debug("boto3_not_installed", detail="Install boto3 for AWS Secrets Manager support")
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("aws_secret_fetch_failed", secret_id=secret_id, error=str(exc))
        return None


def _get_from_vault(key: str) -> str | None:
    try:
        import hvac

        vault_addr = os.environ["VAULT_ADDR"]
        token = os.environ.get("VAULT_TOKEN")
        vault_path = os.environ.get("VAULT_SECRET_PATH", "alerttriage/config")

        client = hvac.Client(url=vault_addr, token=token)
        secret = client.secrets.kv.v2.read_secret_version(path=vault_path)
        data: dict[str, Any] = secret["data"]["data"]
        return str(data[key]) if key in data else None
    except ImportError:
        log.debug("hvac_not_installed", detail="Install hvac for HashiCorp Vault support")
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("vault_secret_fetch_failed", key=key, error=str(exc))
        return None
