"""AlertTriage Web Dashboard – Flask application."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, Response, g, jsonify, render_template, request

# ---------------------------------------------------------------------------
# Path setup – lets app.py import from the parent alerttriage package
# ---------------------------------------------------------------------------
DASHBOARD_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DASHBOARD_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

app = Flask(
    __name__,
    template_folder=str(DASHBOARD_DIR / "templates"),
    static_folder=str(DASHBOARD_DIR / "static"),
)

# ---------------------------------------------------------------------------
# Config – set by run_dashboard.py before import
# ---------------------------------------------------------------------------
CLIENT_ID: str = os.environ.get("ALERTTRIAGE_CLIENT_ID", "demo")
DATA_DIR: Path = PROJECT_ROOT / "data"


def _db_path() -> Path:
    return DATA_DIR / CLIENT_ID / "feedback.db"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        p = _db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure_extra_tables(conn)
        g.db = conn
    return g.db


def _ensure_extra_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS environment_context (
            id      TEXT PRIMARY KEY,
            key     TEXT NOT NULL,
            value   TEXT NOT NULL,
            notes   TEXT DEFAULT '',
            created TEXT NOT NULL
        );
    """)
    conn.commit()


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db:
        db.close()


def q(sql: str, params=(), *, one: bool = False):
    """Execute SQL and return list of dicts (or a single dict if one=True)."""
    cur = get_db().execute(sql, params)
    rows = cur.fetchall()
    if one:
        return dict(rows[0]) if rows else None
    return [dict(r) for r in rows]


def _has_feedback_table() -> bool:
    r = q("SELECT name FROM sqlite_master WHERE type='table' AND name='feedback'", one=True)
    return r is not None


# ---------------------------------------------------------------------------
# Learning data helpers
# ---------------------------------------------------------------------------

def _get_learning_data() -> dict:
    """Try LearningEngine first; fall back to raw SQL if import fails."""
    try:
        from alerttriage.src.feedback.feedback_system import FeedbackSystem  # noqa: F401
        from alerttriage.src.feedback.learning_engine import LearningEngine

        fs = FeedbackSystem(DATA_DIR, CLIENT_ID)
        le = LearningEngine(fs)
        insights = le.compute_rule_insights()
        hints = le.generate_prompt_hints()
        worst = le.worst_performing_rules(n=5)
        acc = le.overall_accuracy()
        return {
            "insights": [
                {
                    "rule_name": i.rule_name,
                    "total": i.total,
                    "correct": i.correct,
                    "false_positive_rate": round(i.false_positive_rate * 100, 1),
                    "misclassifications": i.common_misclassifications,
                }
                for i in insights
            ],
            "hints": hints,
            "worst_rules": [w.rule_name for w in worst],
            "accuracy": round(acc * 100, 1),
        }
    except Exception:
        return _learning_from_sql()


def _learning_from_sql() -> dict:
    if not _has_feedback_table():
        return {"insights": [], "hints": "No feedback data yet.", "worst_rules": [], "accuracy": 0}

    rows = q(
        """
        SELECT rule_name,
               COUNT(*) AS total,
               SUM(ai_correct) AS correct,
               SUM(CASE WHEN analyst_verdict = 'false_positive' THEN 1 ELSE 0 END) AS fps
        FROM feedback WHERE client_id = ?
        GROUP BY rule_name ORDER BY total DESC
        """,
        (CLIENT_ID,),
    )

    insights = [
        {
            "rule_name": r["rule_name"],
            "total": r["total"],
            "correct": r["correct"] or 0,
            "false_positive_rate": round(((r["fps"] or 0) / max(r["total"], 1)) * 100, 1),
            "misclassifications": [],
        }
        for r in rows
    ]

    worst = sorted(insights, key=lambda x: x["false_positive_rate"], reverse=True)[:5]
    overall = q("SELECT AVG(ai_correct) * 100 AS acc FROM feedback WHERE client_id = ?", (CLIENT_ID,), one=True)
    acc = round((overall["acc"] or 0) if overall else 0, 1)

    lines = ["Based on analyst feedback, the system has learned:"]
    for ins in sorted(insights, key=lambda x: x["false_positive_rate"], reverse=True)[:5]:
        if ins["false_positive_rate"] > 30:
            lines.append(f"  • {ins['rule_name']}: {ins['false_positive_rate']}% false positive rate — treat cautiously")
        elif ins["false_positive_rate"] < 10 and ins["total"] > 5:
            lines.append(f"  • {ins['rule_name']}: high precision ({100 - ins['false_positive_rate']}% accuracy)")
    hints = "\n".join(lines) if len(lines) > 1 else "Record more feedback to generate learning hints."

    return {
        "insights": insights,
        "hints": hints,
        "worst_rules": [w["rule_name"] for w in worst],
        "accuracy": acc,
    }


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    return render_template("dashboard.html", client_id=CLIENT_ID)


