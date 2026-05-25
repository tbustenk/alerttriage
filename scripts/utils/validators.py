"""Input validation helpers for the onboarding wizard.

Every function returns ``(ok: bool, error_message: str)``.
An empty error string means the value is valid.
"""

from __future__ import annotations

import ipaddress
import re


def validate_slug(value: str) -> tuple[bool, str]:
    """Client ID: lowercase alphanumeric + hyphens, 2–64 chars."""
    if not value:
        return False, "Client ID cannot be empty."
    if len(value) < 2:
        return False, "Client ID must be at least 2 characters."
    if len(value) > 64:
        return False, "Client ID must be 64 characters or fewer."
    if not re.match(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$", value):
        return False, (
            f"'{value}' contains invalid characters. "
            "Use only lowercase letters, digits, and hyphens."
        )
    if "--" in value:
        return False, "Client ID cannot contain consecutive hyphens."
    return True, ""


def validate_cidr(value: str) -> tuple[bool, str]:
    """CIDR network notation, e.g. 10.0.0.0/8 or 192.168.1.0/24."""
    value = value.strip()
    try:
        ipaddress.ip_network(value, strict=False)
        return True, ""
    except ValueError:
        return False, (
            f"'{value}' is not valid CIDR notation. "
            "Expected format: 10.0.0.0/8 or 192.168.1.100/32"
        )


def validate_ip_or_host(value: str) -> tuple[bool, str]:
    """IP address or resolvable hostname (permissive — checked at runtime)."""
    value = value.strip()
    if not value:
        return False, "Cannot be empty."
    try:
        ipaddress.ip_address(value)
        return True, ""
    except ValueError:
        pass
    # Hostnames: labels separated by dots, each ≤63 chars, total ≤253
    if len(value) > 253:
        return False, f"Hostname too long ({len(value)} chars, max 253)."
    labels = value.rstrip(".").split(".")
    label_re = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$")
    for label in labels:
        if not label_re.match(label):
            return False, (
                f"'{value}' doesn't look like a valid IP address or hostname. "
                "(Hint: don't include the port here.)"
            )
    return True, ""


def validate_port(value: str) -> tuple[bool, str]:
    """TCP port number, 1–65535."""
    try:
        port = int(value)
    except ValueError:
        return False, f"'{value}' is not a number."
    if not 1 <= port <= 65535:
        return False, f"Port must be between 1 and 65535 (got {port})."
    return True, ""


def validate_email(value: str) -> tuple[bool, str]:
    """Minimal RFC-5322 email check."""
    if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
        return True, ""
    return False, f"'{value}' doesn't look like a valid email address."


def validate_url(value: str) -> tuple[bool, str]:
    """Must start with http:// or https://."""
    if re.match(r"^https?://.{3,}", value.strip()):
        return True, ""
    return False, "URL must start with http:// or https://"


def validate_nonempty(value: str) -> tuple[bool, str]:
    """Simple non-blank check for free-text fields."""
    if value.strip():
        return True, ""
    return False, "This field cannot be blank."


def parse_cost(value: str) -> tuple[bool, float | None]:
    """Parse an optional cost limit. Returns (ok, float-or-None)."""
    stripped = value.strip()
    if not stripped:
        return True, None
    stripped = stripped.lstrip("$")
    try:
        v = float(stripped)
        if v > 0:
            return True, v
        return False, None
    except ValueError:
        return False, None
