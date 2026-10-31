"""
parse_fhir.py — Reads Synthea FHIR R4 bundles and extracts structured patient data.

Usage:
    python parse_fhir.py /path/to/synthea/output/fhir/

Output:
    Creates a SQLite database (carethread.db) with tables:
    patients, conditions, medications, observations, encounters, procedures,
    immunizations, care_plan_activities, organizations

    Every clinical row carries the encounter_id it was recorded in, and every
    encounter its organization_id, so each record can be traced to the
    source system it came from (see sources.py).

Note:
    The child tables have no unique constraint, so re-running against an
    existing carethread.db duplicates every clinical row. Delete the database
    first rather than re-running on top of it.
"""

import json
import os
import sys
import sqlite3
from pathlib import Path
from datetime import datetime

from config import DB_PATH
from sources import encounter_role, load_organizations


def parse_patient(resource):
    """Extract key fields from a Patient resource."""
    name = resource.get("name", [{}])[0]
    address = resource.get("address", [{}])[0]
    return {
        "patient_id": resource["id"],
        "given_name": " ".join(name.get("given", [])),
        "family_name": name.get("family", ""),
        "birth_date": resource.get("birthDate", ""),
        "gender": resource.get("gender", ""),
        "city": address.get("city", ""),
        "state": address.get("state", ""),
        "zip_code": address.get("postalCode", ""),
        "marital_status": resource.get("maritalStatus", {}).get("text", ""),
        "deceased_date": resource.get("deceasedDateTime", "")[:10]
                 if resource.get("deceasedDateTime") else "",
    }


def encounter_ref(resource):
    """Id of the Encounter a resource was recorded in ('urn:uuid:<id>')."""
    ref = (resource.get("encounter") or {}).get("reference", "")
    return ref.rsplit(":", 1)[-1] if ref else ""


def get_code(resource):
    """Extract the primary code, system, and display from a coded resource."""
    code_field = resource.get("code", resource.get("medicationCodeableConcept", {}))
    codings = code_field.get("coding", [])
    if codings:
        return {
            "code": codings[0].get("code", ""),
            "system": codings[0].get("system", ""),
            "display": codings[0].get("display", ""),
        }
    return {"code": "", "system": "", "display": code_field.get("text", "")}


def parse_condition(resource, patient_id):
    """Extract key fields from a Condition resource."""
    code_info = get_code(resource)
    clinical_status = ""
    cs = resource.get("clinicalStatus", {})
    if cs:
        codings = cs.get("coding", [])
        if codings:
            clinical_status = codings[0].get("code", "")

    return {
        "patient_id": patient_id,
        "code": code_info["code"],
        "system": code_info["system"],
        "display": code_info["display"],
        "onset_date": resource.get("onsetDateTime", "")[:10],
        "abatement_date": resource.get("abatementDateTime", "")[:10]
                          if resource.get("abatementDateTime") else "",
        "clinical_status": clinical_status,
        "encounter_id": encounter_ref(resource),
    }


def parse_medication(resource, patient_id):
    """Extract key fields from a MedicationRequest resource."""
    code_info = get_code(resource)
    return {
        "patient_id": patient_id,
        "code": code_info["code"],
        "system": code_info["system"],
        "display": code_info["display"],
        "status": resource.get("status", ""),
        "authored_on": resource.get("authoredOn", "")[:10],
        "encounter_id": encounter_ref(resource),
    }


def parse_observation(resource, patient_id):
    """Extract key fields from an Observation resource."""
    code_info = get_code(resource)
    value = ""
    unit = ""
    vq = resource.get("valueQuantity", {})
    if vq:
        value = str(vq.get("value", ""))
        unit = vq.get("unit", "")
    elif resource.get("valueCodeableConcept"):
        codings = resource["valueCodeableConcept"].get("coding", [])
        if codings:
            value = codings[0].get("display", "")

    return {
        "patient_id": patient_id,
        "code": code_info["code"],
        "system": code_info["system"],
        "display": code_info["display"],
        "value": value,
        "unit": unit,
        "effective_date": resource.get("effectiveDateTime", "")[:10],
        "encounter_id": encounter_ref(resource),
    }


def parse_encounter(resource, patient_id):
    """Extract key fields from an Encounter resource."""
    enc_class = resource.get("class", {}).get("code", "")
    enc_type, enc_code = "", ""
    types = resource.get("type", [])
    if types:
        codings = types[0].get("coding", [])
        if codings:
            enc_type = codings[0].get("display", "")
            enc_code = codings[0].get("code", "")   # SNOMED in Synthea
        else:
            enc_type = types[0].get("text", "")

    period = resource.get("period", {})
    # 'Organization?identifier=https://github.com/synthetichealth/synthea|<id>'
    provider = (resource.get("serviceProvider") or {}).get("reference", "")
    return {
        "patient_id": patient_id,
        "encounter_id": resource.get("id", ""),
        "encounter_class": enc_class,
        "encounter_type": enc_type,
        "encounter_code": enc_code,
        "start_date": period.get("start", "")[:10],
        "end_date": period.get("end", "")[:10] if period.get("end") else "",
        "organization_id": provider.rsplit("|", 1)[-1] if provider else "",
        "role": encounter_role(enc_class, enc_type),
    }