@app.route("/alerts")
def alerts_page():
    return render_template("alerts.html", client_id=CLIENT_ID)


@app.route("/learning")
def learning_page():
    return render_template("learning.html", client_id=CLIENT_ID)


@app.route("/feedback")
def feedback_page():
    return render_template("feedback.html", client_id=CLIENT_ID)


@app.route("/admin")
def admin_page():
    return render_template("admin.html", client_id=CLIENT_ID)


# ---------------------------------------------------------------------------
# API – Dashboard stats
# ---------------------------------------------------------------------------

@app.route("/api/stats")
def api_stats():
    if not _has_feedback_table():
        return jsonify({"today": 0, "total": 0, "fp_caught": 0, "time_saved_hrs": 0, "accuracy": 0})

    today = date.today().isoformat()
    row = q(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN date(timestamp) = ? THEN 1 ELSE 0 END) AS today,
               SUM(CASE WHEN analyst_verdict = 'false_positive' THEN 1 ELSE 0 END) AS fp_caught,
               AVG(ai_correct) * 100 AS accuracy,
               SUM(CASE WHEN ai_correct=1 AND analyst_verdict='false_positive' THEN 1 ELSE 0 END) AS tn
        FROM feedback WHERE client_id = ?
        """,
        (today, CLIENT_ID),
        one=True,
    )
    if not row or not row["total"]:
        return jsonify({"today": 0, "total": 0, "fp_caught": 0, "time_saved_hrs": 0, "accuracy": 0})

    return jsonify({
        "today": row["today"] or 0,
        "total": row["total"] or 0,
        "fp_caught": row["fp_caught"] or 0,
        "time_saved_hrs": round((row["tn"] or 0) * 0.25, 1),
        "accuracy": round(row["accuracy"] or 0, 1),
    })


@app.route("/api/trend")
def api_trend():
    if not _has_feedback_table():
        return jsonify([])

    days = int(request.args.get("days", 30))
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = q(
        """
        SELECT date(timestamp) AS day,
               COUNT(*) AS total,
               SUM(CASE WHEN analyst_verdict='false_positive' THEN 1 ELSE 0 END) AS fps,
               AVG(ai_correct) * 100 AS accuracy
        FROM feedback WHERE client_id = ? AND timestamp >= ?
        GROUP BY day ORDER BY day
        """,
        (CLIENT_ID, since),
    )
    return jsonify([
        {
            "date": r["day"],
            "total": r["total"],
            "fp_rate": round(((r["fps"] or 0) / max(r["total"], 1)) * 100, 1),
            "accuracy": round(r["accuracy"] or 0, 1),
        }
        for r in rows
    ])


@app.route("/api/precision-recall")
def api_precision_recall():
    if not _has_feedback_table():
        return jsonify({"precision": 0, "recall": 0, "f1": 0, "tp": 0, "fp": 0, "fn": 0, "tn": 0})

    month = datetime.now().strftime("%Y-%m")
    row = q(
        """
        SELECT
            SUM(CASE WHEN ai_correct=1 AND analyst_verdict='true_positive'  THEN 1 ELSE 0 END) AS tp,
            SUM(CASE WHEN ai_correct=0 AND analyst_verdict='true_positive'  THEN 1 ELSE 0 END) AS fn,
            SUM(CASE WHEN ai_correct=0 AND analyst_verdict='false_positive' THEN 1 ELSE 0 END) AS fp,
            SUM(CASE WHEN ai_correct=1 AND analyst_verdict='false_positive' THEN 1 ELSE 0 END) AS tn
        FROM feedback WHERE client_id = ? AND strftime('%Y-%m', timestamp) = ?
        """,
        (CLIENT_ID, month),
        one=True,
    )
    tp = row["tp"] or 0
    fn = row["fn"] or 0
    fp = row["fp"] or 0
    tn = row["tn"] or 0
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    return jsonify({
        "precision": round(precision * 100, 1),
        "recall": round(recall * 100, 1),
        "f1": round(f1 * 100, 1),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
    })


# ---------------------------------------------------------------------------
# API – Alerts
# ---------------------------------------------------------------------------

@app.route("/api/alerts")
def api_alerts():
    if not _has_feedback_table():
        return jsonify({"alerts": [], "total": 0})

    verdict = request.args.get("verdict", "")
    sort = request.args.get("sort", "timestamp")
    order = request.args.get("order", "desc").upper()
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(100, max(10, int(request.args.get("per_page", 50))))

    if sort not in {"timestamp", "rule_name", "analyst_verdict", "ai_correct"}:
        sort = "timestamp"
    if order not in {"ASC", "DESC"}:
        order = "DESC"

    where, params = "client_id = ?", [CLIENT_ID]
    if verdict:
        where += " AND analyst_verdict = ?"
        params.append(verdict)

    total = (q(f"SELECT COUNT(*) AS n FROM feedback WHERE {where}", params, one=True) or {}).get("n", 0)
    offset = (page - 1) * per_page

    rows = q(
        f"""
        SELECT id, alert_id, rule_name, analyst_verdict, analyst_notes,
               ai_correct, timestamp, metadata
        FROM feedback WHERE {where}
        ORDER BY {sort} {order} LIMIT ? OFFSET ?
        """,
        [*params, per_page, offset],
    )

    alerts = []
    for r in rows:
        meta = {}
        try:
            meta = json.loads(r["metadata"] or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        alerts.append({
            "id": r["id"],
            "alert_id": r["alert_id"],
            "rule_name": r["rule_name"],
            "verdict": r["analyst_verdict"],
            "ai_correct": bool(r["ai_correct"]),
            "confidence": meta.get("confidence"),
            "notes": r["analyst_notes"],
            "timestamp": r["timestamp"],
        })

    return jsonify({"alerts": alerts, "total": total, "page": page, "per_page": per_page})


@app.route("/api/rules")
def api_rules():
    if not _has_feedback_table():
        return jsonify([])
    rows = q(
        "SELECT DISTINCT rule_name FROM feedback WHERE client_id = ? ORDER BY rule_name",
        (CLIENT_ID,),
    )
    return jsonify([r["rule_name"] for r in rows])


# ---------------------------------------------------------------------------
# API – Learning
# ---------------------------------------------------------------------------

@app.route("/api/learning")
def api_learning():
    return jsonify(_get_learning_data())


# ---------------------------------------------------------------------------
# API – Feedback
# ---------------------------------------------------------------------------

@app.route("/api/feedback", methods=["GET"])
def api_feedback_list():
    if not _has_feedback_table():
        return jsonify({"feedback": [], "total": 0})

    page = max(1, int(request.args.get("page", 1)))
    per_page = min(100, max(10, int(request.args.get("per_page", 20))))
    offset = (page - 1) * per_page

    total = (q("SELECT COUNT(*) AS n FROM feedback WHERE client_id = ?", (CLIENT_ID,), one=True) or {}).get("n", 0)
    rows = q(
        """
        SELECT id, alert_id, analyst_id, analyst_verdict, analyst_notes,
               ai_correct, rule_name, timestamp
        FROM feedback WHERE client_id = ?
        ORDER BY timestamp DESC LIMIT ? OFFSET ?
        """,
        (CLIENT_ID, per_page, offset),
    )
    return jsonify({"feedback": rows, "total": total, "page": page, "per_page": per_page})


@app.route("/api/feedback", methods=["POST"])
def api_feedback_record():
    data = request.get_json(force=True) or {}
    if not data.get("rule_name") or not data.get("analyst_verdict"):
        return jsonify({"error": "rule_name and analyst_verdict are required"}), 400

    valid = {"true_positive", "false_positive", "escalated", "closed"}
    if data["analyst_verdict"] not in valid:
        return jsonify({"error": f"analyst_verdict must be one of: {', '.join(sorted(valid))}"}), 400

    rid = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    get_db().execute(
        """
        INSERT INTO feedback
            (id, alert_id, analysis_id, client_id, analyst_id, analyst_verdict,
             analyst_notes, ai_correct, rule_name, timestamp, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rid,
            data.get("alert_id") or f"manual-{rid[:8]}",
            data.get("analysis_id") or f"dashboard-{rid[:8]}",
            CLIENT_ID,
            data.get("analyst_id", "dashboard-user"),
            data["analyst_verdict"],
            data.get("notes", ""),
            1 if data.get("ai_correct") else 0,
            data["rule_name"],
            now,
            json.dumps(data.get("metadata", {})),
        ),
    )
    get_db().commit()
    return jsonify({"id": rid, "status": "recorded"}), 201


