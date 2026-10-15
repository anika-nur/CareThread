"""
review_brief.py — Put a generated brief next to its source data so you can
check it. The mechanical checks run automatically; the rest is printed
side by side for your eyes.

Usage:
    python review_brief.py                 # most recently written brief
    python review_brief.py <patient_id>
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

from assemble_patient import assemble_patient_data

# Filler words that would otherwise make unrelated items look similar —
# "Mammogram Overdue" and "HbA1c Overdue" share nothing clinical.
STOP = {"of", "the", "a", "an", "and", "or", "for", "to", "in", "on", "with",
        "disorder", "finding", "situation", "history",
        "overdue", "due", "order", "schedule", "needed", "recommended"}


def words(text):
    return set(re.sub(r"[^a-z0-9 ]", " ", str(text).lower()).split()) - STOP


def best_match(needle, haystack, threshold=0.6):
    """
    Loose overlap match — clinical terms rarely repeat verbatim between the
    rule output and the model's phrasing. Scored against the shorter of the
    two term sets so a wordy action still matches a terse gap name.
    """
    nw = words(needle)
    if not nw:
        return None
    best, best_score = None, 0.0
    for item in haystack:
        hw = words(item)
        if not hw:
            continue
        score = len(nw & hw) / min(len(nw), len(hw))
        if score > best_score:
            best, best_score = item, score
    return best if best_score >= threshold else None


def check_care_gaps(brief, data):
    print("\n── 2. CARE GAP RECALL " + "─" * 40)
    source = [g["gap"] for g in data["care_gaps"]]
    found = [g.get("gap", "") for g in brief.get("care_gaps", [])]
    print(f"  detected by rules: {len(source)}   |   in brief: {len(found)}")

    dropped = [g for g in source if not best_match(g, found)]
    added = [g for g in found if not best_match(g, source)]

    for g in dropped:
        print(f"  DROPPED  {g}")
    for g in added:
        print(f"  INVENTED {g}   <- not in rule output")
    if not dropped and not added:
        print("  every detected gap appears in the brief, none added")


def check_interactions(brief, data):
    print("\n── 2b. DRUG INTERACTION RECALL " + "─" * 31)
    source = [f"{ix['drug_a_generic']} {ix['drug_b_generic']}"
              for ix in data["drug_interactions"]]
    found = [" ".join(a.get("drugs", []))
             for a in brief.get("medication_alerts", [])]
    print(f"  detected by rules: {len(source)}   |   in brief: {len(found)}")

    if not source and not found:
        print("  no interactions found for this patient — nothing to check")
        return

    for ix in source:
        if not best_match(ix, found):
            print(f"  DROPPED  {ix}")
    for a in found:
        if not best_match(a, source):
            print(f"  INVENTED {a}   <- not in rule output")
    if source and all(best_match(ix, found) for ix in source) \
            and all(best_match(a, source) for a in found):
        print("  every detected interaction appears in the brief, none added")


def check_hallucinations(brief, data):
    print("\n── 1. HALLUCINATION SURFACE " + "─" * 34)
    flagged = 0
    src_conditions = [c["condition"] for c in data["conditions"]]
    src_meds = [m["medication"] for m in data["medications"]]
    src_values = {str(l["value"]).strip() for l in data["recent_labs"]}

    for item in brief.get("key_conditions", []):
        name = item.get("condition", "")
        if not best_match(name, src_conditions):
            print(f"  UNSOURCED condition: {name}")
            flagged += 1

    for item in brief.get("current_medications", []):
        name = item.get("medication", "")
        if not best_match(name, src_meds):
            print(f"  UNSOURCED medication: {name}")
            flagged += 1

    # Any number in the prose that isn't a lab value, date, or age
    prose = " ".join([
        brief.get("patient_summary", ""),
        brief.get("recent_activity", ""),
        *[str(a) for a in brief.get("priority_actions", [])],
    ])
    # Numbers that legitimately appear in source text ("Type 2 diabetes",
    # doses, dates) shouldn't be flagged
    source_text = " ".join(src_conditions + src_meds + list(src_values) +
                           [str(l.get("date", "")) for l in data["recent_labs"]])
    known = set(re.findall(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])",
                           source_text))
    known |= src_values | {str(data["demographics"]["age"])}
    # Standalone numbers only — the '1' inside 'HbA1c' is not a measurement
    for num in set(re.findall(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])",
                              prose)):
        if num in known or re.fullmatch(r"(19|20)\d{2}", num):  # year
            continue
        if not any(num in v for v in src_values):
            print(f"  CHECK number '{num}' in prose — not a stored lab value")
            flagged += 1

    if flagged:
        print("  (these need your eyes — loose matching gives false flags)")
    else:
        print("  nothing flagged: every condition, medication, and number in "
              "the brief traces to the source data")


def check_ordering(brief, data):
    print("\n── 4. PRIORITY ORDERING " + "─" * 38)
    rank = {"high": 0, "medium": 1, "low": 2}
    severity_of = {g["gap"]: g["severity"] for g in data["care_gaps"]}

    seen = []
    for i, action in enumerate(brief.get("priority_actions", []), 1):
        match = best_match(action, list(severity_of))
        sev = severity_of.get(match, "—")
        seen.append(rank.get(sev, 9))
        print(f"  {i}. [{sev:>6}] {action}")

    ranked = [s for s in seen if s < 9]
    if ranked and ranked != sorted(ranked):
        print("  OUT OF ORDER — a lower-severity action precedes a higher one")
    elif ranked:
        print("  ranked actions are in descending severity order")
    if any(s == 9 for s in seen):
        print("  '—' = action doesn't correspond to a detected care gap "
              "(often drawn from conditions or SDOH). Not an error; the "
              "ordering check ignores these, so judge their placement yourself.")


def check_parse(brief):
    print("\n── 3. JSON PARSE " + "─" * 45)
    if brief.get("parse_error"):
        print("  FAILED to parse. First 500 chars the model actually returned:\n")
        print("  " + (brief.get("raw_text") or "")[:500].replace("\n", "\n  "))
        return False
    expected = ["patient_summary", "key_conditions", "current_medications",
                "medication_alerts", "care_gaps", "recent_activity",
                "sdoh_flags", "priority_actions"]
    missing = [k for k in expected if k not in brief]
    print("  parsed cleanly" + (f", MISSING KEYS: {missing}" if missing else
                                ", all 8 keys present"))
    return True


def review(brief, data):
    meta = brief.get("_meta", {})
    print("=" * 62)
    print(f"  {meta.get('patient_name', '?')}  |  {meta.get('model', '?')}")
    print("=" * 62)

    if not check_parse(brief):
        return
    check_hallucinations(brief, data)
    check_care_gaps(brief, data)
    check_interactions(brief, data)
    check_ordering(brief, data)

    print("\n── BRIEF AS WRITTEN " + "─" * 42)
    print(f"  {brief.get('patient_summary', '')}\n")
    print(f"  {brief.get('recent_activity', '')}")


def main():
    briefs = sorted(Path("briefs").glob("*.json"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    if not briefs:
        print("No briefs found. Run generate_brief.py first.")
        return

    if len(sys.argv) > 1:
        path = Path(f"briefs/{sys.argv[1]}.json")
        if not path.exists():
            print(f"No brief at {path}")
            return
    else:
        path = briefs[0]

    brief = json.loads(path.read_text())
    pid = brief.get("_meta", {}).get("patient_id") or path.stem

    conn = sqlite3.connect("carethread.db")
    data = assemble_patient_data(conn, pid)
    conn.close()

    if not data:
        print(f"Patient {pid} not in database.")
        return

    review(brief, data)


if __name__ == "__main__":
    main()