def parse_procedure(resource, patient_id):
    """Extract key fields from a Procedure resource."""
    code_info = get_code(resource)
    performed = resource.get("performedDateTime",
                resource.get("performedPeriod", {}).get("start", ""))
    return {
        "patient_id": patient_id,
        "code": code_info["code"],
        "system": code_info["system"],
        "display": code_info["display"],
        "performed_date": performed[:10] if performed else "",
        "encounter_id": encounter_ref(resource),
    }


def parse_immunization(resource, patient_id):
    """Extract key fields from an Immunization resource.

    Immunizations use `vaccineCode`/`occurrenceDateTime` rather than the
    `code`/`performedDateTime` pair the other resources use, so get_code()
    does not apply here.
    """
    code_field = resource.get("vaccineCode", {})
    codings = code_field.get("coding", [])
    if codings:
        code = codings[0].get("code", "")
        system = codings[0].get("system", "")
        display = codings[0].get("display", "")
    else:
        code, system, display = "", "", code_field.get("text", "")

    occurrence = resource.get("occurrenceDateTime", "")
    return {
        "patient_id": patient_id,
        "code": code,
        "system": system,
        "display": display,
        "status": resource.get("status", ""),
        "occurrence_date": occurrence[:10] if occurrence else "",
        "encounter_id": encounter_ref(resource),
    }


def parse_care_plan_activities(resource, patient_id):
    """Extract one row per coded activity on a CarePlan resource.

    Synthea models counseling and education interventions (e.g. smoking
    cessation, SNOMED 225323000) as CarePlan activities rather than as
    Procedure resources, so they are invisible unless parsed here.
    """
    start = resource.get("period", {}).get("start", "")[:10]
    status = resource.get("status", "")

    rows = []
    for activity in resource.get("activity", []):
        codings = activity.get("detail", {}).get("code", {}).get("coding", [])
        for coding in codings:
            rows.append({
                "patient_id": patient_id,
                "code": coding.get("code", ""),
                "system": coding.get("system", ""),
                "display": coding.get("display", ""),
                "status": status,
                "start_date": start,
                "encounter_id": encounter_ref(resource),
            })
    return rows


def parse_observation_components(resource, patient_id):
    """Emit each Observation.component[] entry as its own observation row.

    Panel observations (notably blood pressure, LOINC 85354-9) carry no
    top-level valueQuantity — the real numbers live in component[], keyed by
    their own LOINC codes (8480-6 systolic, 8462-4 diastolic). Without this
    those values are dropped and the panel row stores an empty value.
    """
    effective = resource.get("effectiveDateTime", "")[:10]

    rows = []
    for comp in resource.get("component", []):
        codings = comp.get("code", {}).get("coding", [])
        if not codings:
            continue

        vq = comp.get("valueQuantity", {})
        value = str(vq.get("value", "")) if vq else ""
        unit = vq.get("unit", "") if vq else ""
        if not value and comp.get("valueCodeableConcept"):
            vcodings = comp["valueCodeableConcept"].get("coding", [])
            if vcodings:
                value = vcodings[0].get("display", "")

        rows.append({
            "patient_id": patient_id,
            "code": codings[0].get("code", ""),
            "system": codings[0].get("system", ""),
            "display": codings[0].get("display", ""),
            "value": value,
            "unit": unit,
            "effective_date": effective,
            "encounter_id": encounter_ref(resource),
        })
    return rows


def process_bundle(filepath):
    """Parse a single FHIR bundle file and return structured data."""
    with open(filepath) as f:
        bundle = json.load(f)

    patient_id = None
    data = {
        "patients": [],
        "conditions": [],
        "medications": [],
        "observations": [],
        "encounters": [],
        "procedures": [],
        "immunizations": [],
        "care_plan_activities": [],
    }

    # First pass: find the Patient resource to get the patient_id
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") == "Patient":
            patient_id = resource["id"]
            data["patients"].append(parse_patient(resource))
            break

    if not patient_id:
        return data

    # Second pass: parse everything else
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        rtype = resource.get("resourceType")

        if rtype == "Condition":
            data["conditions"].append(parse_condition(resource, patient_id))
        elif rtype == "MedicationRequest":
            data["medications"].append(parse_medication(resource, patient_id))
        elif rtype == "Observation":
            data["observations"].append(parse_observation(resource, patient_id))
            data["observations"].extend(
                parse_observation_components(resource, patient_id))
        elif rtype == "Encounter":
            data["encounters"].append(parse_encounter(resource, patient_id))
        elif rtype == "Procedure":
            data["procedures"].append(parse_procedure(resource, patient_id))
        elif rtype == "Immunization":
            data["immunizations"].append(parse_immunization(resource, patient_id))
        elif rtype == "CarePlan":
            data["care_plan_activities"].extend(
                parse_care_plan_activities(resource, patient_id))

    return data