@app.route("/api/feedback/impact")
def api_feedback_impact():
    if not _has_feedback_table():
        return jsonify({"message": "No feedback data yet."})

    total = (q("SELECT COUNT(*) AS n FROM feedback WHERE client_id = ?", (CLIENT_ID,), one=True) or {}).get("n", 0)
    if total < 10:
        return jsonify({"message": f"Need at least 10 records for impact analysis (have {total}).", "total": total})

    mid = total // 2
    first = q(
        "SELECT AVG(ai_correct)*100 AS acc FROM (SELECT ai_correct FROM feedback WHERE client_id=? ORDER BY timestamp ASC LIMIT ?)",
        (CLIENT_ID, mid),
        one=True,
    )
    last = q(
        "SELECT AVG(ai_correct)*100 AS acc FROM (SELECT ai_correct FROM feedback WHERE client_id=? ORDER BY timestamp DESC LIMIT ?)",
        (CLIENT_ID, mid),
        one=True,
    )
    early = round((first["acc"] or 0) if first else 0, 1)
    recent = round((last["acc"] or 0) if last else 0, 1)
    delta = round(recent - early, 1)
    return jsonify({
        "early_accuracy": early,
        "recent_accuracy": recent,
        "delta": delta,
        "total_records": total,
        "message": f"Accuracy {'improved' if delta >= 0 else 'declined'} by {abs(delta)}% from early to recent feedback.",
    })


