"""
build_frag_db.py — Build carethread_frag.db, the record-fragmentation copy of
the database, with every record tagged by its source organization.

    python build_frag_db.py              # → carethread_frag.db
    python build_frag_db.py other.db     # → other.db

Then run the app against it:

    CARETHREAD_DB=carethread_frag.db python app.py

Steps (no LLM calls; briefs are not touched):
  1. parse_fhir.py      clinical tables + organizations, from the Synthea bundles
  2. copy sdoh and feedback from carethread.db, which is only read
  3. sources.py         home clinic per patient
  4. care_gaps.py, drug_interactions.py   the existing rules, unchanged
  5. clinic_view.py     the same rules on home-clinic records only
  6. compare the briefed patients' gaps and interactions with carethread.db:
     gaps against the rules rerun today on carethread.db (read-only), since
     its stored day counts date from its last build; interactions row for row
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

SOURCE_DB = "carethread.db"
FHIR_DIR = Path("..") / "synthea" / "output" / "fhir"
BRIEFS_DIR = Path("briefs")

# Columns that must match between the two databases (everything but the id)
COMPARE = {
    "care_gaps": "patient_id, gap_type, severity, description, guideline, "
                 "qualifying_condition, last_date, days_overdue",
    "drug_interactions": "patient_id, drug_a, drug_b, drug_a_matched, "
                         "drug_b_matched, severity",
}


def run(target, *args):
    """Run a pipeline script against the target database."""
    print(f"\n$ CARETHREAD_DB={target} python {' '.join(map(str, args))}")
    subprocess.run([sys.executable, *map(str, args)], check=True,
                   env={**os.environ, "CARETHREAD_DB": target})


def copy_tables(target, tables):
    """Copy tables from carethread.db as they are. The source is opened
    read-only, so this can never write to it."""
    conn = sqlite3.connect(target)
    conn.execute(f"ATTACH 'file:{SOURCE_DB}?mode=ro' AS src")
    for table in tables:
        sql = conn.execute("SELECT sql FROM src.sqlite_master WHERE name=?",
                           (table,)).fetchone()
        if not sql:
            print(f"  {table}: not in {SOURCE_DB}, skipped")
            continue
        conn.execute(f"DROP TABLE IF EXISTS main.{table}")
        conn.execute(sql[0])
        conn.execute(f"INSERT INTO main.{table} SELECT * FROM src.{table}")
        n = conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
        print(f"  {table}: {n} rows copied")
    conn.commit()
    conn.execute("DETACH src")
    conn.close()


def fresh_gaps(target, patient_ids):
    """The gap rules run today on an in-memory copy of carethread.db, which is
    only read. Its stored care_gaps rows date from whenever care_gaps.py last
    ran, so their day counts lag; rerunning the rules compares like with like.
    A carethread.db parsed before encounters had encounter_code gets the codes
    from the target, matched by encounter id."""
    from care_gaps import detect_gaps_for_patient
    src = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    conn = sqlite3.connect(":memory:")
    src.backup(conn)
    src.close()
    columns = {r[1] for r in conn.execute("PRAGMA table_info(encounters)")}
    if "encounter_code" not in columns:
        print(f"  ({SOURCE_DB} has no encounter_code yet; using {target}'s)")
        conn.execute(f"ATTACH 'file:{target}?mode=ro' AS t")
        conn.execute("ALTER TABLE encounters ADD COLUMN encounter_code TEXT")
        conn.execute("UPDATE encounters SET encounter_code = (SELECT x.encounter_code "
                     "FROM t.encounters x WHERE x.encounter_id = encounters.encounter_id)")
    rows = [tuple(g[c.strip()] for c in COMPARE["care_gaps"].split(","))
            for pid in patient_ids for g in detect_gaps_for_patient(conn, pid)]
    conn.close()
    return sorted(rows, key=repr)


def compare(target, patient_ids):
    """Rows per table that differ between carethread.db and the target, for
    these patients. Both sides are compared as multisets."""
    conn = sqlite3.connect(target)
    conn.execute(f"ATTACH 'file:{SOURCE_DB}?mode=ro' AS src")
    marks = ",".join("?" * len(patient_ids))
    ok = True
    for table, cols in COMPARE.items():
        def rows(schema):
            return sorted(conn.execute(
                f"SELECT {cols} FROM {schema}.{table} WHERE patient_id IN ({marks})",
                patient_ids).fetchall(), key=repr)
        old = fresh_gaps(target, patient_ids) if table == "care_gaps" else rows("src")
        new = rows("main")
        same = old == new
        ok &= same
        print(f"  {table}: {len(old)} rows in {SOURCE_DB}, {len(new)} in {target} "
              f"— {'identical' if same else 'DIFFERENT'}")
        if not same:
            for r in sorted(set(old) ^ set(new), key=repr)[:10]:
                print(f"    {'only in ' + SOURCE_DB if r in old else 'only in ' + target}: {r}")
    conn.close()
    return ok


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "carethread_frag.db"
    if Path(target).resolve() == Path(SOURCE_DB).resolve():
        raise SystemExit(f"Refusing to rebuild {SOURCE_DB}; pick another file.")

    # parse_fhir appends, so start from an empty file
    Path(target).unlink(missing_ok=True)

    run(target, "parse_fhir.py", FHIR_DIR)
    print(f"\nCopying from {SOURCE_DB} (read-only):")
    copy_tables(target, ["sdoh", "feedback"])
    run(target, "sources.py")
    run(target, "care_gaps.py")
    run(target, "drug_interactions.py")
    run(target, "clinic_view.py")

    briefed = sorted(p.stem for p in BRIEFS_DIR.glob("*.json"))
    print(f"\nComparing the {len(briefed)} briefed patients with {SOURCE_DB}:")
    ok = compare(target, briefed)

    from clinic_view import totals
    conn = sqlite3.connect(target)
    counts, patients = totals(conn, briefed)
    conn.close()
    print(f"\nClinic-only vs CareThread view, {len(briefed)} briefed patients "
          f"({patients['any']} affected):")
    print(f"  done elsewhere:        {counts['done_elsewhere']} gaps "
          f"({patients['done_elsewhere']} patients)")
    print(f"  unknown to the clinic: {counts['unknown']} interactions, "
          f"{counts['unknown_major']} major ({patients['unknown']} patients)")
    print(f"  missed by the clinic:  {counts['missed']} gaps "
          f"({patients['missed']} patients)")
    print(f"  history elsewhere:     {counts['history']} gaps "
          f"({patients['history']} patients)")
    print("\nMATCH" if ok else "\nMISMATCH — see rows above")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
