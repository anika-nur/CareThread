"""
assemble_patient.py — Gathers all clinical data for one patient into a
structured dict that can be passed to the LLM prompt.

Usage:
    # As a module:
    from assemble_patient import assemble_patient_data
    data = assemble_patient_data(conn, patient_id)

    # Standalone test:
    python assemble_patient.py
"""

import sqlite3
import json
from datetime import datetime


def get_patient_demographics(conn, patient_id):
    """Basic patient info."""
    row = conn.execute(
        "SELECT given_name, family_name, birth_date, gender, "
        "city, state, zip_code, marital_status "
        "FROM patients WHERE patient_id=?",
        (patient_id,)
    ).fetchone()
    if not row:
        return None

    birth = datetime.strptime(row[2], "%Y-%m-%d")
    age = datetime.now().year - birth.year - (
        (datetime.now().month, datetime.now().day) < (birth.month, birth.day)
    )

    return {
        "name": f"{row[0]} {row[1]}",
        "age": age,
        "birth_date": row[2],
        "gender": row[3],
        "city": row[4],
        "state": row[5],
        "zip_code": row[6],
        "marital_status": row[7],
    }


def get_active_conditions(conn, patient_id):
    """Active diagnoses with onset dates."""
    rows = conn.execute(
        "SELECT display, onset_date, code FROM conditions "
        "WHERE patient_id=? AND clinical_status='active' "
        "ORDER BY onset_date",
        (patient_id,)
    ).fetchall()
    return [
        {"condition": r[0], "since": r[1], "snomed_code": r[2]}
        for r in rows
    ]


def get_active_medications(conn, patient_id):
    """Currently active medications."""
    rows = conn.execute(
        "SELECT display, authored_on, code FROM medications "
        "WHERE patient_id=? AND status='active' "
        "ORDER BY authored_on DESC",
        (patient_id,)
    ).fetchall()
    return [
        {"medication": r[0], "prescribed": r[1], "rxnorm_code": r[2]}
        for r in rows
    ]


def get_recent_encounters(conn, patient_id, limit=10):
    """Most recent encounters."""
    rows = conn.execute(
        "SELECT encounter_class, encounter_type, start_date, end_date "
        "FROM encounters WHERE patient_id=? "
        "ORDER BY start_date DESC LIMIT ?",
        (patient_id, limit)
    ).fetchall()
    return [
        {"class": r[0], "type": r[1], "date": r[2], "end_date": r[3]}
        for r in rows
    ]


def get_recent_labs(conn, patient_id, limit=20):
    """Most recent lab results (observations with numeric values)."""
    rows = conn.execute(
        "SELECT display, value, unit, effective_date, code FROM observations "
        "WHERE patient_id=? AND value != '' AND effective_date != '' "
        "ORDER BY effective_date DESC LIMIT ?",
        (patient_id, limit)
    ).fetchall()
    return [
        {"test": r[0], "value": r[1], "unit": r[2], "date": r[3],
         "loinc_code": r[4]}
        for r in rows
    ]


def get_care_gaps(conn, patient_id):
    """Detected care gaps."""
    rows = conn.execute(
        "SELECT gap_type, severity, description, guideline, "
        "qualifying_condition, last_date, days_overdue "
        "FROM care_gaps WHERE patient_id=? "
        "ORDER BY CASE severity "
        "  WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 "
        "  ELSE 4 END",
        (patient_id,)
    ).fetchall()
    return [
        {"gap": r[0], "severity": r[1], "description": r[2],
         "guideline": r[3], "condition": r[4], "last_date": r[5],
         "days_overdue": r[6]}
        for r in rows
    ]


def get_drug_interactions(conn, patient_id):
    """Detected drug-drug interactions."""
    rows = conn.execute(
        "SELECT drug_a, drug_b, drug_a_matched, drug_b_matched, severity "
        "FROM drug_interactions WHERE patient_id=? "
        "ORDER BY CASE severity "
        "  WHEN 'major' THEN 1 WHEN 'moderate' THEN 2 WHEN 'minor' THEN 3 "
        "  ELSE 4 END",
        (patient_id,)
    ).fetchall()
    return [
        {"drug_a": r[0], "drug_b": r[1],
         "drug_a_generic": r[2], "drug_b_generic": r[3],
         "severity": r[4]}
        for r in rows
    ]


def get_sdoh_data(conn, patient_id):
    """Community-level SDOH indicators for the patient's zip code."""
    # Get available SDOH columns dynamically
    cols_info = conn.execute("PRAGMA table_info(sdoh)").fetchall()
    sdoh_cols = [r[1] for r in cols_info if r[1] != "zip_code"]

    if not sdoh_cols:
        return None

    select_cols = ", ".join(f"s.{c}" for c in sdoh_cols)
    row = conn.execute(
        f"SELECT {select_cols} FROM patients p "
        f"JOIN sdoh s ON p.zip_code = s.zip_code "
        f"WHERE p.patient_id=?",
        (patient_id,)
    ).fetchone()

    if not row:
        return None

    return {sdoh_cols[i]: row[i] for i in range(len(sdoh_cols))}


def get_recent_immunizations(conn, patient_id, limit=10):
    """Recent immunizations."""
    rows = conn.execute(
        "SELECT display, occurrence_date FROM immunizations "
        "WHERE patient_id=? AND status='completed' "
        "ORDER BY occurrence_date DESC LIMIT ?",
        (patient_id, limit)
    ).fetchall()
    return [
        {"vaccine": r[0], "date": r[1]}
        for r in rows
    ]


def assemble_patient_data(conn, patient_id):
    """Assemble all data for one patient into a single structured dict."""
    demographics = get_patient_demographics(conn, patient_id)
    if not demographics:
        return None

    return {
        "patient_id": patient_id,
        "demographics": demographics,
        "conditions": get_active_conditions(conn, patient_id),
        "medications": get_active_medications(conn, patient_id),
        "recent_encounters": get_recent_encounters(conn, patient_id),
        "recent_labs": get_recent_labs(conn, patient_id),
        "immunizations": get_recent_immunizations(conn, patient_id),
        "care_gaps": get_care_gaps(conn, patient_id),
        "drug_interactions": get_drug_interactions(conn, patient_id),
        "sdoh": get_sdoh_data(conn, patient_id),
    }


# ── Standalone test ──

if __name__ == "__main__":
    conn = sqlite3.connect("carethread.db")

    # Pick the patient with the most care gaps
    top = conn.execute("""
        SELECT p.patient_id, p.given_name, p.family_name,
               COUNT(DISTINCT g.id) as gap_count
        FROM patients p
        LEFT JOIN care_gaps g ON p.patient_id = g.patient_id
        WHERE p.deceased_date = '' OR p.deceased_date IS NULL
        GROUP BY p.patient_id
        ORDER BY gap_count DESC LIMIT 1
    """).fetchone()

    if top:
        print(f"Assembling data for: {top[1]} {top[2]} ({top[3]} gaps)\n")
        data = assemble_patient_data(conn, top[0])
        print(json.dumps(data, indent=2, default=str))
    else:
        print("No patients found.")

    conn.close()