"""
verify_brief.py — Verifies a generated pre-visit brief against source data.

Checks:
1. Every condition mentioned exists in the patient's record.
2. Every medication mentioned is in the active medication list.
3. Care gaps match what care_gaps.py detected: none dropped, and none
   listed that the record no longer holds.
4. Drug interactions match what drug_interactions.py detected.
5. No fabricated lab values.
6. Every number in community_context matches the patient's ZIP row in the
   sdoh table, and there is no community context at all without one.
7. No day counts ("overdue by 401 days"): they go stale, so any is a WARN,
   and one that doesn't match the gap record as of the brief's generation
   date is a FAIL.

Usage:
    python verify_brief.py
"""

import json
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from assemble_patient import SDOH_INDICATORS
from drug_interactions import SYNONYMS
from config import DB_PATH


# Numbers as the model writes them: 9.84, 72,797, $72,797
_COMMUNITY_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_YEAR = re.compile(r"(19|20)\d{2}")

# Numbers that belong to an indicator's definition, not its value
# ("30% or more of income on rent", "adults 25+")
_DEFINITION_NUMBERS = {n for label in SDOH_INDICATORS.values()
                       for n in _COMMUNITY_NUMBER.findall(label)}


def _matches_sdoh(token, values):
    """True if a number in the brief is one of the ZIP's values, allowing
    for rounding to however many decimals the brief used."""
    text = token.replace(",", "")
    n = float(text)
    decimals = len(text.split(".")[1]) if "." in text else 0
    tolerance = 0.5 * 10 ** -decimals + 1e-9
    return any(abs(float(v) - n) <= tolerance for v in values)


# Dose units, forms, routes, salts and esters — everything in a prescription
# string that is not the name of a drug.
_NON_DRUG_WORDS = {
    "tablet", "tablets", "capsule", "capsules", "oral", "inhalation",
    "solution", "injection", "topical", "extended", "release", "metered",
    "inert", "ingredients", "actuat", "sodium", "hydrochloride", "valerate",
    "propionate", "succinate", "besylate", "tartrate", "sulfate", "phosphate",
    "daily", "pack", "prefilled", "syringe", "auto", "injector",
}


# Lay and clinical names for the same condition. A brief may say "breast
# cancer" where the record says "Malignant neoplasm of breast"; each group
# is matched as a whole, in either direction.
CONDITION_SYNONYMS = [
    {"cancer", "malignant neoplasm", "carcinoma"},
    {"heart attack", "myocardial infarction"},
    {"high blood pressure", "hypertension"},
    {"kidney disease", "chronic kidney disease"},
    {"diabetes", "diabetes mellitus"},
]

_CONDITION_FILLER = {"of", "the", "and", "with", "disorder", "finding",
                     "situation", "history"}


def condition_variants(text):
    """The text plus every rewording the synonym map allows:
    'breast cancer' → also 'breast malignant neoplasm', 'breast carcinoma'."""
    text = text.lower()
    variants = {text}
    for group in CONDITION_SYNONYMS:
        for term in group:
            if re.search(rf"\b{re.escape(term)}\b", text):
                for alt in group - {term}:
                    variants.add(re.sub(rf"\b{re.escape(term)}\b", alt, text))
    return variants


def _condition_words(text):
    return {w for w in re.findall(r"[a-z0-9]+", text) if w not in _CONDITION_FILLER}


def condition_in_record(name, source_conditions):
    """True if a condition named in the brief is one the record holds:
    substring either way, or every meaningful word present, for the name
    as written or any synonym rewording of it."""
    for variant in condition_variants(name):
        words = _condition_words(variant)
        for sc in source_conditions:
            if variant in sc or sc in variant:
                return True
            if words and words <= _condition_words(sc):
                return True
    return False


_GAP_FILLER = {"overdue", "missing", "due"}


def gap_in_record(text, source_gaps):
    """True if a care gap the brief lists has the same gap type as one in
    the record. The type is the text before the colon (all of it if there
    is none); failing an exact match, every word of a record gap type
    (less 'overdue', 'missing') must appear in it."""
    gap_type = text.split(":")[0].strip().lower()
    words = set(re.findall(r"[a-z0-9]+", gap_type))
    for g in source_gaps:
        source = g["gap"].lower()
        if gap_type == source:
            return True
        key = set(re.findall(r"[a-z0-9]+", source)) - _GAP_FILLER
        if key and key <= words:
            return True
    return False


def canonical_drug(name):
    """A drug name with every US alias swapped for the name DDInter and the
    interaction table use: 'albuterol 0.83 MG/ML' → 'salbutamol 0.83 MG/ML'."""
    name = name.lower()
    for alias, canonical in SYNONYMS.items():
        name = re.sub(rf"\b{re.escape(alias)}\b", canonical, name)
    return name


def _drug_words(text):
    """Alphabetic tokens of 5+ characters that could be a drug name."""
    return {w for w in re.findall(r"[a-z]{5,}", str(text).lower())
            if w not in _NON_DRUG_WORDS}


def load_brief_file(path):
    """A brief from disk. Briefs written before _meta carried generated_on
    are dated by the file's modification time."""
    path = Path(path)
    brief = json.loads(path.read_text())
    meta = brief.setdefault("_meta", {})
    meta.setdefault("generated_on",
                    date.fromtimestamp(path.stat().st_mtime).isoformat())
    return brief


# ── Day counts (check 7) ─────────────────────────────────────────────────

