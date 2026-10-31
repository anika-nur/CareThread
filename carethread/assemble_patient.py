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
from config import DB_PATH


# SNOMED codes Synthea's social-determinants module writes into the
# conditions table. They describe the patient's circumstances, not a
# diagnosis, so they go to the model as social history, not as conditions.
# "(finding)" alone can't be the test: Prediabetes and BMI 30+ are findings too.
# Stress and substance misuse stay clinical.
SOCIAL_FINDING_CODES = {
    "160903007",   # Full-time employment
    "160904001",   # Part-time employment
    "741062008",   # Not in labor force
    "73438004",    # Unemployed
    "224299000",   # Received higher education
    "473461003",   # Educated to high school level
    "224295006",   # Only received primary school education
    "266948004",   # Has a criminal record
    "105531004",   # Housing unsatisfactory
    "32911000",    # Homeless
    "713458007",   # Lack of access to transportation
    "266934004",   # Transport problem
    "423315002",   # Limited social contact
    "422650009",   # Social isolation
    "706893006",   # Victim of intimate partner abuse
    "424393004",   # Reports of violence in the environment
    "160968000",   # Risk activity involvement
    "446654005",   # Refugee
    "1187604002",  # Serving in military service
}

# Administrative entries Synthea files as conditions. Not a diagnosis and
# not social history, so they go nowhere. ("History of…" situations such as
# past MI or renal transplant are clinical history and stay as conditions.)
ADMIN_FINDING_CODES = {
    "314529007",   # Medication review due (situation)
}

# An active prescription older than this, never re-prescribed since, may no
# longer be taken. Synthea leaves long-term drugs active for decades, so these
# are flagged for confirmation, not removed: dropping them would hide drugs
# that appear in detected interactions.
STALE_MED_YEARS = 5


def stale_cutoff():
    """ISO date STALE_MED_YEARS ago: a drug last prescribed before it is stale."""
    today = datetime.now().date()
    return today.replace(year=today.year - STALE_MED_YEARS).isoformat()

# Social findings the UI keeps behind a "show sensitive items" toggle, so
# they aren't on screen by default when a patient might see it.
SENSITIVE_SOCIAL_CODES = {
    "706893006",   # Victim of intimate partner abuse
    "424393004",   # Reports of violence in the environment
    "266948004",   # Has a criminal record
    "105531004",   # Housing unsatisfactory
    "32911000",    # Homeless
    "446654005",   # Refugee
    "160968000",   # Risk activity involvement
}
# The same items as the model might word them, for entries that didn't trace
SENSITIVE_SOCIAL_WORDS = ("abuse", "violence", "criminal", "housing",
                          "homeless", "refugee", "risk activity")


# ZIP-level AHRQ indicators, in display order. Each label reads after its
# value and describes the area, never the patient.
SDOH_INDICATORS = {
    "pct_poverty":               "of residents live below the federal poverty line",
    "median_household_income":   "median household income",
    "pct_uninsured":             "of residents have no health insurance",
    "pct_unemployed":            "of the labor force is unemployed",
    "pct_public_assistance":     "of households receive public assistance or SNAP",
    "pct_less_than_high_school": "of adults 25+ have no high school diploma",
    "pct_no_vehicle":            "of households have no vehicle",
    "pct_rent_burdened":         "of renter households pay 30% or more of income on rent",
}


def format_indicator(key, value):
    """'pct_poverty', 9.84 → '9.84% of residents live below the federal poverty line'."""
    label = SDOH_INDICATORS.get(key, key)
    if key == "median_household_income":
        return f"${float(value):,.0f} {label}"
    return f"{value}% {label}"


def valid_zip(zip_code):
    """Synthea writes '00000' when it has no ZIP; that can never match."""
    z = str(zip_code or "").strip()
    return len(z) == 5 and z.isdigit() and z != "00000"


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
    """Active diagnoses with onset dates (social findings excluded)."""
    rows = conn.execute(
        "SELECT display, onset_date, code FROM conditions "
        "WHERE patient_id=? AND clinical_status='active' "
        "ORDER BY onset_date",
        (patient_id,)
    ).fetchall()
    return [
        {"condition": r[0], "since": r[1], "snomed_code": r[2]}
        for r in rows
        if str(r[2]) not in SOCIAL_FINDING_CODES | ADMIN_FINDING_CODES
    ]


