"""
add_interactions.py — Add verified drug pairs that DDInter coverage missed.

    python add_interactions.py            # dry run, shows what would change
    python add_interactions.py --apply    # write to the database

Every pair here must be verified against a real clinical source before it goes
in, and the citation recorded. This file is a coverage patch on DDInter, not a
place to record things a language model asserted.

Rows added here are marked source='curated' so the interface can distinguish
them from DDInter-derived rows.
"""

import sqlite3
import sys

# (generic_a, generic_b, severity, citation)
#
# VERIFY EACH ONE BEFORE ENABLING IT. The two below are starting points from
# the failures your verification layer caught — confirm against the FDA label
# or a drug-interaction reference, paste the citation, then uncomment.
SUPPLEMENT = [
    # ("amlodipine", "simvastatin", "major",
    #  "FDA simvastatin label — limit to 20 mg daily with amlodipine"),
    # ("simvastatin", "tacrolimus", "major",
    #  "CITATION HERE"),
]


def active_pairs(conn, a, b):
    """Patients with both drugs active, with the prescription text for each."""
    return conn.execute(
        """
        SELECT DISTINCT p.patient_id, ma.display, mb.display
        FROM patients p
        JOIN medications ma ON ma.patient_id = p.patient_id
                           AND ma.status = 'active' AND LOWER(ma.display) LIKE ?
        JOIN medications mb ON mb.patient_id = p.patient_id
                           AND mb.status = 'active' AND LOWER(mb.display) LIKE ?
        WHERE p.deceased_date = '' OR p.deceased_date IS NULL
        """,
        (f"%{a}%", f"%{b}%"),
    ).fetchall()


def exists(conn, pid, a, b):
    return conn.execute(
        """
        SELECT 1 FROM drug_interactions
        WHERE patient_id = ?
          AND ((LOWER(drug_a_matched) LIKE ? AND LOWER(drug_b_matched) LIKE ?)
            OR (LOWER(drug_a_matched) LIKE ? AND LOWER(drug_b_matched) LIKE ?))
        """,
        (pid, f"%{a}%", f"%{b}%", f"%{b}%", f"%{a}%"),
    ).fetchone() is not None


def ensure_source_column(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(drug_interactions)")]
    if "source" not in cols:
        conn.execute("ALTER TABLE drug_interactions ADD COLUMN source TEXT "
                     "DEFAULT 'DDInter'")
        print("  added column drug_interactions.source (existing rows = DDInter)")


def main():
    apply_changes = "--apply" in sys.argv

    if not SUPPLEMENT:
        print("SUPPLEMENT is empty — nothing to add.\n"
              "Verify each pair, paste its citation, and uncomment it first.")
        return

    conn = sqlite3.connect("carethread.db")
    if apply_changes:
        ensure_source_column(conn)

    added = skipped = 0
    for a, b, severity, citation in SUPPLEMENT:
        print(f"\n{a} ↔ {b}  [{severity}]")
        print(f"  source: {citation}")
        for pid, display_a, display_b in active_pairs(conn, a, b):
            if exists(conn, pid, a, b):
                skipped += 1
                continue
            # Canonical ordering, matching the convention in drug_interactions.py
            first, second = sorted([(a, display_a), (b, display_b)])
            print(f"  + {pid[:8]}  {first[1]} / {second[1]}")
            if apply_changes:
                conn.execute(
                    "INSERT INTO drug_interactions (patient_id, drug_a, drug_b,"
                    " drug_a_matched, drug_b_matched, severity, source)"
                    " VALUES (?,?,?,?,?,?,'curated')",
                    (pid, first[1], second[1], first[0], second[0], severity),
                )
            added += 1

    if apply_changes:
        conn.commit()
        print(f"\nAdded {added} rows ({skipped} already present).")
        print("Regenerate affected briefs: python run_pipeline.py --count 40 --force")
    else:
        print(f"\nDry run — {added} rows would be added, {skipped} already present.")
        print("Rerun with --apply to write.")
    conn.close()


if __name__ == "__main__":
    main()
