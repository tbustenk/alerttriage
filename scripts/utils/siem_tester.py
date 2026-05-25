"""Lightweight SIEM connectivity probes.

Uses only the stdlib (socket + urllib) so no extra dependencies are needed
during onboarding before the full package is installed.
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass
class ProbeResult:
    ok: bool
    message: str
    latency_ms: int = 0
    detail: str = ""
    # None = warning (reachable but auth unknown), True/False = definitive
    auth_ok: bool | None = None


def test_splunk(host: str, port: int, *, timeout: int = 5) -> ProbeResult:
    """TCP probe to the Splunk REST port."""
    result = _tcp_probe(host, port, label="Splunk", timeout=timeout)
    if result.ok:
        result.detail = (
            "TCP handshake succeeded. Full auth requires the API token; "
            "set it via the ALERTTRIAGE_<CLIENT_ID>_API_KEY env var."
        )
    return result


def test_elk(host: str, port: int, *, timeout: int = 5) -> ProbeResult:
    """TCP probe to Elasticsearch + optional HTTP /_cluster/health."""
    result = _tcp_probe(host, port, label="Elasticsearch", timeout=timeout)
    if not result.ok:
        return result

    # Try a quick HTTP GET for a richer response
    t0 = time.monotonic()
    try:
        url = f"http://{host}:{port}/_cluster/health?timeout=3s"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "AlertTriage/2.0-onboarding")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            import json
            body = json.loads(resp.read())
            status = body.get("status", "unknown")
            latency = int((time.monotonic() - t0) * 1000)
            result.message = f"Cluster health: {status}"
            result.latency_ms = latency
            result.auth_ok = True
    except urllib.error.HTTPError as e:
        if e.code == 401:
            result.message = "Reachable — authentication required (set API key)"
            result.auth_ok = None
        # Any other HTTP response still means the server is up
    except Exception:
        pass  # TCP probe already confirmed reachability

    return result


def test_webhook(url: str, *, timeout: int = 5) -> ProbeResult:
    """HTTP OPTIONS probe to verify the webhook endpoint is reachable."""
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, method="OPTIONS")
        req.add_header("User-Agent", "AlertTriage/2.0-onboarding")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency = int((time.monotonic() - t0) * 1000)
            return ProbeResult(
                ok=True,
                message=f"Reachable (HTTP {resp.status})",
                latency_ms=latency,
                auth_ok=True,
            )
    except urllib.error.HTTPError as e:
        latency = int((time.monotonic() - t0) * 1000)
        # 4xx/5xx still means the server replied — reachable
        note = "Authentication or method may be required in production."
        return ProbeResult(
            ok=True,
            message=f"Server responded (HTTP {e.code}) — {note}",
            latency_ms=latency,
            auth_ok=None,
        )
    except urllib.error.URLError as e:
        latency = int((time.monotonic() - t0) * 1000)
        return ProbeResult(
            ok=False,
            message=f"Cannot reach {url}",
            latency_ms=latency,
            detail=str(e.reason),
        )
    except Exception as e:
        return ProbeResult(
            ok=False,
            message=f"Unexpected error probing {url}",
            detail=str(e),
        )


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _tcp_probe(host: str, port: int, label: str, timeout: int) -> ProbeResult:
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency = int((time.monotonic() - t0) * 1000)
            return ProbeResult(
                ok=True,
                message=f"TCP connection to {host}:{port} succeeded",
                latency_ms=latency,
            )
    except socket.timeout:
        return ProbeResult(
            ok=False,
            message=f"Connection to {host}:{port} timed out after {timeout}s",
            detail="Check the host address, port, and any firewall rules between this machine and the SIEM.",
        )
    except ConnectionRefusedError:
        return ProbeResult(
            ok=False,
            message=f"Connection refused at {host}:{port}",
            detail=f"{label} may not be running, or the port number is wrong.",
        )
    except socket.gaierror as e:
        return ProbeResult(
            ok=False,
            message=f"Cannot resolve hostname '{host}'",
            detail=str(e),
        )
    except OSError as e:
        return ProbeResult(
            ok=False,
            message=f"Network error connecting to {host}:{port}",
            detail=str(e),
        )
