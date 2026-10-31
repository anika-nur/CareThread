"""
clinic_view.py — What the home clinic would see from its own records alone.

The existing rules (care_gaps.py, drug_interactions.py) run unchanged on a
copy of the database that holds only the rows recorded at each patient's home
clinic (sources.py). Results go to care_gaps_clinic and
drug_interactions_clinic, beside the full-record tables.

Comparing the two gives three groups per patient:
  done elsewhere         gaps the clinic alone would chase: the screening or
                         test happened at another organization
  unknown to the clinic  interactions involving a prescription written
                         elsewhere
  missed by the clinic   gaps that only appear because a diagnosis or
                         finding was recorded elsewhere
  history elsewhere      gaps open in both views whose last-done date is
                         later in the full record: the test was done
                         elsewhere, just not recently enough

Usage:
    python clinic_view.py      # (re)build the two clinic tables
"""

import contextlib
import io
import sqlite3
import sys
from collections import Counter

from assemble_patient import get_care_gaps, get_drug_interactions
from care_gaps import detect_gaps_for_patient
from config import DB_PATH
from drug_interactions import (EXCLUDED_SEVERITIES, check_patient_interactions,
                               load_ddinter)
from sources import SourceIndex, gap_subject, org_display

# Clinical tables whose rows each belong to one encounter
RECORD_TABLES = ("conditions", "medications", "observations", "procedures",
                 "immunizations", "care_plan_activities")

GAP_TABLE = "care_gaps_clinic"
INTERACTION_TABLE = "drug_interactions_clinic"

NOTE = ("Source organizations come from Synthea's synthetic data; "
        "the home clinic is inferred from visit history.")


# ── Building the clinic tables ───────────────────────────────────────────

def clinic_only_db(db_path):
    """In-memory copy of the database holding only home-clinic rows: an
    encounter is kept when its organization is in the patient's home clinic
    group, and a record when its encounter is kept."""
    mem = sqlite3.connect(":memory:")
    mem.execute(f"ATTACH 'file:{db_path}?mode=ro' AS src")
    mem.execute("CREATE TABLE patients AS SELECT * FROM src.patients")
    mem.execute("""
        CREATE TABLE encounters AS
        SELECT e.* FROM src.encounters e
        JOIN src.organizations o ON o.organization_id = e.organization_id
        JOIN src.home_clinic h ON h.patient_id = e.patient_id
                              AND h.group_id = o.group_id""")
    for table in RECORD_TABLES:
        mem.execute(f"""
            CREATE TABLE {table} AS
            SELECT t.* FROM src.{table} t
            JOIN encounters e ON e.encounter_id = t.encounter_id""")
    for table in ("patients", "encounters", *RECORD_TABLES):
        mem.execute(f"CREATE INDEX idx_{table}_patient ON {table}(patient_id)")
    mem.commit()
    mem.execute("DETACH src")
    return mem


def build_clinic_tables(db_path=DB_PATH):
    """Run the gap and interaction rules on clinic-only records for every
    living patient and save the results beside the full-record tables."""
    clinic = clinic_only_db(db_path)
    patient_ids = [r[0] for r in clinic.execute(
        "SELECT patient_id FROM patients "
        "WHERE deceased_date = '' OR deceased_date IS NULL")]

    with contextlib.redirect_stdout(io.StringIO()):   # load_ddinter is chatty
        lookup, index = load_ddinter()

    gaps, interactions = [], []
    for pid in patient_ids:
        gaps.extend(detect_gaps_for_patient(clinic, pid))
        interactions.extend(ix for ix in check_patient_interactions(clinic, pid, lookup, index)
                            if ix["severity"] not in EXCLUDED_SEVERITIES)
    clinic.close()

    conn = sqlite3.connect(db_path)
    conn.execute(f"DROP TABLE IF EXISTS {GAP_TABLE}")
    conn.execute(f"""CREATE TABLE {GAP_TABLE} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT, gap_type TEXT, severity TEXT,
        description TEXT, guideline TEXT,
        qualifying_condition TEXT, last_date TEXT, days_overdue INTEGER
    )""")
    conn.executemany(
        f"INSERT INTO {GAP_TABLE} (patient_id, gap_type, severity, description, "
        "guideline, qualifying_condition, last_date, days_overdue) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(g["patient_id"], g["gap_type"], g["severity"], g["description"],
          g["guideline"], g["qualifying_condition"], g.get("last_date"),
          g.get("days_overdue")) for g in gaps])

    conn.execute(f"DROP TABLE IF EXISTS {INTERACTION_TABLE}")
    conn.execute(f"""CREATE TABLE {INTERACTION_TABLE} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT, drug_a TEXT, drug_b TEXT,
        drug_a_matched TEXT, drug_b_matched TEXT,
        severity TEXT
    )""")
    conn.executemany(
        f"INSERT INTO {INTERACTION_TABLE} (patient_id, drug_a, drug_b, "
        "drug_a_matched, drug_b_matched, severity) VALUES (?,?,?,?,?,?)",
        [(ix["patient_id"], ix["drug_a"], ix["drug_b"], ix["drug_a_matched"],
          ix["drug_b_matched"], ix["severity"]) for ix in interactions])
    for table in (GAP_TABLE, INTERACTION_TABLE):
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_patient "
                     f"ON {table}(patient_id)")
    conn.commit()
    conn.close()
    return len(gaps), len(interactions)


