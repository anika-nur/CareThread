"""
app.py — CareThread web interface.

    python app.py      → http://127.0.0.1:5001

Routes:
    /                      coordinator dashboard: today's schedule, or the
                           whole living panel ranked by urgency
    /patient/<id>          one brief, every claim traceable to its source
                           (rule-based gaps and interactions if no brief yet)
    /api/brief/<id>        the annotated brief as JSON
    /api/feedback          POST a clinician's response to one brief item
    /feedback              feedback summary (demo pilot metric)
"""

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request
from markupsafe import Markup, escape

from assemble_patient import (assemble_patient_data, interaction_row, stale_cutoff,
                              valid_zip, INTERACTION_COLUMNS, INTERACTION_ORDER,
                              SENSITIVE_SOCIAL_CODES, SENSITIVE_SOCIAL_WORDS)
from provenance import (_condition_src, _gap_src, _ix_sources, _med_src,
                        annotate_brief)
from clinic_view import NOTE as SOURCE_NOTE, clinic_comparison, gap_key, has_clinic_view
from config import DB_PATH
from sources import SourceIndex
from schedule import PROVIDERS, display_time, load_schedule
from verify_brief import load_brief_file, verify_brief

app = Flask(__name__)

GUIDELINE_TERMS = {
    "USPSTF": "U.S. Preventive Services Task Force: national expert panel on screening",
    "HEDIS": "Quality measures used to grade health plans",
    "UDS": "HRSA's reporting system for health centers",
    "ACIP": "CDC's vaccine advisory committee",
}


@app.template_filter("guideline_tooltips")
def guideline_tooltips(value):
    """Keep the guideline wording, with definitions on its abbreviations."""
    parts = re.split(r"\b(USPSTF|HEDIS|UDS|ACIP)\b", value or "")
    return Markup("".join(
        f'<abbr title="{escape(GUIDELINE_TERMS[part])}">{part}</abbr>'
        if part in GUIDELINE_TERMS else str(escape(part)) for part in parts
    ))

BRIEFS_DIR = Path("briefs")

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Clinician feedback ───────────────────────────────────────────────────
# Append-only: every click is a row, and an item's state is its latest row.
# "cleared" records an un-click, so history survives a change of mind.

FEEDBACK_RESPONSES = {
    "addressed": "Addressed today",
    "not_applicable": "Not applicable",
    "done_elsewhere": "Already done elsewhere",
}
FEEDBACK_ITEM_TYPES = {"care_gap", "medication_alert"}
# Responses that mean the brief was wrong or incomplete for this patient
FEEDBACK_MISSES = ("not_applicable", "done_elsewhere")


def init_feedback(conn):
    """carethread.db gets rebuilt by the pipeline; recreate the table on use."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id  TEXT NOT NULL,
            item_type   TEXT NOT NULL,
            item_text   TEXT NOT NULL,
            response    TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )""")


def current_feedback(conn, patient_id=None):
    """Latest response per (patient, item), skipping cleared ones."""
    init_feedback(conn)
    where = "WHERE patient_id = ?" if patient_id else ""
    rows = conn.execute(f"""
        SELECT f.* FROM feedback f
        JOIN (SELECT MAX(id) AS id FROM feedback {where}
              GROUP BY patient_id, item_type, item_text) latest
          ON f.id = latest.id
        WHERE f.response != 'cleared'
        ORDER BY f.id DESC""", (patient_id,) if patient_id else ()).fetchall()
    return [dict(r) for r in rows]


def display_name(raw):
    """Synthea suffixes every name part with digits ("Carlita746 Pollich983").
    Strip them for display only; the database and briefs keep the raw form."""
    return " ".join(re.sub(r"\d+$", "", part) for part in str(raw).split())


def mrn(patient_id):
    """Short chart-style ID. The UUID is random, so its first six hex digits
    are a stable, deterministic stand-in for a clinic MRN."""
    return "MRN " + patient_id.replace("-", "")[:6].upper()