def get_social_history(conn, patient_id):
    """Active patient-level social findings: employment, education, housing..."""
    rows = conn.execute(
        "SELECT display, onset_date, code FROM conditions "
        "WHERE patient_id=? AND clinical_status='active' "
        "ORDER BY onset_date",
        (patient_id,)
    ).fetchall()
    return [
        {"finding": r[0].replace(" (finding)", "").replace(" (person)", ""),
         "since": r[1], "snomed_code": r[2]}
        for r in rows if str(r[2]) in SOCIAL_FINDING_CODES
    ]


def get_active_medications(conn, patient_id):
    """Currently active medications. Each carries `confirm_with_patient`:
    started over STALE_MED_YEARS ago and never re-prescribed since."""
    cutoff = stale_cutoff()
    rows = conn.execute(
        "SELECT m.display, m.authored_on, m.code, "
        "  EXISTS (SELECT 1 FROM medications later "
        "          WHERE later.patient_id = m.patient_id AND later.code = m.code "
        "            AND later.authored_on > m.authored_on) "
        "FROM medications m "
        "WHERE m.patient_id=? AND m.status='active' "
        "ORDER BY m.authored_on DESC",
        (patient_id,)
    ).fetchall()
    return [
        {"medication": r[0], "prescribed": r[1], "rxnorm_code": r[2],
         "confirm_with_patient": bool(r[1] and r[1] < cutoff and not r[3])}
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


def get_care_gaps(conn, patient_id, table="care_gaps"):
    """Detected care gaps. `table` may name care_gaps_clinic, the same rules
    run on the home clinic's records alone (see clinic_view.py)."""
    rows = conn.execute(
        "SELECT gap_type, severity, description, guideline, "
        "qualifying_condition, last_date, days_overdue "
        f"FROM {table} WHERE patient_id=? "
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


INTERACTION_COLUMNS = (
    "di.patient_id, di.drug_a, di.drug_b, di.drug_a_matched, di.drug_b_matched, "
    "di.severity, "
    "(SELECT MAX(m.authored_on) FROM medications m "
    "  WHERE m.patient_id = di.patient_id AND m.display = di.drug_a), "
    "(SELECT MAX(m.authored_on) FROM medications m "
    "  WHERE m.patient_id = di.patient_id AND m.display = di.drug_b)"
)
INTERACTION_ORDER = ("CASE di.severity WHEN 'major' THEN 1 WHEN 'moderate' THEN 2 "
                     "WHEN 'minor' THEN 3 ELSE 4 END")


def interaction_row(r, cutoff):
    """One drug_interactions row (selected with INTERACTION_COLUMNS) as a dict.
    `drug_a_confirm` / `drug_b_confirm` hold the year a drug was last prescribed
    when that was before the cutoff (never re-prescribed since), else None.
    `current` is True when both drugs were prescribed within the cutoff."""
    a_last, b_last = r[6] or "", r[7] or ""
    a_confirm = a_last[:4] if a_last < cutoff else None
    b_confirm = b_last[:4] if b_last < cutoff else None
    return {"drug_a": r[1], "drug_b": r[2],
            "drug_a_generic": r[3], "drug_b_generic": r[4],
            "severity": r[5],
            "drug_a_last_prescribed": a_last, "drug_b_last_prescribed": b_last,
            "drug_a_confirm": a_confirm, "drug_b_confirm": b_confirm,
            "current": not (a_confirm or b_confirm)}


def get_drug_interactions(conn, patient_id, table="drug_interactions"):
    """Detected drug-drug interactions, each with when its drugs were last
    prescribed (see interaction_row). `table` as for get_care_gaps."""
    rows = conn.execute(
        f"SELECT {INTERACTION_COLUMNS} FROM {table} di "
        f"WHERE di.patient_id=? ORDER BY {INTERACTION_ORDER}",
        (patient_id,)
    ).fetchall()
    cutoff = stale_cutoff()
    return [interaction_row(r, cutoff) for r in rows]


def get_sdoh_data(conn, patient_id):
    """Community-level SDOH indicators for the patient's zip code.
    None when the table is missing or the ZIP has no AHRQ row."""
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
        "social_history": get_social_history(conn, patient_id),
        "sdoh": get_sdoh_data(conn, patient_id),
    }


# ── Standalone test ──

if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)

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