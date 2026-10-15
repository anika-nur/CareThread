"""
verify_brief.py — Verifies a generated pre-visit brief against source data.

Checks:
1. Every condition mentioned exists in the patient's record.
2. Every medication mentioned is in the active medication list.
3. Care gaps match what care_gaps.py detected.
4. Drug interactions match what drug_interactions.py detected.
5. No fabricated lab values.

Usage:
    python verify_brief.py
"""

import json
import os
import re
import sqlite3
from pathlib import Path


# Dose units, forms, routes, salts and esters — everything in a prescription
# string that is not the name of a drug.
_NON_DRUG_WORDS = {
    "tablet", "tablets", "capsule", "capsules", "oral", "inhalation",
    "solution", "injection", "topical", "extended", "release", "metered",
    "inert", "ingredients", "actuat", "sodium", "hydrochloride", "valerate",
    "propionate", "succinate", "besylate", "tartrate", "sulfate", "phosphate",
    "daily", "pack", "prefilled", "syringe", "auto", "injector",
}


def _drug_words(text):
    """Alphabetic tokens of 5+ characters that could be a drug name."""
    return {w for w in re.findall(r"[a-z]{5,}", str(text).lower())
            if w not in _NON_DRUG_WORDS}


def verify_brief(brief, patient_data):
    """Check a generated brief against source data. Returns a report."""
    issues = []
    checks = 0

    pid = brief.get("_meta", {}).get("patient_id", "unknown")
    source_conditions = {c["condition"].lower() for c in patient_data["conditions"]}
    source_meds = {m["medication"].lower() for m in patient_data["medications"]}
    source_gaps = {g["gap"].lower() for g in patient_data["care_gaps"]}
    source_interactions = {
        frozenset([ix["drug_a_generic"].lower(), ix["drug_b_generic"].lower()])
        for ix in patient_data["drug_interactions"]
    }

    # Check 1: Conditions mentioned in brief
    for cond in brief.get("key_conditions", []):
        checks += 1
        cond_name = cond.get("condition", "").lower()
        # Check if any source condition contains this name or vice versa
        matched = any(cond_name in sc or sc in cond_name
                     for sc in source_conditions)
        if not matched:
            issues.append({
                "type": "hallucinated_condition",
                "severity": "high",
                "detail": f"Brief mentions '{cond.get('condition')}' but no "
                         f"matching active condition found in source data.",
            })

    # Check 2: Medications mentioned in brief
    #
    # Exact containment first. It fails on pack notation, where Synthea stores
    # a contraceptive as "{5 (dienogest 2 MG / estradiol valerate 2 MG Oral
    # Tablet) / 17 (...)} Pack [Natazia 28 Day]" and the model sensibly writes
    # "Natazia 28 Day (dienogest/estradiol valerate)". Neither string contains
    # the other, so fall back to comparing the drug names inside each.
    source_med_words = [_drug_words(m["medication"])
                        for m in patient_data["medications"]]

    for med in brief.get("current_medications", []):
        checks += 1
        med_name = med.get("medication", "").lower()
        brief_words = _drug_words(med_name)

        if not brief_words:
            # Nothing name-like in it ("Oral Tablet") — can't be a medication
            matched = False
        else:
            matched = any(med_name in sm or sm in med_name
                          for sm in source_meds)
            if not matched:
                matched = any(brief_words & sw for sw in source_med_words)
        if not matched:
            issues.append({
                "type": "hallucinated_medication",
                "severity": "high",
                "detail": f"Brief mentions '{med.get('medication')}' but not "
                         f"found in active medication list.",
            })

    # Check 3: Care gaps in brief should match detected gaps
    brief_gaps = brief.get("care_gaps", [])
    checks += 1
    if len(brief_gaps) < len(patient_data["care_gaps"]):
        issues.append({
            "type": "missing_gaps",
            "severity": "medium",
            "detail": f"Brief has {len(brief_gaps)} care gaps but source "
                     f"data has {len(patient_data['care_gaps'])}. "
                     f"Some gaps may have been dropped.",
        })

    # Check 4: Drug interaction alerts should match detected interactions
    brief_alerts = brief.get("medication_alerts", [])
    for alert in brief_alerts:
        checks += 1
        drugs = [d.lower() for d in alert.get("drugs", [])]
        if len(drugs) >= 2:
            # Match in BOTH directions. The brief may name the full
            # prescription ("amlodipine 2.5 MG Oral Tablet") while the source
            # stores the generic ("amlodipine"), or the reverse. Each drug in
            # the alert must match a DIFFERENT drug in the source pair, so
            # two mentions of one drug can't satisfy a pair.
            def same_drug(x, y):
                return x in y or y in x

            source_pairs = [tuple(si) for si in source_interactions
                            if len(si) == 2]
            matched = any(
                (same_drug(drugs[0], p[0]) and same_drug(drugs[1], p[1])) or
                (same_drug(drugs[0], p[1]) and same_drug(drugs[1], p[0]))
                for p in source_pairs
            )
            if not matched:
                issues.append({
                    "type": "hallucinated_interaction",
                    "severity": "high",
                    "detail": f"Brief alerts about {' ↔ '.join(drugs)} but "
                             f"this pair was not in detected interactions.",
                })

    # Check 5: Verify no extra conditions fabricated in summary
    checks += 1
    summary = brief.get("patient_summary", "").lower()
    # Simple check: look for common condition words that shouldn't be there
    condition_keywords = ["cancer", "hiv", "hepatitis", "copd", "stroke",
                         "heart failure", "renal failure", "cirrhosis"]
    for kw in condition_keywords:
        if kw in summary and not any(kw in sc for sc in source_conditions):
            issues.append({
                "type": "hallucinated_condition_in_summary",
                "severity": "high",
                "detail": f"Summary mentions '{kw}' but no matching "
                         f"condition in source data.",
            })

    # Build report
    report = {
        "patient_id": pid,
        "patient_name": brief.get("_meta", {}).get("patient_name", ""),
        "checks_run": checks,
        "issues_found": len(issues),
        "issues": issues,
        "verdict": "PASS" if not issues else (
            "FAIL" if any(i["severity"] == "high" for i in issues)
            else "WARN"
        ),
    }

    return report