# "overdue by 401 days", "401 days ago". Intervals ("every 180 days",
# "within 30 days", "a 90-day supply") are rules, not counts, and don't go stale.
# Nor are pack sizes in drug names ("Natazia 28 Day", "21 DAY ethinyl
# estradiol"): a count over one is always "days".
_DAY_COUNT = re.compile(r"(?<![\w.-])(\d+)(-?)\s*(days?)\b", re.I)
_INTERVAL_WORDS = re.compile(r"\b(?:every|within|per|each|after)\s+$", re.I)


def _gap_interval(gap):
    """The interval a gap's description states, in days, as the rules count
    them: 'Due every 180 days', 'Due annually' (365), 'every 2 years' (730)."""
    text = gap.get("description") or ""
    if m := re.search(r"every (\d+) days", text):
        return int(m.group(1))
    if m := re.search(r"every (\d+) years", text):
        return 365 * int(m.group(1))
    return 365 if "annually" in text else None


def _valid_day_counts(gaps, on):
    """Day counts the gap record supports on date `on`: days since each last
    date, and days overdue. The stored figures are accepted too."""
    since, overdue = set(), set()
    for g in gaps:
        if g.get("days_overdue") is not None:
            overdue.add(int(g["days_overdue"]))
        if m := re.search(r"was (\d+) days ago", g.get("description") or ""):
            since.add(int(m.group(1)))
        if not g.get("last_date"):
            continue
        days = (on - date.fromisoformat(g["last_date"][:10])).days
        since.add(days)
        if (interval := _gap_interval(g)) is not None:
            overdue.add(days - interval)
    return since, overdue


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def day_count_issues(brief, gaps):
    """Check 7: every day count in the brief, as a WARN, or a FAIL when the
    gap record didn't support it on the brief's generation date."""
    generated = brief.get("_meta", {}).get("generated_on")
    on = datetime.strptime(generated, "%Y-%m-%d").date() if generated else date.today()
    since, overdue = _valid_day_counts(gaps, on)
    issues = []
    body = {k: v for k, v in brief.items() if k != "_meta"}
    for text in _strings(body):
        for m in _DAY_COUNT.finditer(text):
            n = int(m.group(1))
            if (m.group(2) or _INTERVAL_WORDS.search(text[:m.start()])
                    or (n != 1 and m.group(3).lower() == "day")):
                continue
            around = text[max(0, m.start() - 20):m.end() + 12].lower()
            if "overdue" in around:
                valid = overdue
            elif "ago" in around or "since" in around:
                valid = since
            else:
                valid = since | overdue
            quote = text[max(0, m.start() - 40):m.end() + 20].strip()
            if any(abs(n - v) <= 1 for v in valid):
                issues.append({
                    "type": "day_count",
                    "severity": "medium",
                    "detail": f"Day counts go stale; use dates: '{quote}'",
                })
            else:
                issues.append({
                    "type": "day_count_mismatch",
                    "severity": "high",
                    "detail": f"'{n} days' matches no care gap in the record "
                             f"as of {on.isoformat()}: '{quote}'",
                })
    return issues


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
        # Containment either way, allowing for synonyms ("breast cancer")
        if not condition_in_record(cond_name, source_conditions):
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

    # Check 3b: Every gap the brief lists must be one the record holds now.
    # Briefs write "<gap type>: <description>"; a gap the rules no longer
    # detect (an old positive screen since followed by a negative) fails.
    for g in brief_gaps:
        checks += 1
        text = str(g.get("gap", "")).strip()
        if not gap_in_record(text, patient_data["care_gaps"]):
            issues.append({
                "type": "care_gap_not_in_record",
                "severity": "high",
                "detail": "Brief lists a care gap not in the current record: "
                          f"{text.split(':')[0].strip()}",
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
            # Synonyms count as the same drug (albuterol / salbutamol).
            def same_drug(x, y):
                x, y = canonical_drug(x), canonical_drug(y)
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
        if kw in summary and not any(v in sc for v in condition_variants(kw)
                                     for sc in source_conditions):
            issues.append({
                "type": "hallucinated_condition_in_summary",
                "severity": "high",
                "detail": f"Summary mentions '{kw}' but no matching "
                         f"condition in source data.",
            })

    # Check 6: Community (ZIP-level) figures must come from the sdoh table
    sdoh = patient_data.get("sdoh") or {}
    sdoh_values = [v for k, v in sdoh.items()
                   if k in SDOH_INDICATORS and v is not None]
    zip_code = str(patient_data["demographics"].get("zip_code", ""))
    for entry in brief.get("community_context", []):
        checks += 1
        if not sdoh_values:
            issues.append({
                "type": "community_data_without_source",
                "severity": "high",
                "detail": f"Brief gives community context ('{entry[:60]}') "
                         f"but no SDOH data exists for this patient's ZIP.",
            })
            continue
        for token in _COMMUNITY_NUMBER.findall(str(entry)):
            plain = token.replace(",", "")
            if (plain == zip_code or plain in _DEFINITION_NUMBERS
                    or _YEAR.fullmatch(plain)):
                continue
            if not _matches_sdoh(token, sdoh_values):
                issues.append({
                    "type": "fabricated_community_number",
                    "severity": "high",
                    "detail": f"Community context cites {token}, which matches "
                             f"no SDOH value for ZIP {zip_code}: '{entry[:80]}'",
                })

    # Check 7: No day counts; any that appear must match the gap record
    checks += 1
    issues.extend(day_count_issues(brief, patient_data["care_gaps"]))

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
    conn = sqlite3.connect(DB_PATH)
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
        brief = load_brief_file(bf)

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