# ── Comparing the two views ──────────────────────────────────────────────

def has_clinic_view(conn):
    """True when this database was built with source tagging."""
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    return {"home_clinic", "organizations", GAP_TABLE, INTERACTION_TABLE} <= names


def gap_key(gap_type):
    """'Smoking Cessation Counseling Missing' and '... Overdue' are one gap
    in two states; compare gaps by what they are about."""
    return gap_subject(gap_type)


def ix_key(ix):
    return (ix["drug_a_generic"], ix["drug_b_generic"])


def home_clinic(conn, patient_id):
    row = conn.execute(
        "SELECT o.name, h.rule, h.last_visit FROM home_clinic h "
        "JOIN organizations o ON o.organization_id = h.group_id "
        "WHERE h.patient_id = ?", (patient_id,)).fetchone()
    return ({"name": org_display(row[0]), "rule": row[1], "last_visit": row[2]}
            if row else None)


def clinic_comparison(conn, patient_id, full_gaps, full_interactions, index=None):
    """The clinic-only gaps and interactions for one patient, and how they
    differ from the full record. Full lists come from assemble_patient_data;
    `index` is the patient's SourceIndex, built here if not passed."""
    index = index or SourceIndex(conn, patient_id)
    clinic_gaps = get_care_gaps(conn, patient_id, GAP_TABLE)
    clinic_ix = get_drug_interactions(conn, patient_id, INTERACTION_TABLE)

    full_by_key = {gap_key(g["gap"]): g for g in full_gaps}
    clinic_gap_keys = {gap_key(g["gap"]) for g in clinic_gaps}
    clinic_pairs = {ix_key(ix) for ix in clinic_ix}

    history = []   # (clinic gap, full-record last date, where it was done)
    for g in clinic_gaps:
        full = full_by_key.get(gap_key(g["gap"]))
        if full and full["last_date"] and full["last_date"] != g["last_date"]:
            history.append({"gap": g["gap"], "last_date": full["last_date"],
                            "source": index.gap(full["gap"], full["last_date"])})

    return {
        "home": home_clinic(conn, patient_id),
        "gaps": clinic_gaps,
        "interactions": clinic_ix,
        "done_elsewhere": [g for g in clinic_gaps if gap_key(g["gap"]) not in full_by_key],
        "history": history,
        "missed": [g for g in full_gaps if gap_key(g["gap"]) not in clinic_gap_keys],
        "unknown": [ix for ix in full_interactions if ix_key(ix) not in clinic_pairs],
    }


def totals(conn, patient_ids):
    """Counts per group across these patients, and how many patients each touches."""
    counts, patients = Counter(), Counter()
    for pid in patient_ids:
        full_gaps = get_care_gaps(conn, pid)
        full_ix = get_drug_interactions(conn, pid)
        comp = clinic_comparison(conn, pid, full_gaps, full_ix)
        for group in ("done_elsewhere", "unknown", "missed", "history"):
            counts[group] += len(comp[group])
            patients[group] += bool(comp[group])
        counts["unknown_major"] += sum(ix["severity"] == "major" for ix in comp["unknown"])
        patients["any"] += any(comp[g] for g in ("done_elsewhere", "unknown", "missed", "history"))
    return counts, patients


if __name__ == "__main__":
    n_gaps, n_ix = build_clinic_tables(sys.argv[1] if len(sys.argv) > 1 else DB_PATH)
    print(f"{GAP_TABLE}: {n_gaps} rows · {INTERACTION_TABLE}: {n_ix} rows")