def create_database(db_path=DB_PATH):
    """Create the SQLite database with the required tables."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS patients (
        patient_id TEXT PRIMARY KEY,
        given_name TEXT, family_name TEXT,
        birth_date TEXT, gender TEXT,
        city TEXT, state TEXT, zip_code TEXT,
        marital_status TEXT, deceased_date TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS conditions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT, code TEXT, system TEXT,
        display TEXT, onset_date TEXT,
        abatement_date TEXT, clinical_status TEXT,
        encounter_id TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS medications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT, code TEXT, system TEXT,
        display TEXT, status TEXT, authored_on TEXT,
        encounter_id TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT, code TEXT, system TEXT,
        display TEXT, value TEXT, unit TEXT, effective_date TEXT,
        encounter_id TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS encounters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT, encounter_id TEXT,
        encounter_class TEXT, encounter_type TEXT, encounter_code TEXT,
        start_date TEXT, end_date TEXT,
        organization_id TEXT, role TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS organizations (
        organization_id TEXT PRIMARY KEY,
        name TEXT, city TEXT, state TEXT, group_id TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS procedures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT, code TEXT, system TEXT,
        display TEXT, performed_date TEXT,
        encounter_id TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS immunizations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT, code TEXT, system TEXT,
        display TEXT, status TEXT, occurrence_date TEXT,
        encounter_id TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS care_plan_activities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT, code TEXT, system TEXT,
        display TEXT, status TEXT, start_date TEXT,
        encounter_id TEXT
    )""")


    conn.commit()
    return conn


def main():
    if len(sys.argv) < 2:
        print("Usage: python parse_fhir.py /path/to/synthea/output/fhir/")
        sys.exit(1)

    fhir_dir = Path(sys.argv[1])
    json_files = list(fhir_dir.glob("*.json"))
    print(f"Found {len(json_files)} FHIR bundles")

    conn = create_database()
    c = conn.cursor()

    for org in load_organizations(fhir_dir):
        c.execute("INSERT OR REPLACE INTO organizations (organization_id,name,"
                  "city,state,group_id) VALUES (?,?,?,?,?)", tuple(org.values()))

    for i, filepath in enumerate(json_files):
        # Skip hospital and practitioner info files
        if "hospitalInformation" in filepath.name or "practitionerInformation" in filepath.name:
            continue

        data = process_bundle(filepath)

        for p in data["patients"]:
            c.execute("INSERT OR REPLACE INTO patients (patient_id,given_name,"
                      "family_name,birth_date,gender,city,state,zip_code,"
                      "marital_status,deceased_date) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      tuple(p.values()))
        for row in data["conditions"]:
            c.execute("INSERT INTO conditions (patient_id,code,system,display,"
                      "onset_date,abatement_date,clinical_status,encounter_id) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      tuple(row.values()))
        for row in data["medications"]:
            c.execute("INSERT INTO medications (patient_id,code,system,display,"
                      "status,authored_on,encounter_id) VALUES (?,?,?,?,?,?,?)",
                      tuple(row.values()))
        for row in data["observations"]:
            c.execute("INSERT INTO observations (patient_id,code,system,display,"
                      "value,unit,effective_date,encounter_id) VALUES (?,?,?,?,?,?,?,?)",
                      tuple(row.values()))
        for row in data["encounters"]:
            c.execute("INSERT INTO encounters (patient_id,encounter_id,encounter_class,"
                      "encounter_type,encounter_code,start_date,end_date,"
                      "organization_id,role) VALUES (?,?,?,?,?,?,?,?,?)",
                      tuple(row.values()))
        for row in data["procedures"]:
            c.execute("INSERT INTO procedures (patient_id,code,system,display,"
                      "performed_date,encounter_id) VALUES (?,?,?,?,?,?)",
                      tuple(row.values()))
        for row in data["immunizations"]:
            c.execute("INSERT INTO immunizations (patient_id,code,system,display,"
                      "status,occurrence_date,encounter_id) VALUES (?,?,?,?,?,?,?)",
                      tuple(row.values()))
        for row in data["care_plan_activities"]:
            c.execute("INSERT INTO care_plan_activities (patient_id,code,system,"
                      "display,status,start_date,encounter_id) VALUES (?,?,?,?,?,?,?)",
                      tuple(row.values()))

        if (i + 1) % 50 == 0:
            print(f"  Processed {i + 1}/{len(json_files)} files...")

    conn.commit()

    # Print summary
    print("\n--- Database Summary ---")
    for table in ["patients", "conditions", "medications", "observations",
                  "encounters", "procedures", "immunizations",
                  "care_plan_activities", "organizations"]:
        count = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count} rows")

    conn.close()
    print(f"\nDatabase saved to {DB_PATH}")


if __name__ == "__main__":
    main()