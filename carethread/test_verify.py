"""
test_verify.py — Prove the verification layer still works.

    python test_verify.py
    CARETHREAD_DB=carethread_frag.db python test_verify.py   # another database

A pass rate only means something if the verifier can still fail things. This
feeds verify_brief.py inputs whose correct verdict is known in advance: real
interactions it must accept, fabricated ones it must reject. Run it after any
change to verify_brief.py, and before trusting a batch run.

Exits 1 if any case behaves wrongly, so it can go in a pre-commit hook.
"""

import sqlite3
import sys

from assemble_patient import assemble_patient_data
from care_gaps import gap_depression_followup
from config import DB_PATH
from provenance import trace_alerts
from verify_brief import day_count_issues, verify_brief

DB = DB_PATH


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
        "social_history": [],
        "community_context": [],
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


def depression_rule_cases():
    """gap_depression_followup on hand-built histories in an in-memory DB:
    (label, PHQ-2 screens as (date, score), encounter dates, expect a gap)."""
    cases = [
        ("old positive, later negative", [("2022-01-04", 4), ("2024-03-01", 0)], [], False),
        ("latest positive, no follow-up", [("2022-01-04", 0), ("2025-05-01", 3)], [], True),
        ("latest positive, follow-up in 30d", [("2025-05-01", 5)], ["2025-05-20"], False),
        ("latest positive, follow-up at 45d", [("2025-05-01", 5)], ["2025-06-15"], True),
        ("old unfollowed, latest followed", [("2021-01-01", 6), ("2025-05-01", 4)],
         ["2025-05-10"], False),
        ("no screens", [], [], False),
    ]
    results = []
    for label, screens, encounters, expect_gap in cases:
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE observations (patient_id, code, value, effective_date)")
        db.execute("CREATE TABLE encounters (patient_id, start_date)")
        db.executemany("INSERT INTO observations VALUES ('p', '55758-7', ?, ?)",
                       [(str(score), date) for date, score in screens])
        db.executemany("INSERT INTO encounters VALUES ('p', ?)",
                       [(date,) for date in encounters])
        got_gap = gap_depression_followup(db, "p") is not None
        db.close()
        ok = got_gap == expect_gap
        got, want = ("gap" if got_gap else "no gap"), ("gap" if expect_gap else "no gap")
        print(f"  {label:38} {got:9} (want {want:8}) {'OK' if ok else '<-- WRONG'}")
        results.append(ok)
    return results