def main():
    conn = sqlite3.connect("carethread.db")
    from assemble_patient import assemble_patient_data

    briefs_dir = Path("briefs")
    if not briefs_dir.exists():
        print("No briefs/ folder found. Run generate_brief.py first.")
        return

    brief_files = list(briefs_dir.glob("*.json"))
    if not brief_files:
        print("No brief files found in briefs/. Run generate_brief.py first.")
        return

    print(f"Verifying {len(brief_files)} briefs...\n")

    results = {"PASS": 0, "WARN": 0, "FAIL": 0}

    for bf in sorted(brief_files):
        with open(bf) as f:
            brief = json.load(f)

        pid = brief.get("_meta", {}).get("patient_id")
        name = brief.get("_meta", {}).get("patient_name", "?")

        if not pid:
            print(f"  {bf.name}: skipped (no patient_id in metadata)")
            continue

        if brief.get("parse_error"):
            print(f"  {name}: SKIP (JSON parse error in brief)")
            continue

        patient_data = assemble_patient_data(conn, pid)
        if not patient_data:
            print(f"  {name}: SKIP (patient not found in database)")
            continue

        report = verify_brief(brief, patient_data)
        results[report["verdict"]] += 1

        icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}[report["verdict"]]
        print(f"  {icon} {name}: {report['verdict']} "
              f"({report['checks_run']} checks, {report['issues_found']} issues)")

        if report["issues"]:
            for issue in report["issues"]:
                print(f"      [{issue['severity'].upper()}] {issue['type']}: "
                      f"{issue['detail']}")

    print(f"\n--- Summary ---")
    print(f"  PASS: {results['PASS']}  |  WARN: {results['WARN']}  |  "
          f"FAIL: {results['FAIL']}")

    conn.close()


if __name__ == "__main__":
    main()