def with_display_names(brief, raw_name):
    """Swap the raw Synthea name for the display name in the rendered brief.
    Works on the annotated copy, so the stored brief JSON is untouched.
    Only whole raw name parts are replaced — a blanket digit strip would turn
    'vitamin B12' into 'vitamin B'."""
    parts = [p for p in str(raw_name).split() if re.search(r"\d$", p)]
    if not parts:
        return brief
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, parts)) + r")\b")

    def clean(text):
        return pattern.sub(lambda m: display_name(m.group(1)), text)

    brief["meta"] = {**brief["meta"], "patient_name": display_name(raw_name)}
    for key in ("summary", "recent_activity"):
        brief[key] = [{**seg, "text": clean(seg["text"])} for seg in brief[key]]
    brief["priority_actions"] = [{**a, "text": clean(a["text"])}
                                 for a in brief["priority_actions"]]
    return brief


def sentence_case(label):
    """'Depression Follow-Up Missing' → 'Depression follow-up missing'.
    Only title-case words are lowered, so HbA1c and BMI survive."""
    first, *rest = label.split()
    return " ".join([first] + ["-".join(p.lower() if p.istitle() else p
                                        for p in word.split("-"))
                               for word in rest])


def urgency_reason(data):
    """One line on why a patient ranks where they do: their top gap
    (high-severity if any) and their first major interaction. A current
    major interaction (both drugs prescribed in the last 5 years) wins over
    one that involves an old prescription."""
    parts = []
    gaps = data["care_gaps"]   # already ordered high → low
    if gaps:
        parts.append(sentence_case(gaps[0]["gap"]))
    majors = [ix for ix in data["drug_interactions"] if ix["severity"] == "major"]
    major = next((ix for ix in majors if ix["current"]), None) or next(iter(majors), None)
    if major:
        parts.append(f"{major['drug_a_generic'].lower()} + "
                     f"{major['drug_b_generic'].lower()} "
                     f"({'major' if major['current'] else 'major, to confirm'})")
    return " · ".join(parts)


def confirm_note(ix):
    """'clopidogrel last prescribed 2015' for each old drug in an interaction."""
    return "; ".join(f"{ix[f'drug_{s}_generic']} last prescribed {ix[f'drug_{s}_confirm']}"
                     for s in "ab" if ix[f"drug_{s}_confirm"])


def ix_drug_tags(ix):
    """[(drug, year or None)] for an interaction row: the year marks a drug
    last prescribed over 5 years ago and never re-prescribed."""
    return [(ix["drug_a_generic"], ix["drug_a_confirm"]),
            (ix["drug_b_generic"], ix["drug_b_confirm"])]


def alert_drug_tags(alert, data):
    """The same tags for a brief's medication alert, as (tags, extra): the
    alert's drug names are the model's, the years come from the interaction
    row it traced to. `extra` holds old drugs no alert name matched."""
    src = next((s for s in alert["sources"] if s["table"] == "drug_interactions"), None)
    ix = next((ix for ix in data["drug_interactions"] if src and src["label"] ==
               f"{ix['drug_a_generic']} ↔ {ix['drug_b_generic']}"), None)
    if not ix:
        return [(d, None) for d in alert.get("drugs", [])], []
    tags, unmatched = [], dict(ix_drug_tags(ix))
    for drug in alert.get("drugs", []):
        generic = next((g for g in unmatched
                        if g.lower() in drug.lower() or drug.lower() in g.lower()), None)
        tags.append((drug, unmatched.pop(generic) if generic else None))
    # An old drug the model named differently still gets its tag, by generic name
    return tags, [(g, year) for g, year in unmatched.items() if year]


GAP_SUFFIXES = (" Overdue", " Missing", " Due")

# What a gap's last_date means, when it isn't "the last time this was done"
LAST_DATE_LABEL = {"Depression Follow-Up Missing": "positive screen"}

