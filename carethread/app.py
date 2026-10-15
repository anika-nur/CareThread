"""
app.py — CareThread web interface.

    python app.py      → http://127.0.0.1:5001

Routes:
    /                      coordinator dashboard, patients ranked by urgency
    /patient/<id>          one brief, every claim traceable to its source
    /api/brief/<id>        the annotated brief as JSON
"""

import json
import sqlite3
from pathlib import Path

from flask import Flask, abort, jsonify, render_template

from assemble_patient import assemble_patient_data
from provenance import annotate_brief
from verify_brief import verify_brief

app = Flask(__name__)

DB_PATH = "carethread.db"
BRIEFS_DIR = Path("briefs")

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_brief(patient_id):
    path = BRIEFS_DIR / f"{patient_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def brief_index():
    """Every brief on disk, with the numbers the dashboard ranks on."""
    conn = get_db()
    rows = []

    for path in BRIEFS_DIR.glob("*.json"):
        brief = json.loads(path.read_text())
        meta = brief.get("_meta", {})
        pid = meta.get("patient_id") or path.stem

        data = assemble_patient_data(conn, pid)
        if not data:
            continue   # brief for a patient no longer in the database

        gaps = data["care_gaps"]
        counts = {s: sum(1 for g in gaps if g["severity"] == s)
                  for s in ("high", "medium", "low")}
        report = verify_brief(brief, data)

        rows.append({
            "patient_id": pid,
            "name": data["demographics"]["name"],
            "age": data["demographics"]["age"],
            "gender": data["demographics"]["gender"],
            "gap_counts": counts,
            "gap_total": len(gaps),
            "alerts": len(data["drug_interactions"]),
            "major_alerts": sum(1 for ix in data["drug_interactions"]
                                if ix["severity"] == "major"),
            "verdict": report["verdict"],
            "issues": report["issues_found"],
            "model": meta.get("model", "—"),
        })

    conn.close()
    # Most urgent first: high gaps, then major interactions, then total gaps
    rows.sort(key=lambda r: (-r["gap_counts"]["high"], -r["major_alerts"],
                             -r["gap_total"]))
    return rows


@app.route("/")
def dashboard():
    rows = brief_index()
    totals = {
        "patients": len(rows),
        "high_gaps": sum(r["gap_counts"]["high"] for r in rows),
        "major_alerts": sum(r["major_alerts"] for r in rows),
        "flagged": sum(1 for r in rows if r["verdict"] != "PASS"),
    }
    return render_template("dashboard.html", rows=rows, totals=totals)


@app.route("/patient/<patient_id>")
def patient(patient_id):
    brief = load_brief(patient_id)
    if brief is None:
        abort(404, "No brief generated for this patient yet.")

    conn = get_db()
    data = assemble_patient_data(conn, patient_id)
    conn.close()
    if not data:
        abort(404, "Patient not in database.")

    return render_template(
        "brief.html",
        brief=annotate_brief(brief, data),
        report=verify_brief(brief, data),
        data=data,
    )


@app.route("/api/brief/<patient_id>")
def api_brief(patient_id):
    brief = load_brief(patient_id)
    if brief is None:
        return jsonify({"error": "no brief for this patient"}), 404

    conn = get_db()
    data = assemble_patient_data(conn, patient_id)
    conn.close()
    if not data:
        return jsonify({"error": "patient not in database"}), 404

    return jsonify({"brief": annotate_brief(brief, data),
                    "verification": verify_brief(brief, data)})


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", message=str(e)), 404


if __name__ == "__main__":
    if not Path(DB_PATH).exists():
        raise SystemExit(f"{DB_PATH} not found — run this from your project folder.")
    if not BRIEFS_DIR.exists():
        raise SystemExit("briefs/ not found — run run_pipeline.py first.")
    # Port 5000 is taken by macOS AirPlay Receiver, which answers 403.
    # debug stays off: its traceback console is a shell on this machine.
    app.run(debug=False, port=5001)
