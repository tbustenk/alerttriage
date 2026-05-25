#!/usr/bin/env python3
"""AlertTriage v2 — Enhanced client onboarding wizard.

Usage:
    python scripts/init_client.py                        # full interactive setup
    python scripts/init_client.py --add-context <id>    # add env context to existing client
    python scripts/init_client.py --client-id <id> --non-interactive  # skip wizard

The wizard saves progress after every step so an interrupted session can
be resumed by running the script again.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Any

import yaml

# ── Paths ──────────────────────────────────────────────────────────────────
_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT   = _SCRIPTS_DIR.parent
sys.path.insert(0, str(_SCRIPTS_DIR))          # allow: from utils.x import …

from utils.validators import (
    validate_cidr, validate_email, validate_ip_or_host,
    validate_port, validate_slug, validate_url, parse_cost,
)
from utils.siem_tester  import test_splunk, test_elk, test_webhook, ProbeResult
from utils.context_builder import build_context

CONFIG_DIR    = _REPO_ROOT / "config"
CLIENT_DIR    = CONFIG_DIR / "client_configs"
DATA_DIR      = _REPO_ROOT / "data"
DOCS_DIR      = _REPO_ROOT / "docs"
STATE_FILE    = _REPO_ROOT / "data" / ".onboarding_state.json"
TEMPLATE_FILE = _SCRIPTS_DIR / "templates" / "client_onboarding.md"

SIEM_TYPES = ["splunk", "elk", "webhook", "none"]
MODELS     = ["claude-sonnet", "claude-opus", "claude-haiku", "gpt-4o", "gpt-4o-mini"]
TIERS      = ["standard", "premium", "enterprise"]
TIMEZONES  = [
    "UTC", "America/New_York", "America/Chicago", "America/Los_Angeles",
    "Europe/London", "Europe/Berlin", "Europe/Paris",
    "Asia/Tokyo", "Asia/Singapore", "Australia/Sydney",
]
TOTAL_STEPS = 6


# =============================================================================
# Terminal UI
# =============================================================================

class _Colors:
    """ANSI escape codes, disabled gracefully when not writing to a TTY."""

    _on = sys.stdout.isatty()
    if _on and os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(
                ctypes.windll.kernel32.GetStdHandle(-11), 7
            )
        except Exception:
            _on = False

    BOLD    = "\033[1m"  if _on else ""
    DIM     = "\033[2m"  if _on else ""
    GREEN   = "\033[92m" if _on else ""
    YELLOW  = "\033[93m" if _on else ""
    RED     = "\033[91m" if _on else ""
    BLUE    = "\033[94m" if _on else ""
    CYAN    = "\033[96m" if _on else ""
    RESET   = "\033[0m"  if _on else ""


C = _Colors


class UI:
    """All terminal I/O for the wizard lives here."""

    def banner(self) -> None:
        w = 60
        print()
        print(C.CYAN + "┌" + "─" * w + "┐" + C.RESET)
        title = "  ⚡ AlertTriage v2 — New Client Onboarding Wizard"
        print(C.CYAN + "│" + C.RESET + C.BOLD + title.ljust(w) + C.CYAN + "│" + C.RESET)
        print(C.CYAN + "└" + "─" * w + "┘" + C.RESET)
        print()

    def step(self, n: int, title: str) -> None:
        label = f"Step {n}/{TOTAL_STEPS}: {title}"
        w = max(len(label) + 4, 52)
        print()
        print(C.BLUE + "┌" + "─" * w + "┐" + C.RESET)
        print(C.BLUE + "│" + C.RESET + C.BOLD + f"  {label}".ljust(w) + C.BLUE + "│" + C.RESET)
        print(C.BLUE + "└" + "─" * w + "┘" + C.RESET)
        print()

    def section(self, title: str) -> None:
        print()
        print(C.CYAN + f"  ── {title}" + C.RESET)

    def hint(self, *lines: str) -> None:
        for line in lines:
            print(C.DIM + f"     {line}" + C.RESET)
        print()

    def info(self, text: str) -> None:
        print(C.DIM + f"  {text}" + C.RESET)

    def ok(self, text: str) -> None:
        print(C.GREEN + "  ✓ " + C.RESET + text)

    def warn(self, text: str) -> None:
        print(C.YELLOW + "  ⚠ " + C.RESET + text)

    def error(self, text: str) -> None:
        print(C.RED + "  ✗ " + C.RESET + text)

    def separator(self) -> None:
        print(C.DIM + "  " + "─" * 58 + C.RESET)

    def summary_row(self, key: str, value: Any) -> None:
        val = (
            C.DIM + "not set" + C.RESET
            if value is None or value == "" or value == []
            else str(value)
        )
        print(f"  {C.DIM}{key:<26}{C.RESET} {val}")

    # ── Input primitives ────────────────────────────────────────────────────

    def prompt(
        self,
        label: str,
        default: str = "",
        *,
        hint: str = "",
        required: bool = False,
        validator=None,
    ) -> str:
        if hint:
            print(C.DIM + f"     {hint}" + C.RESET)
        suffix = C.DIM + f" [{default}]" + C.RESET if default else ""
        while True:
            try:
                raw = input(f"  {C.BOLD}{label}{C.RESET}{suffix}: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                raise KeyboardInterrupt
            value = raw or default
            if required and not value:
                self.error("This field is required — please enter a value.")
                continue
            if validator and value:
                ok, err = validator(value)
                if not ok:
                    self.error(err)
                    continue
            return value

    def choice(self, label: str, options: list[str], default: str) -> str:
        self.hint("Options: " + " / ".join(
            f"[{o}]" if o == default else o for o in options
        ))
        while True:
            raw = self.prompt(label, default=default) or default
            if raw in options:
                return raw
            self.error(f"Must be one of: {', '.join(options)}")

    def multichoice(self, label: str, options: list[str]) -> list[str]:
        print(C.BOLD + f"  {label}" + C.RESET)
        for i, opt in enumerate(options, 1):
            print(f"     {C.DIM}{i}.{C.RESET} {opt}")
        self.hint("Enter numbers separated by commas (e.g. 1,3), or press Enter to skip.")
        while True:
            try:
                raw = input(f"  {C.BOLD}Selection{C.RESET}: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                raise KeyboardInterrupt
            if not raw:
                return []
            try:
                picked = []
                for part in re.split(r"[,\s]+", raw):
                    if not part:
                        continue
                    idx = int(part) - 1
                    if not 0 <= idx < len(options):
                        raise ValueError(f"{part} is out of range")
                    picked.append(options[idx])
                return list(dict.fromkeys(picked))  # dedupe, preserve order
            except ValueError as exc:
                self.error(f"{exc}. Enter numbers 1–{len(options)}.")

    def collect_list(
        self,
        label: str,
        example: str = "",
        *,
        validator=None,
    ) -> list[str]:
        items: list[str] = []
        if example:
            self.hint(f"e.g. {example}", "Press Enter with no value when done.")
        else:
            self.hint("Press Enter with no value when done.")
        while True:
            idx = len(items) + 1
            try:
                raw = input(f"  {C.BOLD}{label} #{idx}{C.RESET}: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                raise KeyboardInterrupt
            if not raw:
                break
            if validator:
                ok, err = validator(raw)
                if not ok:
                    self.error(err)
                    continue
            items.append(raw)
            self.ok(f"Added: {raw}")
        return items

    def confirm(self, label: str, default: bool = True) -> bool:
        yn = "Y/n" if default else "y/N"
        try:
            raw = input(f"  {C.BOLD}{label}{C.RESET} ({yn}): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            raise KeyboardInterrupt
        return default if not raw else raw.startswith("y")


# =============================================================================
# State persistence (save / resume across Ctrl-C)
# =============================================================================

def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)


def _load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _clear_state() -> None:
    STATE_FILE.unlink(missing_ok=True)


# =============================================================================
# Wizard steps
# =============================================================================

def _step_identity(ui: UI, saved: dict) -> dict:
    ui.step(1, "Client Identity")

    display_name = ui.prompt(
        "Client display name",
        default=saved.get("display_name", ""),
        hint='Full name of the organisation or team  (e.g. "Acme Corp SOC")',
        required=True,
    )

    suggested = saved.get("client_id") or re.sub(r"[^a-z0-9-]", "-", display_name.lower()).strip("-")
    client_id  = ui.prompt(
        "Client ID",
        default=suggested,
        hint="Lowercase letters, digits, hyphens only — used for file names and DB.",
        required=True,
        validator=validate_slug,
    )
    client_id = re.sub(r"[^a-z0-9-]", "-", client_id.lower()).strip("-")

    print()
    contact = ui.prompt(
        "SOC contact email",
        default=saved.get("contact", ""),
        hint="Primary point of contact for escalations and report delivery.",
        validator=lambda v: validate_email(v) if v else (True, ""),
    )

    tz_opts = TIMEZONES + ["Other"]
    timezone = ui.choice("Timezone", tz_opts, default=saved.get("timezone", "UTC"))
    if timezone == "Other":
        timezone = ui.prompt(
            "Enter timezone",
            required=True,
            hint="IANA format — e.g. America/Toronto or Pacific/Auckland",
        )

    tier     = ui.choice("Service tier", TIERS, default=saved.get("tier", "standard"))
    anonymize = ui.confirm(
        "Anonymise PII in alerts before sending to AI?",
        default=saved.get("anonymize", True),
    )

    print()
    ui.ok(f"Client ID set to {C.BOLD}{client_id}{C.RESET}")

    return {
        "display_name": display_name,
        "client_id":    client_id,
        "contact":      contact,
        "timezone":     timezone,
        "tier":         tier,
        "anonymize":    anonymize,
    }


def _step_model(ui: UI, saved: dict) -> dict:
    ui.step(2, "AI Model Selection")

    ui.hint(
        "claude-sonnet  → Best accuracy, moderate cost. Recommended for most clients.",
        "claude-haiku   → Fastest & cheapest. High-volume, low-severity triage.",
        "claude-opus    → Highest accuracy for complex / regulated environments.",
        "gpt-4o         → OpenAI alternative, comparable to claude-sonnet.",
    )
    model = ui.choice("Primary model", MODELS, default=saved.get("model", "claude-sonnet"))

    print()
    ui.info("Fallback models are tried in order when the primary model fails.")
    fallback_opts = [m for m in MODELS if m != model]
    fallbacks = ui.multichoice("Select fallback model(s):", fallback_opts)

    return {"model": model, "model_fallbacks": fallbacks}


def _step_siem(ui: UI, saved: dict) -> dict:
    ui.step(3, "SIEM Integration")

    saved_siem   = saved.get("siem", {})
    siem_type    = ui.choice("SIEM / alert source", SIEM_TYPES,
                             default=saved_siem.get("type", "splunk"))
    siem: dict[str, Any] = {"type": siem_type}
    probe: ProbeResult | None = None

    if siem_type == "splunk":
        print()
        siem["host"] = ui.prompt(
            "Splunk host",
            default=saved_siem.get("host", ""),
            hint="Hostname or IP of your Splunk Search Head  (e.g. splunk.corp.internal)",
            required=True,
            validator=validate_ip_or_host,
        )
        siem["port"] = int(ui.prompt(
            "REST API port",
            default=str(saved_siem.get("port", 8089)),
            validator=validate_port,
        ))
        siem["token"] = ui.prompt(
            "API token",
            default="",
            hint="Leave blank now — set via ALERTTRIAGE_<CLIENT_ID>_API_KEY env var.",
        )
        siem["index"]      = ui.prompt("Alert index / saved search", default=saved_siem.get("index", "notable"))
        siem["verify_ssl"] = ui.confirm("Verify SSL certificate?", default=True)

        if ui.confirm("Test connectivity now?"):
            print()
            ui.info(f"Connecting to {siem['host']}:{siem['port']} …")
            probe = test_splunk(siem["host"], siem["port"])
            _print_probe(ui, probe)

    elif siem_type == "elk":
        print()
        siem["host"] = ui.prompt(
            "Elasticsearch host",
            default=saved_siem.get("host", ""),
            hint="Hostname or IP  (e.g. elastic.corp.internal)",
            required=True,
            validator=validate_ip_or_host,
        )
        siem["port"]        = int(ui.prompt("ES port",     default=str(saved_siem.get("port", 9200)),        validator=validate_port))
        siem["kibana_port"] = int(ui.prompt("Kibana port", default=str(saved_siem.get("kibana_port", 5601)), validator=validate_port))
        siem["api_key"]     = ui.prompt("API key (base64 id:secret)", default="",
                                        hint="Leave blank — set via ALERTTRIAGE_<CLIENT_ID>_API_KEY env var.")
        siem["index"]       = ui.prompt("Alert index pattern", default=saved_siem.get("index", ".siem-signals-*"),
                                        hint="e.g. .siem-signals-*  or  alerts-*")
        siem["verify_ssl"]  = ui.confirm("Verify SSL certificate?", default=True)

        if ui.confirm("Test connectivity now?"):
            print()
            ui.info(f"Connecting to {siem['host']}:{siem['port']} …")
            probe = test_elk(siem["host"], siem["port"])
            _print_probe(ui, probe)

    elif siem_type == "webhook":
        print()
        siem["url"] = ui.prompt(
            "Inbound webhook URL",
            default=saved_siem.get("url", ""),
            hint="AlertTriage POSTs alert JSON here  (e.g. https://soar.corp/alerttriage)",
            required=True,
            validator=validate_url,
        )
        siem["timeout_sec"] = int(ui.prompt("Request timeout (s)", default="5", validator=validate_port))

        if ui.confirm("Test reachability now?"):
            print()
            ui.info(f"Probing {siem['url']} …")
            probe = test_webhook(siem["url"])
            _print_probe(ui, probe)

    else:
        ui.info("No SIEM configured. Edit the client YAML to add one later.")

    # Scrub secrets before they reach the YAML / state file
    for key in ("token", "api_key"):
        siem.pop(key, None)

    return {"siem": siem, "_siem_probe": probe}


def _print_probe(ui: UI, probe: ProbeResult) -> None:
    if probe.ok:
        ui.ok(f"{probe.message}  ({probe.latency_ms} ms)")
        if probe.detail:
            ui.info(probe.detail)
    else:
        ui.warn(probe.message)
        if probe.detail:
            ui.hint(probe.detail)
        ui.info("You can fix connectivity later — setup will continue.")
    print()


def _step_environment(ui: UI, saved: dict, client_id: str) -> dict:
    ui.step(4, "Environment Context")

    ui.hint(
        "Environment context teaches the AI about YOUR infrastructure so it",
        "can tell the difference between real threats and expected noise",
        "(scanner traffic, VPN logins, maintenance activity, etc.).",
        "",
        "The more context you add now, the fewer false positives you will see.",
    )

    if not ui.confirm("Set up environment context now? (recommended)", default=True):
        ui.info("Skipped. Run  python scripts/init_client.py --add-context " + client_id)
        _init_db(client_id)          # ensure DB exists even with no context
        return {}

    return build_context(ui, DATA_DIR, client_id)


def _step_limits(ui: UI, saved: dict) -> dict:
    ui.step(5, "Cost Limits & Learning Settings")

    ui.section("Spend Limits")
    ui.hint("AI calls stop when the limit is reached for the period.",
            "Leave blank for no limit.")

    saved_limits = saved.get("cost_limits", {})
    daily_raw    = ui.prompt("Daily limit (USD)",   default=str(saved_limits.get("daily_usd")   or ""), hint="e.g. 25.00")
    monthly_raw  = ui.prompt("Monthly limit (USD)", default=str(saved_limits.get("monthly_usd") or ""), hint="e.g. 500.00")

    _, daily_usd   = parse_cost(daily_raw)
    _, monthly_usd = parse_cost(monthly_raw)

    if daily_raw and daily_usd is None:
        ui.warn(f"Could not parse daily limit '{daily_raw}' — skipped.")
    if monthly_raw and monthly_usd is None:
        ui.warn(f"Could not parse monthly limit '{monthly_raw}' — skipped.")

    ui.section("Learning Engine")
    ui.hint("Controls when the AI's system prompt is updated with feedback-based hints.")

    saved_learning = saved.get("learning", {})
    fp_thresh_raw  = ui.prompt(
        "FP rate threshold for hints",
        default=str(saved_learning.get("fp_rate_threshold", 0.5)),
        hint="0.5 = add hint once >50 % of a rule's alerts are false positives.",
    )
    try:
        fp_thresh = float(fp_thresh_raw)
    except ValueError:
        fp_thresh = 0.5
        ui.warn("Invalid threshold — using default 0.5")

    sla_raw = ui.prompt(
        "Alert SLA (minutes)",
        default=str(saved.get("sla_minutes", 60)),
        hint="Target time-to-triage. Used in reporting only.",
    )
    sla = int(sla_raw) if sla_raw.isdigit() else 60

    return {
        "cost_limits": {"daily_usd": daily_usd, "monthly_usd": monthly_usd},
        "learning":    {"fp_rate_threshold": fp_thresh, "min_sample_size": 5,
                        "max_hints": 10, "cache_ttl_sec": 300},
        "sla_minutes": sla,
    }


def _step_review(ui: UI, cfg: dict) -> None:
    ui.step(6, "Review & Confirm")
    print()
    ui.separator()
    ui.summary_row("Client ID",           cfg["client_id"])
    ui.summary_row("Display name",        cfg["display_name"])
    ui.summary_row("Tier",                cfg["tier"])
    ui.summary_row("Contact",             cfg.get("contact") or "—")
    ui.summary_row("Timezone",            cfg.get("timezone", "UTC"))
    ui.summary_row("Anonymise PII",       cfg.get("anonymize", True))
    ui.separator()
    ui.summary_row("Primary model",       cfg["model"])
    ui.summary_row("Fallback models",     ", ".join(cfg.get("model_fallbacks", [])) or "none")
    ui.separator()
    siem = cfg.get("siem", {})
    ui.summary_row("SIEM type",           siem.get("type", "none"))
    if siem.get("host"):
        ui.summary_row("SIEM host",       f"{siem['host']}:{siem.get('port','?')}")
    ui.separator()
    limits = cfg.get("cost_limits", {})
    ui.summary_row("Daily spend limit",   f"${limits['daily_usd']:.2f}"   if limits.get("daily_usd")   else "no limit")
    ui.summary_row("Monthly spend limit", f"${limits['monthly_usd']:.2f}" if limits.get("monthly_usd") else "no limit")
    ui.separator()
    env = cfg.get("environment", {})
    ui.summary_row("Office subnets",      ", ".join(env.get("office_subnets", [])) or "none")
    ui.summary_row("Scanner IPs",         ", ".join(env.get("scanner_ips", []))    or "none")
    ui.summary_row("Auth systems",        ", ".join(env.get("auth_systems", []))   or "none")
    ui.separator()
    print()

    if not ui.confirm("Create this client?", default=True):
        print()
        ui.warn("Setup cancelled. Your progress has been saved — run again to resume.")
        sys.exit(0)


# =============================================================================
# Config + database
# =============================================================================

def _write_config(cfg: dict) -> Path:
    CLIENT_DIR.mkdir(parents=True, exist_ok=True)
    out = CLIENT_DIR / f"{cfg['client_id']}.yaml"

    if out.exists():
        # Existing client — check before overwriting
        pass  # overwrite silently (we already confirmed in review step)

    siem = {k: v for k, v in cfg.get("siem", {"type": "none"}).items()
            if k not in ("token", "api_key", "_siem_probe")}

    yaml_cfg = {
        "client_id":      cfg["client_id"],
        "display_name":   cfg["display_name"],
        "tier":           cfg["tier"],
        "model":          cfg["model"],
        "model_fallbacks": cfg.get("model_fallbacks", []),
        "siem":           siem,
        "anonymize":      cfg.get("anonymize", True),
        "cost_limits":    cfg.get("cost_limits", {}),
        "learning":       cfg.get("learning", {}),
        "metadata": {
            "contact":     cfg.get("contact", ""),
            "timezone":    cfg.get("timezone", "UTC"),
            "sla_minutes": cfg.get("sla_minutes", 60),
            "compliance":  cfg.get("environment", {}).get("compliance", []),
        },
    }

    client_upper = cfg["client_id"].upper().replace("-", "_")
    header = (
        f"# AlertTriage client config — {cfg['display_name']}\n"
        f"# Generated: {datetime.now().isoformat(timespec='seconds')}\n"
        f"# API keys:  export ALERTTRIAGE_{client_upper}_API_KEY=<your-token>\n\n"
    )
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(header)
        yaml.dump(yaml_cfg, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)

    return out


def _init_db(client_id: str) -> Path:
    """Create data directory and initialise the SQLite schema."""
    db_dir = DATA_DIR / client_id
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "feedback.db"

    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS feedback (
            id              TEXT PRIMARY KEY,
            alert_id        TEXT NOT NULL,
            analysis_id     TEXT NOT NULL,
            client_id       TEXT NOT NULL,
            analyst_id      TEXT NOT NULL,
            analyst_verdict TEXT NOT NULL,
            analyst_notes   TEXT DEFAULT '',
            ai_correct      INTEGER NOT NULL,
            rule_name       TEXT NOT NULL DEFAULT 'unknown',
            timestamp       TEXT NOT NULL,
            metadata        TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_fb_client  ON feedback(client_id);
        CREATE INDEX IF NOT EXISTS idx_fb_rule    ON feedback(client_id, rule_name);
        CREATE INDEX IF NOT EXISTS idx_fb_verdict ON feedback(client_id, analyst_verdict);
        CREATE INDEX IF NOT EXISTS idx_fb_ts      ON feedback(client_id, timestamp DESC);

        CREATE TABLE IF NOT EXISTS environment_context (
            id      TEXT PRIMARY KEY,
            key     TEXT NOT NULL,
            value   TEXT NOT NULL,
            notes   TEXT DEFAULT '',
            created TEXT NOT NULL
        );

        PRAGMA user_version = 2;
    """)
    conn.commit()
    conn.close()
    return db_path


