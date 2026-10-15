"""
run_pipeline.py — Run the full CareThread pipeline on N patients.

Every run is saved to runs/run_<timestamp>.txt, and copied to runs/latest.txt.

Usage:
    python run_pipeline.py              # top 10 patients by gap count
    python run_pipeline.py --count 50   # top 50 patients
    python run_pipeline.py --all        # all living patients
"""

import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime

from assemble_patient import assemble_patient_data
from generate_brief import generate_brief
from verify_brief import verify_brief


class Tee:
    """Send everything printed to the terminal into a log file as well."""

    def __init__(self, path):
        self.file = open(path, "w")
        self.stdout = sys.stdout

    def write(self, text):
        self.stdout.write(text)
        self.file.write(text)

    def flush(self):
        self.stdout.flush()
        self.file.flush()   # flush as we go, so a crash still leaves a log

    def close(self):
        self.file.close()


def generate_with_retry(patient_data, attempts=3, delay=5):
    """
    Retry on transient proxy errors. The Colby proxy runs on shared campus
    GPUs, which queue under load — a timeout usually succeeds on retry.
    """
    for attempt in range(1, attempts + 1):
        try:
            return generate_brief(patient_data)
        except Exception as e:
            if attempt == attempts:
                raise
            print(f"\n    attempt {attempt} failed ({e}); retrying in {delay}s",
                  end=" ", flush=True)
            time.sleep(delay)


def main():
    count = 10
    run_all = False

    if "--all" in sys.argv:
        run_all = True
    elif "--count" in sys.argv:
        idx = sys.argv.index("--count")
        count = int(sys.argv[idx + 1])

    conn = sqlite3.connect("carethread.db")
    os.makedirs("briefs", exist_ok=True)

    if run_all:
        patients = conn.execute("""
            SELECT patient_id, given_name, family_name
            FROM patients
            WHERE deceased_date = '' OR deceased_date IS NULL
        """).fetchall()
    else:
        patients = conn.execute("""
            SELECT p.patient_id, p.given_name, p.family_name
            FROM patients p
            LEFT JOIN care_gaps g ON p.patient_id = g.patient_id
            WHERE p.deceased_date = '' OR p.deceased_date IS NULL
            GROUP BY p.patient_id
            ORDER BY COUNT(DISTINCT g.id) DESC
            LIMIT ?
        """, (count,)).fetchall()

    print(f"Running pipeline on {len(patients)} patients\n")

    results = {"PASS": 0, "WARN": 0, "FAIL": 0, "ERROR": 0}
    total_input_tokens = 0
    total_output_tokens = 0

    for i, (pid, first, last) in enumerate(patients):
        name = f"{first} {last}"
        print(f"[{i+1}/{len(patients)}] {name}...", end=" ", flush=True)

        try:
            # Assemble
            data = assemble_patient_data(conn, pid)
            if not data:
                print("SKIP (no data)")
                continue

            # Generate (with retry — shared campus GPUs can queue)
            brief = generate_with_retry(data)

            # Save
            with open(f"briefs/{pid}.json", "w") as f:
                json.dump(brief, f, indent=2, default=str)

            if brief.get("parse_error"):
                if brief["_meta"].get("truncated"):
                    print("ERROR (truncated at max_tokens — raise MAX_TOKENS)")
                else:
                    print("ERROR (JSON parse)")
                results["ERROR"] += 1
                continue

            # Track tokens (the proxy may return nulls — treat as 0)
            meta = brief.get("_meta", {})
            total_input_tokens += meta.get("input_tokens") or 0
            total_output_tokens += meta.get("output_tokens") or 0

            # Verify
            report = verify_brief(brief, data)
            results[report["verdict"]] += 1

            icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}[report["verdict"]]
            gaps = len(brief.get("care_gaps", []))
            alerts = len(brief.get("medication_alerts", []))
            print(f"{icon} {report['verdict']} — {gaps} gaps, {alerts} alerts")

            if report["issues"]:
                for issue in report["issues"]:
                    print(f"    [{issue['severity']}] {issue['detail'][:80]}")

            # Delay between requests. 2s is a safe starting point for the
            # shared proxy; tune down if throughput is fine.
            time.sleep(2)

        except Exception as e:
            print(f"ERROR: {e}")
            results["ERROR"] += 1

    # Final summary
    total = sum(results.values())
    print(f"\n{'='*50}")
    print(f"PIPELINE COMPLETE")
    print(f"{'='*50}")
    print(f"Patients processed: {total}")
    print(f"  PASS: {results['PASS']}  |  WARN: {results['WARN']}  |  "
          f"FAIL: {results['FAIL']}  |  ERROR: {results['ERROR']}")
    print(f"Total tokens: {total_input_tokens:,} in + "
          f"{total_output_tokens:,} out = "
          f"{total_input_tokens + total_output_tokens:,} total")

    # The Colby proxy is institutional infrastructure — no per-token billing.
    # Token counts are tracked for prompt-size tuning, not cost.
    from llm_client import BACKEND
    print(f"(backend: {BACKEND} — no cost incurred)")

    conn.close()


if __name__ == "__main__":
    os.makedirs("runs", exist_ok=True)
    log_path = f"runs/run_{datetime.now():%Y%m%d_%H%M%S}.txt"

    tee = Tee(log_path)
    sys.stdout = tee
    try:
        main()
    finally:
        sys.stdout = tee.stdout      # restore before printing to terminal
        tee.close()
        shutil.copy(log_path, "runs/latest.txt")
        print(f"\nOutput saved to {log_path} (also runs/latest.txt)")