# ---------------------------------------------------------------------------
# API – Admin
# ---------------------------------------------------------------------------

@app.route("/api/admin/stats")
def api_admin_stats():
    p = _db_path()
    stats: dict = {
        "db_path": str(p),
        "db_exists": p.exists(),
        "db_size_kb": round(p.stat().st_size / 1024, 1) if p.exists() else 0,
        "client_id": CLIENT_ID,
        "total_records": 0,
        "by_verdict": {},
    }
    if _has_feedback_table():
        stats["total_records"] = (
            q("SELECT COUNT(*) AS n FROM feedback WHERE client_id=?", (CLIENT_ID,), one=True) or {}
        ).get("n", 0)
        rows = q(
            "SELECT analyst_verdict, COUNT(*) AS n FROM feedback WHERE client_id=? GROUP BY analyst_verdict",
            (CLIENT_ID,),
        )
        stats["by_verdict"] = {r["analyst_verdict"]: r["n"] for r in rows}
    return jsonify(stats)


@app.route("/api/admin/context", methods=["GET"])
def api_admin_context_list():
    rows = q("SELECT id, key, value, notes, created FROM environment_context ORDER BY created DESC")
    return jsonify({"context": rows})


@app.route("/api/admin/context", methods=["POST"])
def api_admin_context_add():
    data = request.get_json(force=True) or {}
    if not data.get("key") or not data.get("value"):
        return jsonify({"error": "key and value are required"}), 400

    cid = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    get_db().execute(
        "INSERT INTO environment_context (id, key, value, notes, created) VALUES (?, ?, ?, ?, ?)",
        (cid, data["key"], data["value"], data.get("notes", ""), now),
    )
    get_db().commit()
    return jsonify({"id": cid, "status": "added"}), 201


