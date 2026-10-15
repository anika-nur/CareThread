"""
care_gaps.py — Detects care gaps by checking patient records against clinical guidelines.

Each gap rule is a function that takes a patient_id and a database connection,
checks whether the patient qualifies for a specific screening,
and returns a gap dict if the screening is overdue.
"""

import sqlite3
from datetime import datetime, timedelta


TODAY = datetime.now().strftime("%Y-%m-%d")


# ──────────────────────────────────────────────
# CODE SETS
#
# Verified against the actual Synthea FHIR output in synthea/output/fhir/.
# Synthea does not emit every code these measures nominally use, so each set
# leads with the code that actually appears in the data and keeps the
# canonical alternates as fallbacks for other data sources.
# ──────────────────────────────────────────────

DIABETES_CODES = ["44054006", "73211009", "46635009"]

# Synthea emits 59621000 for essential hypertension, not 38341003.
HYPERTENSION_CODES = ["59621000", "38341003"]

# Synthea records the diabetic retinal exam as 722161008.
EYE_EXAM_CODES = ["722161008", "700070005", "36228007", "410451008", "274798009"]

# Synthea records cervical cytology as 90226004.
CERVICAL_SCREENING_CODES = ["90226004", "171149006"]

MAMMOGRAM_CODES = ["71651007", "24623002", "241055006"]

COLONOSCOPY_CODES = ["73761001"]

# Influenza vaccines are Immunization resources coded in CVX, not Procedures.
FLU_CVX_CODES = ["140", "141", "150", "155", "158", "161", "88",
                 "171", "185", "186", "197", "205"]

# Smoking cessation is a CarePlan activity in Synthea, not a Procedure.
SMOKING_CESSATION_CODES = ["225323000", "710081004"]