SEARCH_NAMES = {
    "Blood Pressure": "blood pressure measurement",
    "Cervical Screening": "cervical screening",
    "Diabetic Eye Exam": "diabetic eye exam",
    "Flu Vaccine": "flu vaccine",
    "Lipid Panel": "lipid panel",
    "Mammogram": "mammogram",
    "Nephropathy Screening": "nephropathy screening",
    "Smoking Cessation Counseling": "smoking cessation counseling",
    "Annual Wellness Visit": "annual wellness visit",
}


def never_gap_note(gap, organization_count):
    """Explain a missing-record result using this rule's stored eligibility."""
    subject = gap["gap"]
    for suffix in GAP_SUFFIXES:
        subject = subject.removesuffix(suffix)
    test = SEARCH_NAMES.get(subject, subject).lower()
    reason = (gap.get("condition") or "the rule's eligibility criteria").strip()
    reason = re.sub(r"\s*\(screening age range\)$", "", reason, flags=re.I)
    reason = reason[:1].lower() + reason[1:]
    guideline = gap.get("guideline") or ""
    age_range = re.search(r"\b(?:ages?|women)\s+(\d+)\s*[-–]\s*(\d+)\b", guideline, re.I)
    if age_range and re.search(r"\bage\s+\d+\b", reason, re.I):
        reason += f", in the {age_range.group(1)}–{age_range.group(2)} range"
    scope = (f"all records from {organization_count} "
             f"organization{'s' if organization_count != 1 else ''}"
             if organization_count is not None else "all available records")
    return f"Searched {scope} for {test}; none found. Rule applies because: {reason}."


def add_never_gap_notes(rows, gaps, organization_count):
    by_type = {g["gap"]: g for g in gaps}
    for row in rows:
        gap = by_type.get(row.get("gap_type"))
        if gap and row["when"] == "never on record":
            row["search_note"] = never_gap_note(gap, organization_count)
    return rows


def gap_rows(brief, data):
    """Compact care-gap rows for the brief page. Name, last date and
    guideline come from the care_gaps row the gap traced to; the model's
    full sentence and action ride along for the source panel."""
    by_type = {g["gap"]: g for g in data["care_gaps"]}
    rows = []
    for g in brief["care_gaps"]:
        src = next((s for s in g["sources"] if s["table"] == "care_gaps"), None)
        db = by_type.get(src["label"]) if src else None
        gap_type = db["gap"] if db else g.get("gap", "").split(":")[0]
        for suffix in GAP_SUFFIXES:
            gap_type = gap_type.removesuffix(suffix)

        if db and db.get("last_date"):
            label = LAST_DATE_LABEL.get(db["gap"], "last done")
            when = f"{label} {db['last_date'][:10]}"
        elif db:
            when = "never on record"
        else:
            when = ""

        rows.append({
            "gap_type": db["gap"] if db else None,
            "severity": g.get("severity", ""),
            "name": sentence_case(gap_type) if gap_type else g.get("gap", ""),
            "when": when,
            "last_done": bool(db and db.get("last_date") and
                              db["gap"] not in LAST_DATE_LABEL),
            "guideline": (db or {}).get("guideline") or g.get("source", ""),
            "sources": g["sources"],
            "note": [t for t in (g.get("gap"), g.get("action")) if t],
        })
    return rows


def social_items(brief):
    """Social history rows for the page, each marked sensitive or not.
    Briefs from before the SDOH split only have sdoh_flags; show those too."""
    legacy = not brief["social_history"]
    items = brief["social_history"] or [{"text": f, "sources": []}
                                         for f in brief["sdoh_flags"]]
    for item in items:
        codes = {str(s.get("code")) for s in item["sources"]}
        item["sensitive"] = bool(codes & SENSITIVE_SOCIAL_CODES) or any(
            w in item["text"].lower() for w in SENSITIVE_SOCIAL_WORDS)
    return items, legacy