@app.route("/api/admin/context/<ctx_id>", methods=["DELETE"])
def api_admin_context_delete(ctx_id: str):
    get_db().execute("DELETE FROM environment_context WHERE id = ?", (ctx_id,))
    get_db().commit()
    return jsonify({"status": "deleted"})


@app.route("/api/admin/export")
def api_admin_export():
    if not _has_feedback_table():
        return Response("No data available", mimetype="text/plain", status=404)

    rows = q(
        """
        SELECT id, alert_id, analysis_id, client_id, analyst_id, analyst_verdict,
               analyst_notes, ai_correct, rule_name, timestamp, metadata
        FROM feedback WHERE client_id = ? ORDER BY timestamp DESC
        """,
        (CLIENT_ID,),
    )
    if not rows:
        return Response("No records found", mimetype="text/plain", status=404)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)

    filename = f"feedback_{CLIENT_ID}_{date.today().isoformat()}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/api/admin/reset", methods=["POST"])
def api_admin_reset():
    data = request.get_json(force=True) or {}
    if data.get("confirm") != "RESET":
        return jsonify({"error": 'Send {"confirm": "RESET"} to confirm deletion.'}), 400

    if _has_feedback_table():
        get_db().execute("DELETE FROM feedback WHERE client_id = ?", (CLIENT_ID,))
        get_db().commit()

    return jsonify({"status": "cleared", "client_id": CLIENT_ID})


# ---------------------------------------------------------------------------
# Analytics page
# ---------------------------------------------------------------------------

@app.route("/analytics")
def analytics_page():
    return render_template("analytics.html", client_id=CLIENT_ID)


@app.route("/api/analytics/summary")
def api_analytics_summary():
    days = int(request.args.get("days", 30))
    hourly_rate = float(request.args.get("hourly_rate", 75.0))
    minutes_per_alert = float(request.args.get("minutes_per_alert", 15.0))
    if not _has_feedback_table():
        return jsonify({"total": 0, "accuracy": 0, "hours_saved": 0, "labor_value_usd": 0,
                        "ai_cost_usd": 0, "net_value_usd": 0, "weeks": []})

    since = (date.today() - timedelta(days=days)).isoformat()
    rows = q(
        """
        SELECT strftime('%Y-W%W', timestamp) AS week,
               COUNT(*) AS total,
               SUM(ai_correct) AS correct,
               SUM(CASE WHEN analyst_verdict='false_positive' THEN 1 ELSE 0 END) AS fps
        FROM feedback
        WHERE client_id = ? AND timestamp >= ?
        GROUP BY week ORDER BY week
        """,
        (CLIENT_ID, since),
    )
    weeks = []
    total_correct = 0
    total_count = 0
    for r in rows:
        correct = r["correct"] or 0
        total = r["total"] or 0
        total_correct += correct
        total_count += total
        weeks.append({
            "week": r["week"],
            "total": total,
            "correct": correct,
            "false_positives": r["fps"] or 0,
            "accuracy": round(correct / max(total, 1) * 100, 1),
        })

    hours_saved = total_correct * minutes_per_alert / 60
    labor_value = hours_saved * hourly_rate
    ai_cost = total_count * 0.002

    return jsonify({
        "total": total_count,
        "accuracy": round(total_correct / max(total_count, 1) * 100, 1),
        "hours_saved": round(hours_saved, 1),
        "labor_value_usd": round(labor_value, 2),
        "ai_cost_usd": round(ai_cost, 4),
        "net_value_usd": round(labor_value - ai_cost, 2),
        "weeks": weeks,
    })