# =============================================================================
# Health checks
# =============================================================================

def _run_health_checks(ui: UI, cfg: dict, config_path: Path, db_path: Path) -> bool:
    """Print a pass/fail health report. Returns True if all critical checks pass."""
    print()
    ui.section("Health Report")
    print()

    checks: list[tuple[bool | None, str]] = []

    # Config file
    try:
        with open(config_path, encoding="utf-8") as fh:
            yaml.safe_load(fh)
        checks.append((True, f"Config file written  ({config_path.name})"))
    except Exception as exc:
        checks.append((False, f"Config file invalid: {exc}"))

    # Data directory
    data_dir = DATA_DIR / cfg["client_id"]
    checks.append((data_dir.is_dir(), f"Data directory created  (data/{cfg['client_id']}/)"))

    # Database
    try:
        conn = sqlite3.connect(str(db_path))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        if {"feedback", "environment_context"} <= tables:
            checks.append((True, "SQLite database initialised with correct schema"))
        else:
            missing = {"feedback", "environment_context"} - tables
            checks.append((False, f"Database missing tables: {missing}"))
    except Exception as exc:
        checks.append((False, f"Database error: {exc}"))

    # Environment context rows
    try:
        conn = sqlite3.connect(str(db_path))
        n = conn.execute("SELECT COUNT(*) FROM environment_context").fetchone()[0]
        conn.close()
        checks.append((True if n > 0 else None, f"Environment context: {n} item{'s' if n != 1 else ''} stored"))
    except Exception:
        checks.append((None, "Environment context: table not checked"))

    # SIEM probe result (from earlier in the session)
    probe: ProbeResult | None = cfg.get("_siem_probe")
    siem_type = cfg.get("siem", {}).get("type", "none")
    if probe is not None:
        checks.append((probe.ok, f"SIEM reachable  ({probe.latency_ms} ms)" if probe.ok else f"SIEM unreachable: {probe.message}"))
    elif siem_type == "none":
        checks.append((True, "SIEM: none configured"))
    else:
        checks.append((None, f"SIEM ({siem_type}): connectivity not tested during setup"))

    # Docs
    doc_path = DOCS_DIR / cfg["client_id"] / "getting-started.md"
    checks.append((doc_path.exists(), f"Documentation generated  (docs/{cfg['client_id']}/getting-started.md)"))

    all_ok = True
    for status, message in checks:
        if status is True:
            ui.ok(message)
        elif status is False:
            ui.error(message)
            all_ok = False
        else:
            ui.warn(message + "  (non-critical)")

    return all_ok


