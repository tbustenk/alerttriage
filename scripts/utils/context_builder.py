"""Guided infrastructure-context wizard.

Prompts the user for environment details that help the AI reduce false
positives (office subnets, scanner IPs, maintenance windows, etc.).
Results are returned as a structured dict AND written to the client's
SQLite ``environment_context`` table so the dashboard displays them.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.validators import validate_cidr, validate_ip_or_host, validate_nonempty

AUTH_SYSTEMS = ["Okta", "Azure AD / Entra ID", "Google Workspace", "LDAP / Active Directory", "Duo", "Ping Identity", "Other"]


def build_context(ui: Any, data_dir: Path, client_id: str) -> dict[str, Any]:
    """Walk the user through infrastructure context collection.

    Writes results to the SQLite DB so they are immediately visible in the
    dashboard, and returns the same data as a Python dict for inclusion in
    the client YAML.

    Args:
        ui: The wizard's ``UI`` instance (passed in to avoid circular imports).
        data_dir: Project-level data directory (``alerttriage/data/``).
        client_id: The client being onboarded.
    """
    ctx: dict[str, Any] = {}
    db_rows: list[tuple[str, str, str]] = []  # (key, value, notes)

    # ── Office / internal subnets ──────────────────────────────────────────
    ui.section("Office & Internal Network Ranges")
    ui.hint(
        "IP ranges that belong to your organisation.",
        "Alerts from these IPs are treated as 'trusted internal' by the AI,",
        "reducing false positives for lateral movement and VPN logon alerts.",
    )
    subnets = ui.collect_list(
        "Office subnet",
        example="10.0.0.0/8  or  192.168.0.0/16",
        validator=validate_cidr,
    )
    ctx["office_subnets"] = subnets
    for s in subnets:
        db_rows.append(("office_subnet", s, "Internal network range"))

    # ── Vulnerability scanners ─────────────────────────────────────────────
    ui.section("Vulnerability Scanners & Security Tools")
    ui.hint(
        "IPs or hostnames of your scanners (Nessus, Qualys, Rapid7, etc.).",
        "Port-scan and brute-force alerts from these sources are expected noise.",
    )
    scanners = ui.collect_list(
        "Scanner IP or hostname",
        example="192.168.1.50  or  nessus.internal",
        validator=validate_ip_or_host,
    )
    ctx["scanner_ips"] = scanners
    for s in scanners:
        db_rows.append(("scanner_ip", s, "Vulnerability scanner — alerts expected"))

    if scanners:
        tool = ui.prompt(
            "Scanner tool name(s)",
            default="Nessus",
            hint="e.g. Nessus / Qualys / Rapid7 InsightVM",
        )
        if tool:
            ctx["scanner_tool"] = tool
            db_rows.append(("scanner_tool", tool, "Security scanning product in use"))

    # ── Maintenance windows ────────────────────────────────────────────────
    ui.section("Maintenance Windows")
    ui.hint(
        "Regular windows when patching, restarts, and admin scripts run.",
        "The AI treats elevated alert volume during these windows as expected.",
        "Format: day time timezone — e.g.  Saturday 02:00-04:00 UTC",
    )
    windows = ui.collect_list(
        "Maintenance window",
        example="Saturday 02:00-04:00 UTC — weekly patching",
        validator=validate_nonempty,
    )
    ctx["maintenance_windows"] = windows
    for w in windows:
        db_rows.append(("maintenance_window", w, "Elevated alerts expected"))

    # ── Jump servers / bastion hosts ───────────────────────────────────────
    ui.section("Jump Servers / Bastion Hosts")
    ui.hint(
        "SSH and RDP sessions from these IPs are expected and not attacks.",
    )
    bastions = ui.collect_list(
        "Bastion host IP or hostname",
        example="10.0.0.99  or  bastion.internal",
        validator=validate_ip_or_host,
    )
    ctx["bastion_hosts"] = bastions
    for b in bastions:
        db_rows.append(("bastion_host", b, "Authorised jump server — SSH/RDP expected"))

    # ── Authentication systems ─────────────────────────────────────────────
    ui.section("Authentication & Identity Systems")
    ui.hint(
        "Knowing your auth stack helps distinguish real credential attacks",
        "from expected SSO token refreshes, MFA prompts, and password resets.",
    )
    auth = ui.multichoice("Which auth systems does your organisation use?", AUTH_SYSTEMS)
    ctx["auth_systems"] = auth
    if auth:
        db_rows.append(("auth_systems", ", ".join(auth), "Identity providers in use"))
    if "Other" in auth:
        other = ui.prompt("Specify the other auth system(s)", hint="e.g. CyberArk, BeyondTrust")
        if other:
            ctx["auth_other"] = other
            db_rows.append(("auth_other", other, ""))

    # ── Team info ──────────────────────────────────────────────────────────
    ui.section("SOC Team")
    team = ui.choice(
        "Approximate number of analysts",
        ["1-3", "4-10", "11-25", "26-50", "50+"],
        default="4-10",
    )
    ctx["team_size"] = team
    db_rows.append(("team_size", team, "Number of SOC analysts"))

    # ── Compliance / regulatory ────────────────────────────────────────────
    ui.section("Compliance Requirements")
    compliance = ui.multichoice(
        "Which frameworks apply? (used to tune alert severity weighting)",
        ["PCI-DSS", "HIPAA", "SOC 2", "ISO 27001", "GDPR", "NIST CSF", "FedRAMP", "None"],
    )
    compliance = [c for c in compliance if c != "None"]
    ctx["compliance"] = compliance
    if compliance:
        db_rows.append(("compliance", ", ".join(compliance), "Regulatory frameworks"))

    # ── Persist to SQLite ──────────────────────────────────────────────────
    _persist(data_dir, client_id, db_rows)

    return ctx


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _persist(data_dir: Path, client_id: str, rows: list[tuple[str, str, str]]) -> None:
    db_path = data_dir / client_id / "feedback.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY, alert_id TEXT, analysis_id TEXT, client_id TEXT,
            analyst_id TEXT, analyst_verdict TEXT, analyst_notes TEXT DEFAULT '',
            ai_correct INTEGER NOT NULL, rule_name TEXT NOT NULL DEFAULT 'unknown',
            timestamp TEXT NOT NULL, metadata TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_fb_client ON feedback(client_id);
        CREATE INDEX IF NOT EXISTS idx_fb_ts     ON feedback(client_id, timestamp DESC);
        CREATE TABLE IF NOT EXISTS environment_context (
            id TEXT PRIMARY KEY, key TEXT NOT NULL, value TEXT NOT NULL,
            notes TEXT DEFAULT '', created TEXT NOT NULL
        );
        PRAGMA user_version = 2;
    """)
    now = datetime.utcnow().isoformat()
    for key, value, notes in rows:
        conn.execute(
            "INSERT INTO environment_context (id, key, value, notes, created) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), key, value, notes, now),
        )
    conn.commit()
    conn.close()