def day_count_cases():
    """Check 7 on a fixed record and generation date, so the expected
    figures don't move with today's date: (label, generated on, text,
    expected issue type or None)."""
    gaps = [{"gap": "BMI Screening Overdue", "last_date": "2025-09-23",
             "days_overdue": 36,
             "description": "Last BMI was 401 days ago (27.31 kg/m2). Due annually."}]
    cases = [
        ("correct 'overdue by N days'", "2026-10-29", "Calculate BMI (overdue by 36 days).", "day_count"),
        ("correct 'N days ago'", "2026-10-29", "Last BMI was 401 days ago.", "day_count"),
        ("days since given as overdue", "2026-10-29", "Calculate BMI (overdue by 401 days).",
         "day_count_mismatch"),
        ("count true later, not on gen date", "2026-10-29", "Calculate BMI (overdue by 70 days).",
         "day_count_mismatch"),
        ("same count on its gen date", "2026-12-01", "Calculate BMI (overdue by 70 days).", "day_count"),
        ("date and interval", "2026-10-29", "BMI last done 2025-09-23; due annually.", None),
        ("intervals aren't counts", "2026-10-29",
         "Due every 180 days; follow up within 30 days; a 90-day supply.", None),
        ("pack size in a drug name", "2026-10-29",
         "Natazia 28 Day Pack; 21 DAY ethinyl estradiol vaginal ring.", None),
    ]
    results = []
    for label, generated, text, expect in cases:
        brief = {"_meta": {"generated_on": generated}, "priority_actions": [text]}
        kinds = {i["type"] for i in day_count_issues(brief, gaps)}
        ok = kinds == ({expect} if expect else set())
        got = ", ".join(sorted(kinds)) or "accepted"
        print(f"  {label:38} {got:18} (want {expect or 'accepted'}) "
              f"{'OK' if ok else '<-- WRONG'}")
        results.append(ok)
    return results


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

    # Drug synonyms: the record stores the DDInter name (salbutamol), the
    # model may write the US one (albuterol)
    syn = {**data, "drug_interactions": [{
        "drug_a": "albuterol 0.83 MG/ML Inhalation Solution",
        "drug_b": "budesonide 0.25 MG/ML Inhalation Suspension",
        "drug_a_generic": "salbutamol", "drug_b_generic": "budesonide",
        "severity": "minor"}]}
    results.append(check(
        "albuterol for salbutamol", skeleton(pid, syn, medication_alerts=alert(
            ["albuterol", "budesonide"])), syn,
        "hallucinated_interaction", False))
    results.append(check(
        "different drug, synonym pair on record", skeleton(pid, syn, medication_alerts=alert(
            ["albuterol", "fluticasone"])), syn,
        "hallucinated_interaction", True))

    # The tracer (provenance.py) must cite the same synonym pair, so the page
    # shows a source rather than "unverified"
    print("\nTracer drug synonyms (provenance)")
    for label, drugs, expect_traced in [
            ("albuterol traced to salbutamol row", ["albuterol", "budesonide"], True),
            ("different drug not traced", ["albuterol", "fluticasone"], False)]:
        traced = bool(trace_alerts({"medication_alerts": alert(drugs)}, syn)[0]["sources"])
        ok = traced == expect_traced
        got, want = ("traced" if traced else "untraced"), ("traced" if expect_traced else "untraced")
        print(f"  {label:38} {got:9} (want {want:8}) {'OK' if ok else '<-- WRONG'}")
        results.append(ok)

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

    # Removed care gaps (check 3b): a patient with gaps on record but no
    # depression gap, whose brief still lists one
    gpid = conn.execute("""
        SELECT patient_id FROM care_gaps GROUP BY patient_id
        HAVING SUM(gap_type = 'Depression Follow-Up Missing') = 0
        LIMIT 1""").fetchone()
    if gpid:
        gdata = assemble_patient_data(conn, gpid[0])
        described = [{"gap": f"{g['gap']}: {g['description']}"} for g in gdata["care_gaps"]]
        print("\nRemoved care gaps (check 3b)")
        results.append(check(
            "all listed gaps on record", skeleton(gpid[0], gdata, care_gaps=described),
            gdata, "care_gap_not_in_record", False))
        results.append(check(
            "lists a removed depression gap", skeleton(gpid[0], gdata, care_gaps=described + [
                {"gap": "Depression Follow-Up Missing: PHQ-2 score of 4.0 on 2022-01-04 "
                        "with no follow-up encounter within 30 days."}]),
            gdata, "care_gap_not_in_record", True))

    # Condition synonyms (checks 1 and 5): one patient with a malignant
    # neoplasm on record, one with nothing cancer-like at all
    with_cancer = conn.execute("""
        SELECT patient_id FROM conditions
        WHERE clinical_status = 'active' AND display LIKE 'Malignant neoplasm of breast%'
        LIMIT 1""").fetchone()
    without_cancer = conn.execute("""
        SELECT patient_id FROM patients WHERE patient_id NOT IN (
            SELECT patient_id FROM conditions
            WHERE lower(display) LIKE '%neoplasm%' OR lower(display) LIKE '%cancer%'
               OR lower(display) LIKE '%carcinoma%')
        LIMIT 1""").fetchone()

    print("\nCondition synonyms (checks 1 and 5)")
    if with_cancer:
        cpid = with_cancer[0]
        cdata = assemble_patient_data(conn, cpid)
        results.append(check(
            "'cancer' in summary, neoplasm on record", skeleton(cpid, cdata,
                patient_summary="History of breast cancer."), cdata,
            "hallucinated_condition_in_summary", False))
        results.append(check(
            "'Breast cancer' as key condition", skeleton(cpid, cdata, key_conditions=[
                {"condition": "Breast cancer"}]), cdata, "hallucinated_condition", False))
        results.append(check(
            "'Lung cancer' as key condition", skeleton(cpid, cdata, key_conditions=[
                {"condition": "Lung cancer"}]), cdata, "hallucinated_condition", True))
    if without_cancer:
        npid = without_cancer[0]
        ndata = assemble_patient_data(conn, npid)
        results.append(check(
            "'cancer' in summary, none on record", skeleton(npid, ndata,
                patient_summary="History of breast cancer."), ndata,
            "hallucinated_condition_in_summary", True))

    # Check 6 needs a patient whose ZIP is in the sdoh table, and one whose isn't
    with_sdoh = conn.execute("""
        SELECT p.patient_id FROM patients p JOIN sdoh s ON p.zip_code = s.zip_code
        WHERE s.pct_poverty IS NOT NULL LIMIT 1""").fetchone()
    without_sdoh = conn.execute("""
        SELECT patient_id FROM patients WHERE zip_code = '00000' LIMIT 1""").fetchone()

    print("\nCommunity SDOH figures (check 6)")
    if with_sdoh:
        spid = with_sdoh[0]
        sdata = assemble_patient_data(conn, spid)
        real = sdata["sdoh"]["pct_poverty"]
        values = [v for v in sdata["sdoh"].values() if v is not None]
        fake = round(real + 17.3, 1)
        while any(abs(fake - v) <= 0.05 for v in values):
            fake = round(fake + 3.1, 1)
        zip_code = sdata["demographics"]["zip_code"]
        results.append(check(
            "real poverty rate", skeleton(spid, sdata, community_context=[
                f"In the patient's ZIP code ({zip_code}), {real}% of residents "
                f"live below the federal poverty line."]), sdata,
            "fabricated_community_number", False))
        results.append(check(
            "fabricated poverty rate", skeleton(spid, sdata, community_context=[
                f"In the patient's ZIP code ({zip_code}), {fake}% of residents "
                f"live below the federal poverty line."]), sdata,
            "fabricated_community_number", True))
    if without_sdoh:
        npid = without_sdoh[0]
        ndata = assemble_patient_data(conn, npid)
        results.append(check(
            "community context, no ZIP data", skeleton(npid, ndata, community_context=[
                "In the patient's ZIP code, 12.5% of residents live below "
                "the federal poverty line."]), ndata,
            "community_data_without_source", True))

    conn.close()

    print("\nDepression follow-up rule (only the most recent PHQ-2 counts):")
    results += depression_rule_cases()

    print("\nDay counts (check 7) on a fixed record:")
    results += day_count_cases()

    # The verdict each kind of day count gives, on a real patient's gaps
    dated = [g for g in data["care_gaps"] if g["days_overdue"] is not None]
    if dated:
        g = dated[0]
        print(f"\nDay-count verdicts on {data['demographics']['name']}'s "
              f"'{g['gap']}' ({g['days_overdue']} days overdue in the record):")
        for label, n, want in (("matching count", g["days_overdue"], "WARN"),
                               ("wrong count", g["days_overdue"] + 500, "FAIL")):
            brief = skeleton(pid, data, priority_actions=[
                f"Address {g['gap']} (overdue by {n} days)."])
            got = verify_brief(brief, data)["verdict"]
            ok = got == want
            print(f"  {label:38} {got:9} (want {want:8}) {'OK' if ok else '<-- WRONG'}")
            results.append(ok)

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} cases correct")
    if failed:
        print("The verifier is not behaving as intended. Do not trust pass "
              "rates until this is fixed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
