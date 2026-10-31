"""
test_clinic_view.py — Prove the clinic-only comparison can find each group.

    python test_clinic_view.py

Across the 40 briefed patients "missed by the clinic" is 0, which on its own
can't tell a real zero from a comparison that never fires. Each case here
builds a one-patient database with a home clinic and a hospital, puts a
diabetes diagnosis and an HbA1c at one or the other, runs the real gap rules
and clinic_view.py on it, and checks which group the HbA1c gap lands in.

Needs no real data and writes only to a temporary file.
Exits 1 if any case behaves wrongly.
"""

import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from care_gaps import detect_gaps_for_patient
from clinic_view import build_clinic_tables, clinic_comparison, gap_key
from assemble_patient import get_care_gaps, get_drug_interactions
from parse_fhir import create_database

PID = "test-patient"
CLINIC, HOSPITAL = "org-clinic", "org-hospital"
GAP = "HbA1c"


def days_ago(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def build(path, diagnosed_at, hba1c_at=None):
    """A diabetic adult whose home clinic is CLINIC. The diagnosis, and the
    HbA1c if any, are recorded at the given organizations."""
    conn = create_database(path)
    conn.executemany("INSERT INTO organizations VALUES (?,?,?,?,?)", [
        (CLINIC, "TEST FAMILY CLINIC LLC", "Boston", "MA", CLINIC),
        (HOSPITAL, "TEST GENERAL HOSPITAL", "Boston", "MA", HOSPITAL)])
    conn.execute("INSERT INTO patients VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (PID, "Test", "Patient", "1970-01-01", "female",
                  "Boston", "MA", "02101", "", ""))
    encounters = {CLINIC: ("enc-clinic", "AMB", "General examination of patient (procedure)",
                           "162673000", days_ago(30), "wellness"),
                  HOSPITAL: ("enc-hospital", "EMER", "Emergency room admission (procedure)",
                             "50849002", days_ago(60), "emergency")}
    for org, (eid, cls, typ, code, date, role) in encounters.items():
        conn.execute("INSERT INTO encounters (patient_id, encounter_id, encounter_class, "
                     "encounter_type, encounter_code, start_date, end_date, "
                     "organization_id, role) VALUES (?,?,?,?,?,?,?,?,?)",
                     (PID, eid, cls, typ, code, date, date, org, role))
    conn.execute("INSERT INTO conditions (patient_id, code, system, display, onset_date, "
                 "abatement_date, clinical_status, encounter_id) VALUES (?,?,?,?,?,?,?,?)",
                 (PID, "44054006", "http://snomed.info/sct", "Diabetes mellitus type 2 (disorder)",
                  encounters[diagnosed_at][4], "", "active", encounters[diagnosed_at][0]))
    if hba1c_at:
        conn.execute("INSERT INTO observations (patient_id, code, system, display, value, "
                     "unit, effective_date, encounter_id) VALUES (?,?,?,?,?,?,?,?)",
                     (PID, "4548-4", "http://loinc.org", "Hemoglobin A1c", "6.8", "%",
                      encounters[hba1c_at][4], encounters[hba1c_at][0]))
    conn.execute("CREATE TABLE home_clinic (patient_id TEXT PRIMARY KEY, group_id TEXT, "
                 "visits INTEGER, last_visit TEXT, rule TEXT)")
    conn.execute("INSERT INTO home_clinic VALUES (?,?,?,?,?)",
                  (PID, CLINIC, 1, days_ago(30), "test"))

    # The full-record tables, as care_gaps.py and drug_interactions.py write them
    conn.execute("""CREATE TABLE care_gaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT, gap_type TEXT, severity TEXT,
        description TEXT, guideline TEXT,
        qualifying_condition TEXT, last_date TEXT, days_overdue INTEGER
    )""")
    conn.executemany(
        "INSERT INTO care_gaps (patient_id, gap_type, severity, description, guideline, "
        "qualifying_condition, last_date, days_overdue) VALUES (?,?,?,?,?,?,?,?)",
        [(g["patient_id"], g["gap_type"], g["severity"], g["description"], g["guideline"],
          g["qualifying_condition"], g.get("last_date"), g.get("days_overdue"))
         for g in detect_gaps_for_patient(conn, PID)])
    conn.execute("""CREATE TABLE drug_interactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT, drug_a TEXT, drug_b TEXT,
        drug_a_matched TEXT, drug_b_matched TEXT, severity TEXT
    )""")
    conn.commit()
    conn.close()
    build_clinic_tables(path)


def groups_with_gap(path):
    """The comparison groups the HbA1c gap lands in."""
    conn = sqlite3.connect(path)
    comp = clinic_comparison(conn, PID, get_care_gaps(conn, PID),
                             get_drug_interactions(conn, PID))
    conn.close()
    return {group for group in ("done_elsewhere", "missed", "history")
            if any(gap_key(g["gap"]) == GAP for g in comp[group])}


# (description, diagnosis at, HbA1c at, expected groups)
CASES = [
    ("diagnosed at the hospital, never tested", HOSPITAL, None, {"missed"}),
    ("diagnosed at the clinic, never tested", CLINIC, None, set()),
    ("diagnosed at the clinic, tested at the hospital", CLINIC, HOSPITAL, {"done_elsewhere"}),
]


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, (desc, diagnosed_at, hba1c_at, expected) in enumerate(CASES):
            path = str(Path(tmp) / f"case{i}.db")
            build(path, diagnosed_at, hba1c_at)
            got = groups_with_gap(path)
            ok = got == expected
            failures += not ok
            print(f"  {'✅' if ok else '❌'} {desc}: HbA1c gap in "
                  f"{sorted(got) or 'no group'} (expected {sorted(expected) or 'no group'})")
    print(f"\n{len(CASES) - failures}/{len(CASES)} cases correct")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
