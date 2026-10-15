"""
validate.py — Spot-check the full pipeline on a few patients.
Prints a mini pre-visit summary for manual review.
"""

import sqlite3


def summarize_patient(conn, patient_id):
    """Print a human-readable summary of one patient's data + gaps."""
    p = conn.execute(
        "SELECT given_name, family_name, birth_date, gender, zip_code "
        "FROM patients WHERE patient_id=?", (patient_id,)
    ).fetchone()

    if not p:
        print(f"Patient {patient_id} not found.")
        return

    print(f"\n{'='*60}")
    print(f"PATIENT: {p[0]} {p[1]}  |  DOB: {p[2]}  |  {p[3]}  |  ZIP: {p[4]}")
    print(f"{'='*60}")

    # Active conditions
    conds = conn.execute(
        "SELECT display, onset_date FROM conditions "
        "WHERE patient_id=? AND clinical_status='active' ORDER BY onset_date",
        (patient_id,)
    ).fetchall()
    print(f"\nActive Conditions ({len(conds)}):")
    for c in conds:
        print(f"  • {c[0]} (since {c[1]})")

    # Active medications
    meds = conn.execute(
        "SELECT display FROM medications WHERE patient_id=? AND status='active'",
        (patient_id,)
    ).fetchall()
    print(f"\nActive Medications ({len(meds)}):")
    for m in meds:
        print(f"  • {m[0]}")

    # Care gaps
    gaps = conn.execute(
        "SELECT gap_type, severity, description FROM care_gaps WHERE patient_id=?",
        (patient_id,)
    ).fetchall()
    print(f"\nCare Gaps ({len(gaps)}):")
    if gaps:
        for g in gaps:
            icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(g[1], "⚪")
            print(f"  {icon} [{g[1].upper()}] {g[0]}: {g[2]}")
    else:
        print("  None detected — all screenings current.")

    # Drug interactions
    # DDInter supplies only a severity level, so there is no description text —
    # show the names the displays were matched on instead, which is what needs
    # eyeballing when a flagged pair looks wrong.
    ixns = conn.execute(
        "SELECT drug_a, drug_b, severity, drug_a_matched, drug_b_matched "
        "FROM drug_interactions WHERE patient_id=?",
        (patient_id,)
    ).fetchall()
    print(f"\nDrug Interactions ({len(ixns)}):")
    if ixns:
        for ix in ixns:
            print(f"  ⚠️  [{ix[2].upper()}] {ix[0]}  ↔  {ix[1]}")
            print(f"     matched as: {ix[3]} ↔ {ix[4]}")
    else:
        print("  None detected.")

    print()


def main():
    conn = sqlite3.connect("carethread.db")

    # Find patients with the most gaps (best demo candidates)
    top_patients = conn.execute("""
        SELECT p.patient_id, p.given_name, p.family_name,
               COUNT(DISTINCT g.id) as gap_count
        FROM patients p
        LEFT JOIN care_gaps g ON p.patient_id = g.patient_id
        GROUP BY p.patient_id
        ORDER BY gap_count DESC
        LIMIT 5
    """).fetchall()

    print("Top 5 patients by gap count (best for demo):")
    for tp in top_patients:
        print(f"  {tp[1]} {tp[2]}: {tp[3]} gaps")

    # Show full summary for the top patient
    if top_patients:
        summarize_patient(conn, top_patients[0][0])

    conn.close()


if __name__ == "__main__":
    main()