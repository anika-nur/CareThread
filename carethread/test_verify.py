"""
test_verify.py — Prove the verification layer still works.

    python test_verify.py

A pass rate only means something if the verifier can still fail things. This
feeds verify_brief.py inputs whose correct verdict is known in advance: real
interactions it must accept, fabricated ones it must reject. Run it after any
change to verify_brief.py, and before trusting a batch run.

Exits 1 if any case behaves wrongly, so it can go in a pre-commit hook.
"""

import sqlite3
import sys

from assemble_patient import assemble_patient_data
from verify_brief import verify_brief

DB = "carethread.db"


def skeleton(pid, data, **overrides):
    """A brief that passes every check except the one under test."""
    brief = {
        "_meta": {"patient_id": pid, "patient_name": data["demographics"]["name"]},
        "patient_summary": "",
        "key_conditions": [],
        "current_medications": [],
        "medication_alerts": [],
        "care_gaps": [{"gap": g["gap"]} for g in data["care_gaps"]],
        "recent_activity": "",
        "sdoh_flags": [],
        "priority_actions": [],
    }
    brief.update(overrides)
    return brief


def issues_of(brief, data, kind):
    return [i for i in verify_brief(brief, data)["issues"] if i["type"] == kind]


def check(label, brief, data, kind, expect_flagged):
    flagged = bool(issues_of(brief, data, kind))
    ok = flagged == expect_flagged
    got = "FLAGGED" if flagged else "accepted"
    want = "FLAGGED" if expect_flagged else "accepted"
    print(f"  {label:38} {got:9} (want {want:8}) {'OK' if ok else '<-- WRONG'}")
    return ok


def alert(drugs):
    return [{"type": "drug_interaction", "severity": "major",
             "drugs": drugs, "recommendation": "x"}]


def main():
    conn = sqlite3.connect(DB)

    row = conn.execute("""
        SELECT patient_id FROM drug_interactions
        GROUP BY patient_id ORDER BY COUNT(*) DESC LIMIT 1
    """).fetchone()
    if not row:
        print("No drug interactions in the database — can't test check 4.")
        return 1

    pid = row[0]
    data = assemble_patient_data(conn, pid)
    ix = data["drug_interactions"][0]
    cond = data["conditions"][0]["condition"] if data["conditions"] else None
    med = data["medications"][0]["medication"] if data["medications"] else None

    print(f"\nTest patient: {data['demographics']['name']}")
    print(f"Real interaction: {ix['drug_a_generic']} / {ix['drug_b_generic']}\n")

    results = []

    print("Drug interactions (check 4)")
    results.append(check(
        "generic names", skeleton(pid, data, medication_alerts=alert(
            [ix["drug_a_generic"], ix["drug_b_generic"]])), data,
        "hallucinated_interaction", False))
    results.append(check(
        "full prescription names", skeleton(pid, data, medication_alerts=alert(
            [ix["drug_a"], ix["drug_b"]])), data,
        "hallucinated_interaction", False))
    results.append(check(
        "reversed order", skeleton(pid, data, medication_alerts=alert(
            [ix["drug_b_generic"], ix["drug_a_generic"]])), data,
        "hallucinated_interaction", False))
    results.append(check(
        "fabricated pair", skeleton(pid, data, medication_alerts=alert(
            ["zzqdrugone", "zzqdrugtwo"])), data,
        "hallucinated_interaction", True))
    results.append(check(
        "one real drug + one invented", skeleton(pid, data, medication_alerts=alert(
            [ix["drug_a_generic"], "zzqdrugtwo"])), data,
        "hallucinated_interaction", True))
    results.append(check(
        "same drug listed twice", skeleton(pid, data, medication_alerts=alert(
            [ix["drug_a_generic"], ix["drug_a"]])), data,
        "hallucinated_interaction", True))

    if cond:
        print("\nConditions (check 1)")
        results.append(check(
            "real condition", skeleton(pid, data, key_conditions=[
                {"condition": cond}]), data, "hallucinated_condition", False))
        results.append(check(
            "fabricated condition", skeleton(pid, data, key_conditions=[
                {"condition": "Zzq syndrome"}]), data,
            "hallucinated_condition", True))

    if med:
        print("\nMedications (check 2)")
        results.append(check(
            "real medication", skeleton(pid, data, current_medications=[
                {"medication": med}]), data, "hallucinated_medication", False))
        results.append(check(
            "fabricated medication", skeleton(pid, data, current_medications=[
                {"medication": "zzqamycin 50 MG"}]), data,
            "hallucinated_medication", True))

    print("\nDropped care gaps (check 3)")
    if data["care_gaps"]:
        results.append(check(
            "all gaps present", skeleton(pid, data), data, "missing_gaps", False))
        results.append(check(
            "one gap dropped", skeleton(pid, data, care_gaps=[
                {"gap": g["gap"]} for g in data["care_gaps"][:-1]]), data,
            "missing_gaps", True))

    conn.close()

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} cases correct")
    if failed:
        print("The verifier is not behaving as intended. Do not trust pass "
              "rates until this is fixed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