def stale_meds(brief, data):
    """Brief medication index → year started, for meds the record flags as
    started long ago and never re-prescribed ('confirm with patient')."""
    flagged = {m["medication"]: m["prescribed"][:4] for m in data["medications"]
               if m["confirm_with_patient"]}
    out = {}
    for i, item in enumerate(brief["current_medications"]):
        year = next((flagged[s["label"]] for s in item["sources"]
                     if s["table"] == "medications" and s["label"] in flagged), None)
        if year:
            out[i] = year
    return out


# Short labels for the neighborhood table, in display order
SDOH_TABLE_LABELS = {
    "pct_poverty":               "Below the federal poverty line",
    "median_household_income":   "Median household income",
    "pct_uninsured":             "No health insurance",
    "pct_unemployed":            "Unemployed (labor force)",
    "pct_public_assistance":     "Households on public assistance or SNAP",
    "pct_less_than_high_school": "Adults 25+ without a high school diploma",
    "pct_no_vehicle":            "Households with no vehicle",
    "pct_rent_burdened":         "Renters paying 30%+ of income on rent",
}


def sdoh_table(sdoh):
    """(label, value) rows from the sdoh table: one decimal for percentages,
    income to the nearest $1,000."""
    rows = []
    for key, label in SDOH_TABLE_LABELS.items():
        v = (sdoh or {}).get(key)
        if v is None:
            continue
        value = (f"${round(float(v), -3):,.0f}" if key == "median_household_income"
                 else f"{float(v):.1f}%")
        rows.append((label, value))
    return rows


# Decimal places for tests where magnitude alone gives the wrong precision
LAB_DECIMALS = {"body mass index": 1, "bmi": 1, "temperature": 1}


@app.template_filter("lab_round")
def lab_round(value, label=""):
    """Round a lab value for display: 2.6778 → 2.68, 84.163 → 84.
    Never adds digits the record didn't have (pain score 0 stays 0)."""
    text = str(value).strip()
    try:
        v = float(text)
    except ValueError:
        return value
    test = str(label).lower()
    dp = next((d for key, d in LAB_DECIMALS.items() if key in test), None)
    if dp is None:
        dp = 0 if abs(v) >= 20 else 1 if abs(v) >= 3 else 2
    recorded = len(text.split(".")[1]) if "." in text else 0
    return f"{v:.{min(dp, recorded)}f}"


def load_brief(patient_id):
    path = BRIEFS_DIR / f"{patient_id}.json"
    if not path.exists():
        return None
    return load_brief_file(path)


# ── Panel and verification cache ─────────────────────────────────────────

# Clinical tables assemble_patient_data reads one patient at a time
PATIENT_TABLES = ("conditions", "medications", "observations", "encounters",
                  "immunizations", "care_gaps", "drug_interactions")

# A cached verdict is stale once either of these changes
VERIFIER_FILES = ("verify_brief.py", "assemble_patient.py")

GAP_ORDER_SQL = ("CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 "
                 "WHEN 'low' THEN 3 ELSE 4 END")