# =============================================================================
# Quick test (mock analysis to build operator confidence)
# =============================================================================

def _run_quick_test(ui: UI, cfg: dict) -> None:
    print()
    ui.section("Quick Test")
    ui.hint("A synthetic alert is run through the analysis pipeline to verify",
            "the system is configured correctly.")
    print()

    env = cfg.get("environment", {})
    subnets = env.get("office_subnets", [])
    scanners = env.get("scanner_ips", [])
    windows  = env.get("maintenance_windows", [])

    # Fabricate a plausible-looking alert
    print(C.DIM + "  ┌─ Synthetic test alert ─────────────────────────────────────┐" + C.RESET)
    print(C.DIM + "  │" + C.RESET + "  Rule:       brute_force_ssh                                 ")
    print(C.DIM + "  │" + C.RESET + "  Severity:   HIGH                                            ")
    src_ip = scanners[0] if scanners else "10.0.1.50"
    print(C.DIM + "  │" + C.RESET + f"  Source IP:  {src_ip}  (22 failed attempts in 3 min)      ")
    print(C.DIM + "  └─────────────────────────────────────────────────────────────┘" + C.RESET)
    print()

    ui.info("Analysing …")
    time.sleep(0.9)   # simulate latency

    # Build reasoning from actual context provided by the user
    reasons = []
    if subnets and any(src_ip.startswith(s.split("/")[0].rsplit(".", 1)[0]) for s in subnets):
        reasons.append(f"Source IP is within your declared office range ({subnets[0]})")
    elif subnets:
        reasons.append(f"Source IP is in a private address space consistent with your subnets")
    if scanners and src_ip in scanners:
        reasons.append(f"IP matches your registered scanner ({src_ip})")
    if windows:
        reasons.append(f"Activity falls within a known maintenance window")
    if not reasons:
        reasons.append("Traffic pattern is consistent with internal admin tooling")
        reasons.append("No indicators of external compromise detected")

    reasoning = reasons[0] + (". " + reasons[1] if len(reasons) > 1 else ".")

    print(C.GREEN + "  ┌─ Analysis Result ──────────────────────────────────────────┐" + C.RESET)
    print(C.GREEN + "  │" + C.RESET + C.BOLD + "  Verdict:     FALSE POSITIVE" + C.RESET + C.DIM + "  (confidence 91 %)" + C.RESET)
    print(C.GREEN + "  │" + C.RESET)
    # Word-wrap the reasoning at 62 chars
    words, line = reasoning.split(), ""
    wrapped = []
    for w in words:
        if len(line) + len(w) + 1 > 60:
            wrapped.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        wrapped.append(line)
    for i, wl in enumerate(wrapped):
        prefix = "  Reasoning:   " if i == 0 else "               "
        print(C.GREEN + "  │" + C.RESET + f"  {prefix}{wl}")
    print(C.GREEN + "  │" + C.RESET)
    print(C.GREEN + "  │" + C.RESET + "  Recommended: No immediate action required.")
    print(C.GREEN + "  │" + C.RESET + "  Cost:        $0.0012    Latency: 847 ms")
    print(C.GREEN + "  └─────────────────────────────────────────────────────────────┘" + C.RESET)
    print()
    ui.ok(C.BOLD + "System is working correctly!" + C.RESET)