@app.route("/api/analytics/breakdown")
def api_analytics_breakdown():
    days = int(request.args.get("days", 30))
    if not _has_feedback_table():
        return jsonify({"rules": [], "most_problematic": None, "most_common": None})

    since = (date.today() - timedelta(days=days)).isoformat()
    rows = q(
        """
        SELECT rule_name,
               COUNT(*) AS total,
               SUM(ai_correct) AS correct,
               SUM(CASE WHEN analyst_verdict='false_positive' THEN 1 ELSE 0 END) AS fps
        FROM feedback
        WHERE client_id = ? AND timestamp >= ?
        GROUP BY rule_name ORDER BY total DESC
        """,
        (CLIENT_ID, since),
    )
    rules = []
    for r in rows:
        total = r["total"] or 0
        fps = r["fps"] or 0
        correct = r["correct"] or 0
        rules.append({
            "rule": r["rule_name"],
            "total": total,
            "correct": correct,
            "false_positives": fps,
            "fp_rate": round(fps / max(total, 1) * 100, 1),
            "accuracy": round(correct / max(total, 1) * 100, 1),
        })

    most_problematic = max(rules, key=lambda x: x["fp_rate"])["rule"] if rules else None
    most_common = max(rules, key=lambda x: x["total"])["rule"] if rules else None

    return jsonify({"rules": rules, "most_problematic": most_problematic, "most_common": most_common})


@app.route("/api/analytics/roi")
def api_analytics_roi():
    days = int(request.args.get("days", 30))
    hourly_rate = float(request.args.get("hourly_rate", 75.0))
    minutes_per_alert = float(request.args.get("minutes_per_alert", 15.0))
    if not _has_feedback_table():
        return jsonify({"alerts_analyzed": 0, "hours_saved": 0, "labor_value_usd": 0,
                        "ai_cost_usd": 0, "net_roi_usd": 0, "roi_percentage": 0})

    since = (date.today() - timedelta(days=days)).isoformat()
    row = q(
        "SELECT COUNT(*) AS total, SUM(ai_correct) AS correct FROM feedback WHERE client_id=? AND timestamp>=?",
        (CLIENT_ID, since),
        one=True,
    )
    total = (row["total"] or 0) if row else 0
    correct = (row["correct"] or 0) if row else 0

    hours_saved = correct * minutes_per_alert / 60
    labor_value = hours_saved * hourly_rate
    ai_cost = total * 0.002
    net_roi = labor_value - ai_cost
    roi_pct = (net_roi / max(ai_cost, 0.01)) * 100

    return jsonify({
        "alerts_analyzed": total,
        "hours_saved": round(hours_saved, 1),
        "labor_value_usd": round(labor_value, 2),
        "ai_cost_usd": round(ai_cost, 4),
        "net_roi_usd": round(net_roi, 2),
        "roi_percentage": round(roi_pct, 0),
    })


# ---------------------------------------------------------------------------
# Config editor page
# ---------------------------------------------------------------------------

@app.route("/config-editor")
def config_editor_page():
    return render_template("config_editor.html", client_id=CLIENT_ID)


def _config_dir() -> Path:
    return Path(os.environ.get("ALERTTRIAGE_CONFIG_DIR", str(PROJECT_ROOT / "config")))