def init_db(conn):
    """Indexes on patient_id (without them, assembling one patient scans
    every clinical table, ~50 ms) and the verification cache. carethread.db
    gets rebuilt by the pipeline, so both are recreated on use."""
    for table in PATIENT_TABLES:
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_patient "
                     f"ON {table}(patient_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS verification_cache (
            patient_id    TEXT PRIMARY KEY,
            cache_key     TEXT NOT NULL,
            verdict       TEXT NOT NULL,
            issues_found  INTEGER NOT NULL,
            model         TEXT NOT NULL
        )""")


def record_signature(conn):
    """Changes when the clinical tables are rebuilt or appended to.
    MAX(rowid) is a single index lookup; COUNT(*) would scan."""
    return ",".join(str(conn.execute(f"SELECT MAX(rowid) FROM {t}").fetchone()[0])
                    for t in ("patients", *PATIENT_TABLES, "sdoh"))


def cached_verification(conn, patient_ids):
    """Verdict per brief. verify_brief only re-runs when the brief file, the
    verifier, or the record changed since the cached result was stored."""
    code = ":".join(str(Path(f).stat().st_mtime_ns) for f in VERIFIER_FILES)
    signature = record_signature(conn)
    cached = {r["patient_id"]: dict(r)
              for r in conn.execute("SELECT * FROM verification_cache")}

    out = {}
    for pid in patient_ids:
        stat = (BRIEFS_DIR / f"{pid}.json").stat()
        key = f"{stat.st_mtime_ns}:{stat.st_size}:{code}:{signature}"
        row = cached.get(pid)
        if not row or row["cache_key"] != key:
            brief = load_brief(pid)
            report = verify_brief(brief, assemble_patient_data(conn, pid))
            row = {"patient_id": pid, "cache_key": key,
                   "verdict": report["verdict"],
                   "issues_found": report["issues_found"],
                   "model": brief.get("_meta", {}).get("model") or "—"}
            conn.execute("INSERT OR REPLACE INTO verification_cache VALUES "
                         "(:patient_id, :cache_key, :verdict, :issues_found, :model)",
                         row)
        out[pid] = row
    conn.commit()
    return out


def age_on(birth_date, today):
    born = datetime.strptime(birth_date, "%Y-%m-%d").date()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def panel_index():
    """Every living patient, with the numbers the panel ranks on, read in
    bulk from care_gaps and drug_interactions. No brief needed to be listed."""
    conn = get_db()
    init_db(conn)

    gaps, interactions = {}, {}
    for r in conn.execute("SELECT patient_id, gap_type, severity FROM care_gaps "
                          f"ORDER BY {GAP_ORDER_SQL}, id"):
        gaps.setdefault(r["patient_id"], []).append(
            {"gap": r["gap_type"], "severity": r["severity"]})
    cutoff = stale_cutoff()
    for r in conn.execute(f"SELECT {INTERACTION_COLUMNS} FROM drug_interactions di "
                          f"ORDER BY {INTERACTION_ORDER}, di.id"):
        interactions.setdefault(r[0], []).append(interaction_row(r, cutoff))

    patients = conn.execute("""
        SELECT patient_id, given_name, family_name, birth_date, gender
        FROM patients WHERE deceased_date = '' OR deceased_date IS NULL
    """).fetchall()
    on_disk = {p.stem for p in BRIEFS_DIR.glob("*.json")}
    verified = cached_verification(
        conn, [p["patient_id"] for p in patients if p["patient_id"] in on_disk])
    conn.close()

    today = datetime.now().date()
    rows = []
    for p in patients:
        pid = p["patient_id"]
        data = {"care_gaps": gaps.get(pid, []),
                "drug_interactions": interactions.get(pid, [])}
        counts = {s: sum(1 for g in data["care_gaps"] if g["severity"] == s)
                  for s in ("high", "medium", "low")}
        check = verified.get(pid)
        majors = [ix for ix in data["drug_interactions"] if ix["severity"] == "major"]
        rows.append({
            "patient_id": pid,
            "name": display_name(f"{p['given_name']} {p['family_name']}"),
            "mrn": mrn(pid),
            "birth_date": p["birth_date"],
            "age": age_on(p["birth_date"], today),
            "gender": p["gender"],
            "gap_counts": counts,
            "gap_total": len(data["care_gaps"]),
            "alerts": len(data["drug_interactions"]),
            # Only current majors rank; ones involving an old prescription
            # are counted apart as "to confirm"
            "major_alerts": sum(1 for ix in majors if ix["current"]),
            "major_to_confirm": sum(1 for ix in majors if not ix["current"]),
            "confirm_note": "; ".join(confirm_note(ix) for ix in majors
                                      if not ix["current"]),
            "has_brief": check is not None,
            "verdict": check["verdict"] if check else None,
            "issues": check["issues_found"] if check else None,
            "model": check["model"] if check else "—",
            "reason": urgency_reason(data),
        })

    # Most urgent first: high gaps, then major interactions, then total gaps
    rows.sort(key=lambda r: (-r["gap_counts"]["high"], -r["major_alerts"],
                             -r["gap_total"]))
    return rows


# Panel filters: key → (label, test)
PANEL_FILTERS = {
    "all":   ("All patients", lambda r: True),
    "high":  ("Has high-severity gap", lambda r: r["gap_counts"]["high"] > 0),
    "major": ("Has current major interaction", lambda r: r["major_alerts"] > 0),
    "clear": ("No open gaps", lambda r: r["gap_total"] == 0),
}


@app.route("/")
def dashboard():
    rows = panel_index()
    for r in rows:
        r["filters"] = " ".join(k for k, (_, test) in PANEL_FILTERS.items() if test(r))
    briefed = [r for r in rows if r["has_brief"]]
    filters = [(k, label, sum(1 for r in rows if test(r)))
               for k, (label, test) in PANEL_FILTERS.items()]
    totals = {
        "patients": len(rows),
        "high_gap_patients": sum(1 for r in rows if r["gap_counts"]["high"]),
        "major_patients": sum(1 for r in rows if r["major_alerts"]),
        "briefs": len(briefed),
        "flagged": sum(1 for r in briefed if r["verdict"] != "PASS"),
    }
    models = sorted({r["model"] for r in briefed if r["model"] != "—"})

    # Only patients with a brief are on today's (simulated) schedule
    appts = load_schedule([r["patient_id"] for r in briefed])
    for r in briefed:
        r["appt_time"] = appts[r["patient_id"]]["time"]
        r["appt_display"] = display_time(r["appt_time"])
        r["visit_type"] = appts[r["patient_id"]]["visit_type"]
        r["provider"] = appts[r["patient_id"]]["provider"]

    provider = request.args.get("provider")
    if provider not in PROVIDERS:
        provider = None
    # Within a slot, list providers in a fixed order
    schedule = sorted((r for r in briefed if not provider or r["provider"] == provider),
                      key=lambda r: (r["appt_time"], PROVIDERS.index(r["provider"])))

    view = "urgency" if request.args.get("view") == "urgency" else "schedule"
    return render_template("dashboard.html", rows=rows, schedule=schedule,
                           view=view, totals=totals, models=", ".join(models),
                           providers=PROVIDERS, provider=provider,
                           filters=filters, page_size=50)


def record_gap_rows(data):
    """Care-gap rows straight from the care_gaps table, for patients
    without a brief. Same shape as gap_rows()."""
    rows = []
    for g in data["care_gaps"]:
        gap_type = g["gap"]
        for suffix in GAP_SUFFIXES:
            gap_type = gap_type.removesuffix(suffix)
        if g.get("last_date"):
            when = f"{LAST_DATE_LABEL.get(g['gap'], 'last done')} {g['last_date'][:10]}"
        else:
            when = "never on record"
        rows.append({
            "gap_type": g["gap"],
            "severity": g["severity"],
            "name": sentence_case(gap_type),
            "when": when,
            "last_done": bool(g.get("last_date") and g["gap"] not in LAST_DATE_LABEL),
            "guideline": g.get("guideline") or "",
            "sources": [_gap_src(g)],
            "note": [],
        })
    return rows


def rule_alert(ix, data, frag=None):
    """An interaction row as an alert item, straight from the rules."""
    return {"severity": ix["severity"], "drug_tags": ix_drug_tags(ix),
            "sources": _ix_sources(ix, data), "frag": frag}


def record_page(data, index=None):
    """Detail page for a patient without a brief: the rule-based gaps and
    interactions, each with its source rows. No LLM involved. With a
    SourceIndex, source rows also say which organization they came from."""
    alerts = [rule_alert(ix, data) for ix in data["drug_interactions"]]
    gaps = add_never_gap_notes(record_gap_rows(data), data["care_gaps"],
                               index.organization_count if index else None)
    medications = [{"name": m["medication"], "prescribed": m["prescribed"],
                    "confirm": m["prescribed"][:4] if m["confirm_with_patient"] else None,
                    "sources": [_med_src(m)]} for m in data["medications"]]
    if index:
        for item in gaps + alerts + medications:
            label_sources(item["sources"], index)
    return render_template(
        "record.html",
        data=data,
        name=display_name(data["demographics"]["name"]),
        mrn=mrn(data["patient_id"]),
        gaps=gaps,
        conditions=[{"name": c["condition"], "since": c["since"],
                     "sources": [_condition_src(c)]} for c in data["conditions"]],
        medications=medications,
        major_alerts=[a for a in alerts if a["severity"] == "major"],
        other_alerts=[a for a in alerts if a["severity"] != "major"],
    )


# Groups in the clinic-only vs CareThread comparison (clinic_view.py)
FRAG_TAGS = {
    "done_elsewhere": "done elsewhere",
    "unknown": "unknown to the clinic",
    "missed": "missed by the clinic",
    "history": "history elsewhere",
    "outside": "not in clinic records",
}


def ix_label(ix):
    """The label provenance gives an interaction row's source."""
    return f"{ix['drug_a_generic']} ↔ {ix['drug_b_generic']}"