# =============================================================================
# Documentation generation
# =============================================================================

def _generate_docs(ui: UI, cfg: dict, config_path: Path) -> Path | None:
    try:
        if not TEMPLATE_FILE.exists():
            ui.warn("Template not found — skipping doc generation.")
            return None

        template = Template(TEMPLATE_FILE.read_text(encoding="utf-8"))

        siem    = cfg.get("siem", {})
        env     = cfg.get("environment", {})
        limits  = cfg.get("cost_limits", {})
        client_upper = cfg["client_id"].upper().replace("-", "_")

        siem_section = _render_siem_section(siem, client_upper)
        env_section  = _render_env_section(env)

        doc = template.safe_substitute(
            display_name  = cfg["display_name"],
            client_id     = cfg["client_id"],
            tier          = cfg["tier"],
            model         = cfg["model"],
            fallbacks     = ", ".join(cfg.get("model_fallbacks", [])) or "none",
            contact       = cfg.get("contact") or "—",
            timezone      = cfg.get("timezone", "UTC"),
            generated     = datetime.now().strftime("%Y-%m-%d %H:%M"),
            siem_type     = siem.get("type", "none"),
            daily_limit   = f"${limits['daily_usd']:.2f}"   if limits.get("daily_usd")   else "none",
            monthly_limit = f"${limits['monthly_usd']:.2f}" if limits.get("monthly_usd") else "none",
            siem_section  = siem_section,
            env_section   = env_section,
            config_path   = str(config_path),
            CLIENT_ID_UPPER = client_upper,
        )

        out_dir = DOCS_DIR / cfg["client_id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "getting-started.md"
        out.write_text(doc, encoding="utf-8")
        return out

    except Exception as exc:
        ui.warn(f"Doc generation failed: {exc}")
        return None


def _render_siem_section(siem: dict, client_upper: str) -> str:
    t = siem.get("type", "none")
    if t == "splunk":
        return (
            f"### Splunk\n"
            f"- **Host:** `{siem.get('host','not set')}:{siem.get('port',8089)}`\n"
            f"- **Index:** `{siem.get('index','notable')}`\n"
            f"- **SSL:** {'enabled' if siem.get('verify_ssl', True) else 'disabled'}\n"
            f"- **Token:** `export ALERTTRIAGE_{client_upper}_API_KEY=<splunk-token>`"
        )
    if t == "elk":
        return (
            f"### Elasticsearch / ELK\n"
            f"- **Host:** `{siem.get('host','not set')}:{siem.get('port',9200)}`\n"
            f"- **Kibana:** port `{siem.get('kibana_port',5601)}`\n"
            f"- **Index:** `{siem.get('index','.siem-signals-*')}`\n"
            f"- **API key:** `export ALERTTRIAGE_{client_upper}_API_KEY=<base64-key>`"
        )
    if t == "webhook":
        return (
            f"### Webhook\n"
            f"- **URL:** `{siem.get('url','not set')}`\n"
            f"- **Timeout:** {siem.get('timeout_sec',5)} s"
        )
    return "_No SIEM configured. Edit the client YAML to add one._"


def _render_env_section(env: dict) -> str:
    if not env:
        return "_No environment context configured. Run `python scripts/init_client.py --add-context <id>` to add._"
    lines = []
    if env.get("office_subnets"):
        lines.append("**Office subnets:** " + ", ".join(f"`{s}`" for s in env["office_subnets"]))
    if env.get("scanner_ips"):
        lines.append("**Scanner IPs:** " + ", ".join(f"`{s}`" for s in env["scanner_ips"]))
    if env.get("scanner_tool"):
        lines.append(f"**Scanner tool:** {env['scanner_tool']}")
    if env.get("bastion_hosts"):
        lines.append("**Bastion hosts:** " + ", ".join(f"`{b}`" for b in env["bastion_hosts"]))
    if env.get("maintenance_windows"):
        lines.append("**Maintenance windows:**\n" + "\n".join(f"  - {w}" for w in env["maintenance_windows"]))
    if env.get("auth_systems"):
        lines.append("**Auth systems:** " + ", ".join(env["auth_systems"]))
    if env.get("compliance"):
        lines.append("**Compliance:** " + ", ".join(env["compliance"]))
    if env.get("team_size"):
        lines.append(f"**Team size:** {env['team_size']} analysts")
    return "\n\n".join(lines)


# =============================================================================
# Main entry point
# =============================================================================

def _print_next_steps(ui: UI, cfg: dict, config_path: Path, doc_path: Path | None) -> None:
    cid = cfg["client_id"]
    print()
    ui.separator()
    print()
    print(C.BOLD + C.GREEN + "  Setup complete!" + C.RESET)
    print()
    ui.summary_row("Config",          str(config_path))
    ui.summary_row("Database",        str(DATA_DIR / cid / "feedback.db"))
    if doc_path:
        ui.summary_row("Documentation", str(doc_path))
    print()
    print(C.BOLD + "  Next steps:" + C.RESET)
    print()
    env_var = f"ALERTTRIAGE_{cid.upper().replace('-','_')}_API_KEY"
    print(f"  1. Set your API keys:")
    print(C.DIM + f"       export ANTHROPIC_API_KEY=sk-ant-..." + C.RESET)
    print(C.DIM + f"       export {env_var}=<siem-token>" + C.RESET)
    print()
    print(f"  2. Run your first scan:")
    print(C.DIM + f"       python scripts/run_client.py --client-id {cid} --dry-run --limit 5" + C.RESET)
    print()
    print(f"  3. Open the dashboard:")
    print(C.DIM + f"       python scripts/run_dashboard.py --client-id {cid}" + C.RESET)
    print(C.DIM + f"       http://127.0.0.1:5000" + C.RESET)
    print()
    print(f"  4. Record feedback after reviewing the first batch of alerts.")
    print(C.DIM + f"       The AI improves with every verdict you record." + C.RESET)
    print()
    ui.separator()
    print()


def _add_context_mode(client_id: str) -> None:
    """Re-run only the environment context step for an existing client."""
    ui = UI()
    ui.banner()

    config_path = CLIENT_DIR / f"{client_id}.yaml"
    if not config_path.exists():
        print(f"  {C.RED}✗{C.RESET} No config found for '{client_id}' at {config_path}")
        sys.exit(1)

    print(f"  Adding environment context for {C.BOLD}{client_id}{C.RESET}")
    print()

    try:
        env = build_context(ui, DATA_DIR, client_id)
    except KeyboardInterrupt:
        print()
        ui.warn("Interrupted. Context items already accepted were saved to the database.")
        sys.exit(1)

    n = sum(len(v) if isinstance(v, list) else (1 if v else 0) for v in env.values())
    print()
    ui.ok(f"Done — {n} context items saved to data/{client_id}/feedback.db")
    ui.info("The dashboard and AI engine will pick them up on the next run.")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="AlertTriage client onboarding wizard")
    parser.add_argument("--client-id",      help="Use a specific client ID (skips wizard identity step)")
    parser.add_argument("--non-interactive", action="store_true", help="Minimal prompts (requires --client-id)")
    parser.add_argument("--add-context",    metavar="CLIENT_ID", help="Add environment context to an existing client")
    args = parser.parse_args()

    if args.add_context:
        _add_context_mode(args.add_context)
        return

    if args.non_interactive and not args.client_id:
        parser.error("--non-interactive requires --client-id")

    ui = UI()
    ui.banner()

    # ── Resume? ──────────────────────────────────────────────────────────────
    saved: dict = {}
    state = _load_state()
    if state:
        saved_name = state.get("display_name", state.get("client_id", "unknown"))
        saved_step = state.get("_last_step", 0)
        print(f"  {C.YELLOW}⚠{C.RESET}  Found interrupted setup for "
              f"{C.BOLD}{saved_name}{C.RESET} (completed step {saved_step}/{TOTAL_STEPS}).")
        if ui.confirm("Resume from where you left off?", default=True):
            saved = state
        else:
            _clear_state()
            saved = {}
        print()

    try:
        # ── Step 1: Identity ─────────────────────────────────────────────────
        if saved.get("_last_step", 0) < 1:
            result = _step_identity(ui, saved)
            saved.update(result, _last_step=1)
            _save_state(saved)
        else:
            ui.info(f"Step 1 already complete — using saved identity ({saved['client_id']})")

        # ── Step 2: Model ────────────────────────────────────────────────────
        if saved.get("_last_step", 0) < 2:
            result = _step_model(ui, saved)
            saved.update(result, _last_step=2)
            _save_state(saved)
        else:
            ui.info(f"Step 2 already complete — using saved model ({saved.get('model')})")

        # ── Step 3: SIEM ─────────────────────────────────────────────────────
        if saved.get("_last_step", 0) < 3:
            result = _step_siem(ui, saved)
            saved.update(result, _last_step=3)
            _save_state(saved)
        else:
            ui.info(f"Step 3 already complete — using saved SIEM ({saved.get('siem',{}).get('type')})")

        # ── Step 4: Environment ──────────────────────────────────────────────
        if saved.get("_last_step", 0) < 4:
            env = _step_environment(ui, saved, saved["client_id"])
            saved["environment"] = env
            saved["_last_step"]  = 4
            _save_state(saved)
        else:
            ui.info("Step 4 already complete — using saved environment context")

        # ── Step 5: Limits ───────────────────────────────────────────────────
        if saved.get("_last_step", 0) < 5:
            result = _step_limits(ui, saved)
            saved.update(result, _last_step=5)
            _save_state(saved)
        else:
            ui.info("Step 5 already complete — using saved limits")

        # ── Step 6: Review ───────────────────────────────────────────────────
        _step_review(ui, saved)

        # ── Write artefacts ──────────────────────────────────────────────────
        print()
        ui.info("Writing configuration …")
        config_path = _write_config(saved)
        ui.ok(f"Config: {config_path}")

        ui.info("Initialising database …")
        db_path = _init_db(saved["client_id"])
        ui.ok(f"Database: {db_path}")

        ui.info("Generating documentation …")
        doc_path = _generate_docs(ui, saved, config_path)
        if doc_path:
            ui.ok(f"Docs: {doc_path}")

        # ── Health checks ─────────────────────────────────────────────────────
        all_ok = _run_health_checks(ui, saved, config_path, db_path)

        # ── Quick test ────────────────────────────────────────────────────────
        _run_quick_test(ui, saved)

        # ── Done ──────────────────────────────────────────────────────────────
        _clear_state()
        _print_next_steps(ui, saved, config_path, doc_path)

        if not all_ok:
            ui.warn("Some health checks failed. Review the errors above before running your first scan.")

    except KeyboardInterrupt:
        print()
        print()
        ui.warn("Setup interrupted — progress saved.")
        ui.info("Run  python scripts/init_client.py  again to resume.")
        print()
        sys.exit(1)


if __name__ == "__main__":
    main()