def get_active_conditions(conn, patient_id):
    """Get all active condition codes for a patient."""
    rows = conn.execute(
        "SELECT code, display FROM conditions "
        "WHERE patient_id=? AND clinical_status='active'",
        (patient_id,)
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def get_latest_observation(conn, patient_id, loinc_code):
    """Get the most recent observation of a given LOINC type."""
    row = conn.execute(
        "SELECT value, unit, effective_date FROM observations "
        "WHERE patient_id=? AND code=? "
        "ORDER BY effective_date DESC LIMIT 1",
        (patient_id, loinc_code)
    ).fetchone()
    return row  # (value, unit, date) or None


def get_latest_procedure(conn, patient_id, snomed_code):
    """Get the most recent procedure of a given SNOMED type."""
    row = conn.execute(
        "SELECT display, performed_date FROM procedures "
        "WHERE patient_id=? AND code=? "
        "ORDER BY performed_date DESC LIMIT 1",
        (patient_id, snomed_code)
    ).fetchone()
    return row  # (display, date) or None


def get_latest_procedure_any(conn, patient_id, snomed_codes):
    """Get the most recent procedure matching any of the given SNOMED codes."""
    placeholders = ",".join("?" for _ in snomed_codes)
    row = conn.execute(
        f"SELECT display, performed_date FROM procedures "
        f"WHERE patient_id=? AND code IN ({placeholders}) "
        f"ORDER BY performed_date DESC LIMIT 1",
        (patient_id, *snomed_codes)
    ).fetchone()
    return row  # (display, date) or None


def get_latest_immunization(conn, patient_id, cvx_codes):
    """Get the most recent immunization matching any of the given CVX codes."""
    placeholders = ",".join("?" for _ in cvx_codes)
    row = conn.execute(
        f"SELECT display, occurrence_date FROM immunizations "
        f"WHERE patient_id=? AND code IN ({placeholders}) "
        f"ORDER BY occurrence_date DESC LIMIT 1",
        (patient_id, *cvx_codes)
    ).fetchone()
    return row  # (display, date) or None


def get_latest_care_plan_activity(conn, patient_id, snomed_codes):
    """Get the most recent care plan activity matching any of the given codes."""
    placeholders = ",".join("?" for _ in snomed_codes)
    row = conn.execute(
        f"SELECT display, start_date FROM care_plan_activities "
        f"WHERE patient_id=? AND code IN ({placeholders}) "
        f"ORDER BY start_date DESC LIMIT 1",
        (patient_id, *snomed_codes)
    ).fetchone()
    return row  # (display, date) or None


def get_patient_age(conn, patient_id):
    """Calculate patient's current age in years."""
    row = conn.execute(
        "SELECT birth_date FROM patients WHERE patient_id=?",
        (patient_id,)
    ).fetchone()
    if not row or not row[0]:
        return None
    birth = datetime.strptime(row[0], "%Y-%m-%d")
    today = datetime.now()
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def is_current_smoker_value(value):
    """Decide whether a LOINC 72166-2 status string means *currently* smoking.

    Synthea records smoking status as a SNOMED display string. The values that
    occur are 'Never smoked tobacco (finding)', 'Ex-smoker (finding)' and
    'Smokes tobacco daily (finding)'. A naive "contains 'smok' but not 'never'"
    test classifies every ex-smoker as a current smoker, so former-smoker
    phrasings are excluded explicitly.
    """
    if not value:
        return False

    v = value.lower()
    if "never" in v or "ex-smoker" in v or "former" in v:
        return False
    return "smokes" in v or "current" in v


def days_since(date_str):
    """Calculate days between a date string and today."""
    if not date_str:
        return float("inf")
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return (datetime.now() - d).days
    except ValueError:
        return float("inf")


# ──────────────────────────────────────────────
# GAP RULES
# Each returns a dict if a gap exists, or None
# ──────────────────────────────────────────────

def gap_hba1c(conn, patient_id):
    """
    RULE: Diabetic patients should have HbA1c measured every 6 months.
    Applies to: patients with active diabetes (SNOMED 44054006).
    Checks for: LOINC 4548-4 (HbA1c) within last 180 days.
    Source: HEDIS Comprehensive Diabetes Care (CDC) measure.
    """
    conditions = get_active_conditions(conn, patient_id)

    # Check for any diabetes diagnosis
    has_diabetes = any(code in conditions for code in DIABETES_CODES)
    if not has_diabetes:
        return None

    latest = get_latest_observation(conn, patient_id, "4548-4")
    if latest is None:
        return {
            "gap_type": "HbA1c Overdue",
            "severity": "high",
            "description": "Diabetic patient has no HbA1c on record.",
            "guideline": "HEDIS CDC: HbA1c every 6 months for diabetic patients",
            "qualifying_condition": conditions.get("44054006",
                                   conditions.get("73211009", "Diabetes")),
            "last_date": None,
            "days_overdue": None,
        }

    days = days_since(latest[2])
    if days > 180:
        return {
            "gap_type": "HbA1c Overdue",
            "severity": "high" if days > 365 else "medium",
            "description": f"Last HbA1c was {days} days ago (value: {latest[0]}{latest[1]}). "
                          f"Due every 180 days.",
            "guideline": "HEDIS CDC: HbA1c every 6 months for diabetic patients",
            "qualifying_condition": "Diabetes mellitus",
            "last_date": latest[2],
            "days_overdue": days - 180,
        }
    return None


def gap_blood_pressure(conn, patient_id):
    """
    RULE: Hypertensive patients should have BP measured every 6 months.
    Applies to: patients with active hypertension (SNOMED 59621000).
    Checks for: LOINC 8480-6 (systolic) or 85354-9 (BP panel) in last 180 days.
    Source: HEDIS Controlling High Blood Pressure (CBP).
    """
    conditions = get_active_conditions(conn, patient_id)
    if not any(code in conditions for code in HYPERTENSION_CODES):
        return None

    # Check for systolic BP (LOINC 8480-6) or the BP panel (85354-9)
    latest = get_latest_observation(conn, patient_id, "8480-6")
    if latest is None:
        latest = get_latest_observation(conn, patient_id, "85354-9")

    if latest is None:
        return {
            "gap_type": "Blood Pressure Overdue",
            "severity": "high",
            "description": "Hypertensive patient has no BP reading on record.",
            "guideline": "HEDIS CBP: BP monitoring every 6 months for hypertensive patients",
            "qualifying_condition": "Essential hypertension",
            "last_date": None,
            "days_overdue": None,
        }

    days = days_since(latest[2])
    if days > 180:
        return {
            "gap_type": "Blood Pressure Overdue",
            "severity": "medium",
            "description": f"Last BP reading was {days} days ago ({latest[0]} {latest[1]}). "
                          f"Due every 180 days.",
            "guideline": "HEDIS CBP: BP monitoring every 6 months",
            "qualifying_condition": "Essential hypertension",
            "last_date": latest[2],
            "days_overdue": days - 180,
        }
    return None


def gap_colonoscopy(conn, patient_id):
    """
    RULE: Adults 45-75 should have a colonoscopy every 10 years.
    Applies to: patients aged 45-75 regardless of conditions.
    Checks for: SNOMED 73761001 (Colonoscopy) within last 10 years.
    Source: USPSTF Colorectal Cancer Screening.
    """
    age = get_patient_age(conn, patient_id)
    if age is None or age < 45 or age > 75:
        return None

    latest = get_latest_procedure_any(conn, patient_id, COLONOSCOPY_CODES)
    threshold_days = 365 * 10

    if latest is None:
        return {
            "gap_type": "Colonoscopy Overdue",
            "severity": "medium",
            "description": f"Patient is {age} with no colonoscopy on record. "
                          f"Recommended for ages 45-75.",
            "guideline": "USPSTF: Colonoscopy every 10 years for ages 45-75",
            "qualifying_condition": f"Age {age} (screening age range)",
            "last_date": None,
            "days_overdue": None,
        }

    days = days_since(latest[1])
    if days > threshold_days:
        return {
            "gap_type": "Colonoscopy Overdue",
            "severity": "medium",
            "description": f"Last colonoscopy was {days // 365} years ago. Due every 10 years.",
            "guideline": "USPSTF: Colonoscopy every 10 years for ages 45-75",
            "qualifying_condition": f"Age {age}",
            "last_date": latest[1],
            "days_overdue": days - threshold_days,
        }
    return None


def gap_flu_vaccine(conn, patient_id):
    """
    RULE: Adults 65+ should receive annual influenza vaccination.
    Checks for: a CVX-coded influenza Immunization within last 365 days.
    Source: USPSTF / CDC ACIP.
    """
    age = get_patient_age(conn, patient_id)
    if age is None or age < 65:
        return None

    latest = get_latest_immunization(conn, patient_id, FLU_CVX_CODES)

    if latest is None:
        return {
            "gap_type": "Flu Vaccine Overdue",
            "severity": "medium",
            "description": f"Patient is {age} with no flu vaccination on record.",
            "guideline": "CDC ACIP: Annual flu vaccine for adults 65+",
            "qualifying_condition": f"Age {age}",
            "last_date": None,
            "days_overdue": None,
        }

    days = days_since(latest[1])
    if days > 365:
        return {
            "gap_type": "Flu Vaccine Overdue",
            "severity": "low",
            "description": f"Last flu vaccine was {days} days ago. Due annually.",
            "guideline": "CDC ACIP: Annual flu vaccine for adults 65+",
            "qualifying_condition": f"Age {age}",
            "last_date": latest[1],
            "days_overdue": days - 365,
        }
    return None


def gap_depression_followup(conn, patient_id):
    """
    RULE: Patients who screened positive for depression should have
    a follow-up encounter within 30 days.
    Checks for: PHQ-2 score >= 3 (LOINC 55758-7) with no encounter within 30 days after.
    Source: UDS Depression Screening and Follow-Up measure.
    """
    # Get all PHQ-2 scores
    rows = conn.execute(
        "SELECT value, effective_date FROM observations "
        "WHERE patient_id=? AND code='55758-7' "
        "ORDER BY effective_date DESC",
        (patient_id,)
    ).fetchall()

    for value_str, screen_date in rows:
        try:
            score = float(value_str)
        except (ValueError, TypeError):
            continue

        if score >= 3 and screen_date:
            # Check for any encounter within 30 days after the screening
            screen_dt = datetime.strptime(screen_date[:10], "%Y-%m-%d")
            followup_window = (screen_dt + timedelta(days=30)).strftime("%Y-%m-%d")

            followup = conn.execute(
                "SELECT start_date FROM encounters "
                "WHERE patient_id=? AND start_date > ? AND start_date <= ?",
                (patient_id, screen_date, followup_window)
            ).fetchone()

            if not followup:
                return {
                    "gap_type": "Depression Follow-Up Missing",
                    "severity": "high",
                    "description": f"PHQ-2 score of {score} on {screen_date} "
                                  f"with no follow-up encounter within 30 days.",
                    "guideline": "UDS: Follow-up within 30 days of positive depression screen",
                    "qualifying_condition": f"PHQ-2 positive (score {score})",
                    "last_date": screen_date,
                    "days_overdue": days_since(followup_window),
                }
    return None


def gap_lipid_panel(conn, patient_id):
    """
    RULE: Diabetic patients should have a lipid panel annually.
    Checks for: LOINC 57698-3 (Lipid panel) within last 365 days.
    Source: HEDIS Comprehensive Diabetes Care.
    """
    conditions = get_active_conditions(conn, patient_id)
    if not any(code in conditions for code in DIABETES_CODES):
        return None

    # Try multiple LOINC codes for lipid panels
    for loinc in ["57698-3", "2093-3"]:  # Full panel or just total cholesterol
        latest = get_latest_observation(conn, patient_id, loinc)
        if latest:
            break

    if latest is None:
        return {
            "gap_type": "Lipid Panel Overdue",
            "severity": "medium",
            "description": "Diabetic patient has no lipid panel on record.",
            "guideline": "HEDIS CDC: Annual lipid panel for diabetic patients",
            "qualifying_condition": "Diabetes mellitus",
            "last_date": None,
            "days_overdue": None,
        }

    days = days_since(latest[2])
    if days > 365:
        return {
            "gap_type": "Lipid Panel Overdue",
            "severity": "medium",
            "description": f"Last lipid panel was {days} days ago. Due annually.",
            "guideline": "HEDIS CDC: Annual lipid panel for diabetic patients",
            "qualifying_condition": "Diabetes mellitus",
            "last_date": latest[2],
            "days_overdue": days - 365,
        }
    return None

def gap_mammogram(conn, patient_id):
    """
    RULE: Women aged 40-74 should have a mammogram every 2 years.
    Checks for: SNOMED 71651007 (Mammography) within last 730 days.
    Source: USPSTF Breast Cancer Screening.
    """
    age = get_patient_age(conn, patient_id)
    if age is None or age < 40 or age > 74:
        return None

    gender = conn.execute(
        "SELECT gender FROM patients WHERE patient_id=?", (patient_id,)
    ).fetchone()
    if not gender or gender[0] != "female":
        return None

    latest = get_latest_procedure_any(conn, patient_id, MAMMOGRAM_CODES)
    if latest is None:
        return {
            "gap_type": "Mammogram Overdue",
            "severity": "medium",
            "description": f"Female patient, age {age}, with no mammogram on record. "
                          f"Recommended every 2 years for ages 40-74.",
            "guideline": "USPSTF: Mammography every 2 years for women 40-74",
            "qualifying_condition": f"Female, age {age}",
            "last_date": None,
            "days_overdue": None,
        }

    days = days_since(latest[1])
    if days > 730:
        return {
            "gap_type": "Mammogram Overdue",
            "severity": "medium",
            "description": f"Last mammogram was {days // 365} years ago. Due every 2 years.",
            "guideline": "USPSTF: Mammography every 2 years for women 40-74",
            "qualifying_condition": f"Female, age {age}",
            "last_date": latest[1],
            "days_overdue": days - 730,
        }
    return None


def gap_cervical_screening(conn, patient_id):
    """
    RULE: Women aged 21-65 should have cervical cancer screening every 3 years
    (Pap smear alone) or every 5 years (Pap + HPV co-testing for 30-65).
    Uses the 3-year interval for simplicity.
    Checks for: SNOMED 171149006 (Cervical screening) within last 1095 days.
    Source: USPSTF Cervical Cancer Screening.
    """
    age = get_patient_age(conn, patient_id)
    if age is None or age < 21 or age > 65:
        return None

    gender = conn.execute(
        "SELECT gender FROM patients WHERE patient_id=?", (patient_id,)
    ).fetchone()
    if not gender or gender[0] != "female":
        return None

    # Synthea codes cervical cytology as 90226004, not the canonical 171149006
    latest = get_latest_procedure_any(conn, patient_id, CERVICAL_SCREENING_CODES)
    if latest is None:
        # Also try the observation code for Pap result
        latest_obs = get_latest_observation(conn, patient_id, "10524-7")
        if latest_obs:
            latest = (latest_obs[0], latest_obs[2])

    if latest is None:
        return {
            "gap_type": "Cervical Screening Overdue",
            "severity": "medium",
            "description": f"Female patient, age {age}, with no cervical cancer screening "
                          f"on record. Recommended every 3 years.",
            "guideline": "USPSTF: Cervical cancer screening every 3 years for women 21-65",
            "qualifying_condition": f"Female, age {age}",
            "last_date": None,
            "days_overdue": None,
        }

    days = days_since(latest[1])
    if days > 1095:
        return {
            "gap_type": "Cervical Screening Overdue",
            "severity": "medium",
            "description": f"Last cervical screening was {days // 365} years ago. "
                          f"Due every 3 years.",
            "guideline": "USPSTF: Cervical cancer screening every 3 years for women 21-65",
            "qualifying_condition": f"Female, age {age}",
            "last_date": latest[1],
            "days_overdue": days - 1095,
        }
    return None


def gap_bmi_screening(conn, patient_id):
    """
    RULE: All adults should have BMI screened at least annually.
    Checks for: LOINC 39156-5 (BMI) within last 365 days.
    Source: USPSTF Obesity Screening; UDS BMI Screening measure.
    """
    age = get_patient_age(conn, patient_id)
    if age is None or age < 18:
        return None

    latest = get_latest_observation(conn, patient_id, "39156-5")
    if latest is None:
        return {
            "gap_type": "BMI Screening Overdue",
            "severity": "low",
            "description": f"Adult patient, age {age}, with no BMI on record.",
            "guideline": "USPSTF/UDS: Annual BMI screening for all adults",
            "qualifying_condition": f"Adult, age {age}",
            "last_date": None,
            "days_overdue": None,
        }

    days = days_since(latest[2])
    if days > 365:
        return {
            "gap_type": "BMI Screening Overdue",
            "severity": "low",
            "description": f"Last BMI was {days} days ago ({latest[0]} {latest[1]}). "
                          f"Due annually.",
            "guideline": "USPSTF/UDS: Annual BMI screening for all adults",
            "qualifying_condition": f"Adult, age {age}",
            "last_date": latest[2],
            "days_overdue": days - 365,
        }
    return None


def gap_diabetic_eye_exam(conn, patient_id):
    """
    RULE: Diabetic patients should have a retinal/eye exam annually.
    Checks for: SNOMED 722161008 (Diabetic retinal eye exam) — the code Synthea
    actually emits — or the canonical alternates, within last 365 days.
    Source: HEDIS Comprehensive Diabetes Care — Eye Exam (CDC-EE).
    """
    conditions = get_active_conditions(conn, patient_id)
    if not any(code in conditions for code in DIABETES_CODES):
        return None

    latest = get_latest_procedure_any(conn, patient_id, EYE_EXAM_CODES)

    if latest is None:
        return {
            "gap_type": "Diabetic Eye Exam Overdue",
            "severity": "high",
            "description": "Diabetic patient with no eye exam on record. "
                          "Diabetic retinopathy can cause preventable blindness.",
            "guideline": "HEDIS CDC-EE: Annual eye exam for diabetic patients",
            "qualifying_condition": "Diabetes mellitus",
            "last_date": None,
            "days_overdue": None,
        }

    days = days_since(latest[1])
    if days > 365:
        return {
            "gap_type": "Diabetic Eye Exam Overdue",
            "severity": "high" if days > 730 else "medium",
            "description": f"Last eye exam was {days // 365} years ago. Due annually.",
            "guideline": "HEDIS CDC-EE: Annual eye exam for diabetic patients",
            "qualifying_condition": "Diabetes mellitus",
            "last_date": latest[1],
            "days_overdue": days - 365,
        }
    return None


def gap_nephropathy_screening(conn, patient_id):
    """
    RULE: Diabetic patients should have urine microalbumin tested annually
    to screen for kidney damage.
    Checks for: LOINC 14959-1 (Microalbumin in urine) within last 365 days.
    Source: HEDIS Comprehensive Diabetes Care — Nephropathy (CDC-NP).
    """
    conditions = get_active_conditions(conn, patient_id)
    if not any(code in conditions for code in DIABETES_CODES):
        return None

    # Try multiple LOINC codes for urine albumin tests
    latest = None
    for loinc in ["14959-1", "14957-5", "30000-4", "13705-9"]:
        result = get_latest_observation(conn, patient_id, loinc)
        if result and (latest is None or result[2] > latest[2]):
            latest = result

    if latest is None:
        return {
            "gap_type": "Nephropathy Screening Overdue",
            "severity": "high",
            "description": "Diabetic patient with no urine microalbumin test on record. "
                          "Early kidney damage is treatable if caught.",
            "guideline": "HEDIS CDC-NP: Annual nephropathy screening for diabetic patients",
            "qualifying_condition": "Diabetes mellitus",
            "last_date": None,
            "days_overdue": None,
        }

    days = days_since(latest[2])
    if days > 365:
        return {
            "gap_type": "Nephropathy Screening Overdue",
            "severity": "medium",
            "description": f"Last urine microalbumin was {days} days ago. Due annually.",
            "guideline": "HEDIS CDC-NP: Annual nephropathy screening for diabetic patients",
            "qualifying_condition": "Diabetes mellitus",
            "last_date": latest[2],
            "days_overdue": days - 365,
        }
    return None


def gap_smoking_cessation(conn, patient_id):
    """
    RULE: Patients who currently smoke should have tobacco cessation counseling
    documented at least once in the past year.
    Checks for: Active smoking status (SNOMED 449868002 or 428041000124106)
    with no cessation counseling procedure (SNOMED 225323000) in last 365 days.
    Source: UDS Tobacco Use Screening and Cessation Intervention.
    """
    conditions = get_active_conditions(conn, patient_id)
    smoking_codes = ["449868002", "428041000124106"]

    # Also check observations for smoking status
    smoking_obs = conn.execute(
        "SELECT value FROM observations WHERE patient_id=? AND code='72166-2' "
        "ORDER BY effective_date DESC LIMIT 1",
        (patient_id,)
    ).fetchone()

    is_smoker = any(code in conditions for code in smoking_codes)
    if smoking_obs and is_current_smoker_value(smoking_obs[0]):
        is_smoker = True

    if not is_smoker:
        return None

    latest = get_latest_care_plan_activity(
        conn, patient_id, SMOKING_CESSATION_CODES)
    if latest is None:
        return {
            "gap_type": "Smoking Cessation Counseling Missing",
            "severity": "medium",
            "description": "Active smoker with no tobacco cessation counseling on record.",
            "guideline": "UDS: Tobacco cessation intervention for current smokers annually",
            "qualifying_condition": "Current tobacco user",
            "last_date": None,
            "days_overdue": None,
        }

    days = days_since(latest[1])
    if days > 365:
        return {
            "gap_type": "Smoking Cessation Counseling Overdue",
            "severity": "low",
            "description": f"Last cessation counseling was {days} days ago. Due annually.",
            "guideline": "UDS: Tobacco cessation intervention for current smokers annually",
            "qualifying_condition": "Current tobacco user",
            "last_date": latest[1],
            "days_overdue": days - 365,
        }
    return None


def gap_annual_wellness(conn, patient_id):
    """
    RULE: All patients should have at least one wellness/preventive visit per year.
    Checks for: Any encounter of type 'wellness' or 'check up' within last 365 days.
    Source: General preventive care standard; relevant to UDS visit metrics.
    """
    age = get_patient_age(conn, patient_id)
    if age is None or age < 18:
        return None

    # Check for any wellness/preventive encounter in the last year
    cutoff = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    wellness = conn.execute(
        "SELECT start_date FROM encounters WHERE patient_id=? AND start_date >= ? "
        "AND (encounter_type LIKE '%wellness%' OR encounter_type LIKE '%check up%' "
        "OR encounter_type LIKE '%preventive%' OR encounter_type LIKE '%annual%')",
        (patient_id, cutoff)
    ).fetchone()

    if wellness is None:
        # Check when last wellness visit was
        last = conn.execute(
            "SELECT start_date FROM encounters WHERE patient_id=? "
            "AND (encounter_type LIKE '%wellness%' OR encounter_type LIKE '%check up%' "
            "OR encounter_type LIKE '%preventive%' OR encounter_type LIKE '%annual%') "
            "ORDER BY start_date DESC LIMIT 1",
            (patient_id,)
        ).fetchone()

        return {
            "gap_type": "Annual Wellness Visit Overdue",
            "severity": "low",
            "description": f"No preventive/wellness visit in the past year."
                          + (f" Last was {last[0]}." if last else " None on record."),
            "guideline": "Preventive care: Annual wellness visit recommended for all adults",
            "qualifying_condition": f"Adult, age {age}",
            "last_date": last[0] if last else None,
            "days_overdue": days_since(last[0]) - 365 if last else None,
        }
    return None


# ──────────────────────────────────────────────
# MAIN: Run all gap rules on all patients
# ──────────────────────────────────────────────

ALL_GAP_RULES = [
    gap_hba1c,
    gap_blood_pressure,
    gap_colonoscopy,
    gap_flu_vaccine,
    gap_depression_followup,
    gap_lipid_panel,
    gap_mammogram,
    gap_cervical_screening,
    gap_bmi_screening,
    gap_diabetic_eye_exam,
    gap_nephropathy_screening,
    gap_smoking_cessation,
    gap_annual_wellness,
]


def detect_gaps_for_patient(conn, patient_id):
    """Run all gap rules for one patient and return a list of detected gaps."""
    gaps = []
    for rule in ALL_GAP_RULES:
        result = rule(conn, patient_id)
        if result:
            result["patient_id"] = patient_id
            gaps.append(result)
    return gaps


def main():
    conn = sqlite3.connect("carethread.db")

    # Deceased patients still carry unmet screening history, but scoring them
    # for future care inflates every measure, so exclude them.
    patient_ids = [r[0] for r in conn.execute(
        "SELECT patient_id FROM patients "
        "WHERE deceased_date = '' OR deceased_date IS NULL"
    ).fetchall()]

    total = conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    print(f"Running {len(ALL_GAP_RULES)} gap rules on {len(patient_ids)} living "
          f"patients ({total - len(patient_ids)} deceased excluded)...\n")

    all_gaps = []
    for pid in patient_ids:
        patient_gaps = detect_gaps_for_patient(conn, pid)
        all_gaps.extend(patient_gaps)

    # Summary
    print(f"Total gaps detected: {len(all_gaps)}\n")

    # Count by type
    from collections import Counter
    by_type = Counter(g["gap_type"] for g in all_gaps)
    print("Gaps by type:")
    for gap_type, count in by_type.most_common():
        print(f"  {gap_type}: {count}")

    # Count by severity
    by_severity = Counter(g["severity"] for g in all_gaps)
    print("\nGaps by severity:")
    for sev, count in by_severity.most_common():
        print(f"  {sev}: {count}")

    # Show a detailed example
    if all_gaps:
        print("\n--- Example Gap (first detected) ---")
        ex = all_gaps[0]

        # Get patient name
        row = conn.execute(
            "SELECT given_name, family_name FROM patients WHERE patient_id=?",
            (ex["patient_id"],)
        ).fetchone()
        name = f"{row[0]} {row[1]}" if row else ex["patient_id"]

        print(f"  Patient: {name}")
        print(f"  Gap: {ex['gap_type']}")
        print(f"  Severity: {ex['severity']}")
        print(f"  Description: {ex['description']}")
        print(f"  Guideline: {ex['guideline']}")
        print(f"  Qualifying condition: {ex['qualifying_condition']}")

    # Save gaps to database
    conn.execute("DROP TABLE IF EXISTS care_gaps")
    conn.execute("""CREATE TABLE care_gaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT, gap_type TEXT, severity TEXT,
        description TEXT, guideline TEXT,
        qualifying_condition TEXT, last_date TEXT, days_overdue INTEGER
    )""")

    for g in all_gaps:
        conn.execute(
            "INSERT INTO care_gaps (patient_id, gap_type, severity, description, "
            "guideline, qualifying_condition, last_date, days_overdue) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (g["patient_id"], g["gap_type"], g["severity"], g["description"],
             g["guideline"], g["qualifying_condition"],
             g.get("last_date"), g.get("days_overdue"))
        )
    conn.commit()
    print(f"\nGaps saved to care_gaps table in carethread.db")

    conn.close()


if __name__ == "__main__":
    main()