def tag_carethread_view(frag, gaps, alerts):
    """In the all-sources view, mark the brief's gaps the clinic alone would
    miss and its alerts that involve an outside prescription."""
    missed = {gap_key(g["gap"]) for g in frag["missed"]}
    for row in gaps:
        row["frag"] = ("missed" if row["gap_type"] and gap_key(row["gap_type"]) in missed
                       else None)
    unknown = {ix_label(ix) for ix in frag["unknown"]}
    for a in alerts:
        src = next((s for s in a["sources"] if s["table"] == "drug_interactions"), None)
        a["frag"] = "unknown" if src and src["label"] in unknown else None


def clinic_view_alerts(frag, data):
    """Clinic-record alerts, with outside-only alerts marked for comparison."""
    return ([rule_alert(ix, data) for ix in frag["interactions"]] +
            [rule_alert(ix, data, "unknown") for ix in frag["unknown"]])


def label_sources(sources, index):
    """Add the organization and role a medication or care-gap source row
    came from, for the source panel."""
    for s in sources:
        if s["table"] == "medications":
            origin = index.medication(s["label"], s["date"])
        elif s["table"] == "care_gaps":
            origin = index.gap(s["label"], s["date"])
        else:
            continue
        if origin:
            s["org"], s["role"] = origin["org"], origin["role"]
            s["short_org"] = origin["short_org"]
            s["at_home"] = origin["at_home"]


