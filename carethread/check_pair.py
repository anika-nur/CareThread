"""
check_pair.py — Why isn't this drug pair being flagged?

    python check_pair.py amlodipine simvastatin

Distinguishes the two possible causes:
  (a) the pair isn't in DDInter at all        → needs a curated supplement
  (b) it is, but name matching missed it      → fix the matcher, not the data
"""

import csv
import sqlite3
import sys
from pathlib import Path

# Adjust if your DDInter CSV lives elsewhere
DDINTER_GLOB = "ddinter_data/*.csv"


def find_in_ddinter(a, b):
    paths = list(Path(".").glob(DDINTER_GLOB))
    if not paths:
        print(f"  ! no DDInter CSV found matching {DDINTER_GLOB}")
        print("    edit DDINTER_GLOB at the top of this script")
        return None

    hits, names_a, names_b = [], set(), set()
    for path in paths:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh):
                da = (row.get("Drug_A") or "").strip()
                db = (row.get("Drug_B") or "").strip()
                if a in da.lower():
                    names_a.add(da)
                if b in db.lower():
                    names_b.add(db)
                if b in da.lower():
                    names_a.add(da)
                if a in db.lower():
                    names_b.add(db)
                pair = {da.lower(), db.lower()}
                if any(a in d for d in pair) and any(b in d for d in pair):
                    hits.append((path.name, da, db, row.get("Level", "?")))

    print(f"  names containing '{a}': {sorted(names_a) or 'NONE'}")
    print(f"  names containing '{b}': {sorted(names_b) or 'NONE'}")
    if hits:
        for fname, da, db, level in hits:
            print(f"  PAIR IN DDINTER: {da} ↔ {db}  [{level}]  ({fname})")
    else:
        print("  PAIR NOT IN DDINTER")
    return hits


def patients_on_both(conn, a, b):
    # Living patients only, matching drug_interactions.py — deceased patients
    # are never checked, so listing them here would report them NOT FLAGGED.
    rows = conn.execute(
        """
        SELECT DISTINCT p.patient_id, p.given_name || ' ' || p.family_name
        FROM patients p
        JOIN medications ma ON ma.patient_id = p.patient_id
                           AND ma.status = 'active' AND LOWER(ma.display) LIKE ?
        JOIN medications mb ON mb.patient_id = p.patient_id
                           AND mb.status = 'active' AND LOWER(mb.display) LIKE ?
        WHERE p.deceased_date = '' OR p.deceased_date IS NULL
        """,
        (f"%{a}%", f"%{b}%"),
    ).fetchall()
    return rows


def already_flagged(conn, pid, a, b):
    return conn.execute(
        """
        SELECT 1 FROM drug_interactions
        WHERE patient_id = ?
          AND ((LOWER(drug_a_matched) LIKE ? AND LOWER(drug_b_matched) LIKE ?)
            OR (LOWER(drug_a_matched) LIKE ? AND LOWER(drug_b_matched) LIKE ?))
        """,
        (pid, f"%{a}%", f"%{b}%", f"%{b}%", f"%{a}%"),
    ).fetchone() is not None


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return
    a, b = sys.argv[1].lower(), sys.argv[2].lower()

    print(f"\n=== DDInter source data: {a} ↔ {b} ===")
    find_in_ddinter(a, b)

    conn = sqlite3.connect("carethread.db")
    print(f"\n=== Patients on both drugs ===")
    rows = patients_on_both(conn, a, b)
    if not rows:
        print("  none")
    for pid, name in rows:
        flag = "flagged" if already_flagged(conn, pid, a, b) else "NOT FLAGGED"
        print(f"  {name:38} {flag}")
    conn.close()


if __name__ == "__main__":
    main()
