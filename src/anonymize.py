"""PII anonymization layer — strips identifying data before AI submission."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from typing import Any

from alerttriage.src.core.alert_models import Alert, AlertContext
from alerttriage.src.logger import get_logger

log = get_logger(__name__)

# Patterns that should never reach the AI model.
_PATTERNS = {
    "credit_card": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "ssn":         re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "email":       re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
    "api_key":     re.compile(r"\b(?:sk|pk|api)[-_][A-Za-z0-9]{20,}\b"),
}


class Anonymizer:
    """
    Replaces PII in alert fields with stable pseudonyms so that:
      - The AI cannot see raw sensitive values.
      - Correlations between alerts (same IP → same token) are preserved.
      - Results can be de-anonymized for display if needed.
    """

    def __init__(self, config: Any) -> None:
        self._salt: str = getattr(config, "anonymize_salt", "alerttriage-default-salt")

    def anonymize(self, alert: Alert) -> tuple[Alert, dict[str, str]]:
        """
        Returns (anonymized_alert, reverse_map).

        reverse_map: token → original_value (kept in memory only; never persisted).
        """
        reverse_map: dict[str, str] = {}

        def _redact(value: str) -> str:
            token = f"<REDACTED:{self._token(value)}>"
            reverse_map[token] = value
            return token

        def _anonymize_ip(ip: str) -> str:
            try:
                addr = ipaddress.ip_address(ip)
                if isinstance(addr, ipaddress.IPv4Address):
                    parts = ip.split(".")
                    token = f"<IP:{self._token(ip)}>"
                    reverse_map[token] = ip
                    return token
            except ValueError:
                pass
            return ip

        def _scrub(text: str) -> str:
            for name, pattern in _PATTERNS.items():
                def _replace(m: re.Match) -> str:
                    original = m.group()
                    t = f"<{name.upper()}:{self._token(original)}>"
                    reverse_map[t] = original
                    return t
                text = pattern.sub(_replace, text)
            return text

        anon_payload = {k: _scrub(str(v)) for k, v in alert.raw_payload.items()}

        anon_host = dict(alert.context.host_info)
        if "ip" in anon_host:
            anon_host["ip"] = _anonymize_ip(str(anon_host["ip"]))
        if "hostname" in anon_host:
            h = str(anon_host["hostname"])
            anon_host["hostname"] = f"<HOST:{self._token(h)}>"
            reverse_map[anon_host["hostname"]] = h

        anon_user = dict(alert.context.user_info)
        if "username" in anon_user:
            u = str(anon_user["username"])
            anon_user["username"] = f"<USER:{self._token(u)}>"
            reverse_map[anon_user["username"]] = u

        anon_alert = alert.model_copy(
            update={
                "raw_payload": anon_payload,
                "description": _scrub(alert.description),
                "context": AlertContext(
                    **{
                        **alert.context.model_dump(),
                        "host_info": anon_host,
                        "user_info": anon_user,
                    }
                ),
            }
        )

        log.debug("anonymized", alert_id=alert.id, tokens=len(reverse_map))
        return anon_alert, reverse_map

    def _token(self, value: str) -> str:
        return hashlib.sha256(f"{self._salt}:{value}".encode()).hexdigest()[:8]