@app.route("/api/config/client", methods=["GET"])
def api_config_get():
    import yaml
    cfg_path = _config_dir() / "client_configs" / f"{CLIENT_ID}.yaml"
    if not cfg_path.exists():
        return jsonify({"error": f"No config file for client '{CLIENT_ID}'"}), 404
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return jsonify({"yaml_text": cfg_path.read_text(encoding="utf-8"), "parsed": raw})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/config/client", methods=["PUT"])
def api_config_update():
    import yaml
    data = request.get_json(force=True) or {}
    yaml_text = data.get("yaml_text", "")
    try:
        parsed = yaml.safe_load(yaml_text)
        if not isinstance(parsed, dict):
            return jsonify({"error": "Config must be a YAML mapping"}), 422
    except yaml.YAMLError as exc:
        return jsonify({"error": f"Invalid YAML: {exc}"}), 422
    try:
        cfg_path = _config_dir() / "client_configs" / f"{CLIENT_ID}.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(yaml_text, encoding="utf-8")
        return jsonify({"status": "updated", "client_id": CLIENT_ID})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/config/features", methods=["GET"])
def api_config_features():
    import yaml
    feat_path = _config_dir() / "features.yaml"
    if not feat_path.exists():
        return jsonify({})
    try:
        raw = yaml.safe_load(feat_path.read_text(encoding="utf-8")) or {}
        return jsonify(raw)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/config/alert-types", methods=["GET"])
def api_config_alert_types():
    import yaml
    at_path = _config_dir() / "alert_types.yaml"
    if not at_path.exists():
        return jsonify({})
    try:
        raw = yaml.safe_load(at_path.read_text(encoding="utf-8")) or {}
        return jsonify(raw)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Webhooks management page
# ---------------------------------------------------------------------------

@app.route("/webhooks-mgmt")
def webhooks_page():
    return render_template("webhooks.html", client_id=CLIENT_ID)


_VALID_EVENT_TYPES = {
    "alert_analyzed", "feedback_recorded", "accuracy_changed",
    "threshold_exceeded", "report_generated", "cost_limit_approaching",
}


def _webhooks_path() -> Path:
    return DATA_DIR / "webhooks.json"


def _load_webhooks() -> list:
    p = _webhooks_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_webhooks(webhooks: list) -> None:
    p = _webhooks_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(webhooks, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


@app.route("/api/webhooks", methods=["GET"])
def api_webhooks_list():
    client_filter = request.args.get("client_id", CLIENT_ID)
    webhooks = _load_webhooks()
    return jsonify([w for w in webhooks if w.get("client_id") == client_filter])


@app.route("/api/webhooks", methods=["POST"])
def api_webhooks_register():
    data = request.get_json(force=True) or {}
    if not data.get("url"):
        return jsonify({"error": "url is required"}), 400
    events = data.get("events", list(_VALID_EVENT_TYPES))
    invalid = set(events) - _VALID_EVENT_TYPES
    if invalid:
        return jsonify({"error": f"Invalid event types: {sorted(invalid)}"}), 422

    reg = {
        "id": str(uuid.uuid4()),
        "client_id": data.get("client_id", CLIENT_ID),
        "url": data["url"],
        "events": events,
        "secret": data.get("secret") or None,
        "description": data.get("description", ""),
        "active": True,
        "consecutive_failures": 0,
        "created_at": datetime.utcnow().isoformat(),
        "last_success_at": None,
        "last_failure_at": None,
    }
    webhooks = _load_webhooks()
    webhooks.append(reg)
    _save_webhooks(webhooks)
    return jsonify(reg), 201


@app.route("/api/webhooks/<webhook_id>", methods=["PATCH"])
def api_webhooks_update(webhook_id: str):
    data = request.get_json(force=True) or {}
    webhooks = _load_webhooks()
    for wh in webhooks:
        if wh.get("id") == webhook_id:
            for field in ("url", "events", "description", "active"):
                if field in data:
                    wh[field] = data[field]
            _save_webhooks(webhooks)
            return jsonify(wh)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/webhooks/<webhook_id>", methods=["DELETE"])
def api_webhooks_delete(webhook_id: str):
    webhooks = _load_webhooks()
    filtered = [w for w in webhooks if w.get("id") != webhook_id]
    if len(filtered) == len(webhooks):
        return jsonify({"error": "Not found"}), 404
    _save_webhooks(filtered)
    return "", 204