def tag_outside_meds(medications, index):
    """For the clinic-only view: the brief's medications with no active
    prescription in the home clinic's records, with where they were written."""
    for m in medications:
        src = next((s for s in m["sources"] if s["table"] == "medications"), None)
        if src and src["label"] not in index.clinic_meds:
            origin = index.medication(src["label"], src["date"])
            m["outside_org"] = origin["org"] if origin else "another organization"


@app.route("/patient/<patient_id>")
def patient(patient_id):
    conn = get_db()
    try:
        return patient_page(conn, patient_id)
    finally:
        conn.close()


def patient_page(conn, patient_id):
    """The brief page. The connection stays open while it renders: source
    labels are looked up row by row."""
    brief = load_brief(patient_id)
    init_db(conn)
    data = assemble_patient_data(conn, patient_id)
    feedback = {(f["item_type"], f["item_text"]): f["response"]
                for f in current_feedback(conn, patient_id)}
    conn.commit()
    if not data:
        abort(404, "Patient not in database.")
    index = SourceIndex(conn, patient_id) if has_clinic_view(conn) else None
    frag = (clinic_comparison(conn, patient_id, data["care_gaps"],
                              data["drug_interactions"], index)
            if index else None)
    if brief is None:
        return record_page(data, index)

    annotated = with_display_names(annotate_brief(brief, data),
                                   data["demographics"]["name"])
    for a in annotated["medication_alerts"]:
        a["drug_tags"], a["extra_tags"] = alert_drug_tags(a, data)
    appt = load_schedule([patient_id])[patient_id]
    social, social_legacy = social_items(annotated)

    clinic_only = bool(frag) and request.args.get("view") == "clinic"
    if clinic_only:
        gaps = []
        alerts = clinic_view_alerts(frag, data)
        tag_outside_meds(annotated["current_medications"], index)
    else:
        gaps = add_never_gap_notes(gap_rows(annotated, data), data["care_gaps"],
                                   index.organization_count if index else None)
        alerts = annotated["medication_alerts"]
        if frag:
            tag_carethread_view(frag, gaps, alerts)
    if index:
        for item in gaps + alerts + annotated["current_medications"]:
            label_sources(item["sources"], index)
    return render_template(
        "brief.html",
        brief=annotated,
        report=verify_brief(brief, data),
        data=data,
        mrn=mrn(patient_id),
        appt={"time": display_time(appt["time"]),
              "visit_type": appt["visit_type"], "provider": appt["provider"]},
        stale_meds=stale_meds(annotated, data),
        gaps=gaps,
        major_alerts=[a for a in alerts if a.get("severity") == "major"],
        other_alerts=[a for a in alerts if a.get("severity") != "major"],
        frag=frag,
        clinic_only=clinic_only,
        frag_tags=FRAG_TAGS,
        source_note=SOURCE_NOTE,
        social=social,
        social_legacy=social_legacy,
        zip_ok=valid_zip(data["demographics"]["zip_code"]),
        sdoh_rows=sdoh_table(data["sdoh"]),
        feedback=feedback,
        feedback_responses=FEEDBACK_RESPONSES,
    )


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    body = request.get_json(silent=True)   # JSON only: a plain form post can't land here
    if not isinstance(body, dict):
        return jsonify({"error": "expected a JSON body"}), 400

    pid = str(body.get("patient_id", ""))
    item_type = body.get("item_type")
    item_text = str(body.get("item_text", "")).strip()
    response = body.get("response")

    if load_brief(pid) is None:
        return jsonify({"error": "no brief for this patient"}), 404
    if item_type not in FEEDBACK_ITEM_TYPES or not item_text or len(item_text) > 500:
        return jsonify({"error": "bad item"}), 400
    if response not in FEEDBACK_RESPONSES and response != "cleared":
        return jsonify({"error": "bad response"}), 400

    conn = get_db()
    init_feedback(conn)
    conn.execute(
        "INSERT INTO feedback (patient_id, item_type, item_text, response, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, item_type, item_text, response,
         datetime.now(timezone.utc).isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "response": response})


@app.route("/feedback")
def feedback_summary():
    conn = get_db()
    items = current_feedback(conn)
    conn.commit()
    names = {r["patient_id"]: display_name(f"{r['given_name']} {r['family_name']}")
             for r in conn.execute("SELECT patient_id, given_name, family_name "
                                   "FROM patients")} if items else {}
    conn.close()

    counts = {key: sum(1 for f in items if f["response"] == key)
              for key in FEEDBACK_RESPONSES}
    by_type = {t: {key: sum(1 for f in items if f["item_type"] == t
                            and f["response"] == key)
                   for key in FEEDBACK_RESPONSES}
               for t in sorted(FEEDBACK_ITEM_TYPES)}
    misses = sum(counts[k] for k in FEEDBACK_MISSES)
    for f in items:
        f["patient_name"] = names.get(f["patient_id"], f["patient_id"])
    return render_template(
        "feedback.html", counts=counts, by_type=by_type, total=len(items),
        misses=misses, miss_pct=round(100 * misses / len(items)) if items else None,
        recent=items[:25], labels=FEEDBACK_RESPONSES)


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
