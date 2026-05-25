"""PII anonymisation layer — strips identifying data before AI submission.

Pseudonyms are stable within a salt so correlations between alerts survive
(``10.0.0.5`` always tokenises to the same ``<IP:abcd1234>``). The reverse
map lives only in memory; it is never persisted and never sent to the AI.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from typing import Any

from alerttriage.src.core.alert_models import Alert, AlertContext
from alerttriage.src.logger import get_logger

log = get_logger(__name__)

# Patterns that should never reach the AI model.
_PATTERNS: dict[str, re.Pattern[str]] = {
    "credit_card": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "email": re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
    "api_key": re.compile(r"\b(?:sk|pk|api)[-_][A-Za-z0-9]{20,}\b"),
}


class Anonymizer:
    """Replaces PII in alert fields with stable, deterministic pseudonyms."""

    def __init__(self, config: Any) -> None:
        """Read ``anonymize_salt`` from config (falls back to a fixed default)."""
        self._salt: str = getattr(config, "anonymize_salt", "alerttriage-default-salt")

    def anonymize(self, alert: Alert) -> tuple[Alert, dict[str, str]]:
        """Return an anonymised copy of ``alert`` plus the in-memory reverse map.

        The reverse map (``token → original_value``) lets callers de-anonymise
        results for display. It must never be persisted or sent to the AI.
        """
        reverse_map: dict[str, str] = {}

        def _anonymize_ip(ip: str) -> str:
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                return ip
            if not isinstance(addr, ipaddress.IPv4Address | ipaddress.IPv6Address):
                return ip
            token = f"<IP:{self._token(ip)}>"
            reverse_map[token] = ip
            return token

        def _scrub(text: str) -> str:
            for name, pattern in _PATTERNS.items():
                # Bind ``name`` via default arg so the closure captures
                # the value at each iteration rather than the loop variable.
                def _replace(m: re.Match, _name: str = name) -> str:
                    original = m.group()
                    token = f"<{_name.upper()}:{self._token(original)}>"
                    reverse_map[token] = original
                    return token

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
        """Return the first 8 hex chars of ``sha256(salt + value)``."""
        return hashlib.sha256(f"{self._salt}:{value}".encode()).hexdigest()[:8]
