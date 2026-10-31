"""
link_sdoh.py — Links AHRQ SDOH community-level data to patients by zip code.

Usage:
    python link_sdoh.py clh_2023_zipcode_2_0.xlsx

Requires: pip install openpyxl
"""

import sqlite3
import pandas as pd
import sys
from config import DB_PATH


def load_labels(sdoh_file):
    """Column name → AHRQ description, from the workbook's Layout sheet."""
    if not sdoh_file.endswith((".xlsx", ".xls")):
        return {}
    xls = pd.ExcelFile(sdoh_file)
    if "Layout" not in xls.sheet_names:
        return {}
    layout = pd.read_excel(xls, sheet_name="Layout")
    return dict(zip(layout["name"], layout["label"]))


def load_sdoh_file(sdoh_file):
    """Load the SDOH file, auto-detecting format and finding the data sheet."""
    if sdoh_file.endswith(".xlsx") or sdoh_file.endswith(".xls"):
        # Excel files may have multiple sheets — find the one with actual data
        xls = pd.ExcelFile(sdoh_file)
        print(f"Excel file has {len(xls.sheet_names)} sheet(s): {xls.sheet_names}")

        # Pick the sheet with the most columns (that's the data, not the codebook)
        best_sheet = None
        best_cols = 0
        for sheet in xls.sheet_names:
            df_peek = pd.read_excel(xls, sheet_name=sheet, nrows=1)
            if len(df_peek.columns) > best_cols:
                best_cols = len(df_peek.columns)
                best_sheet = sheet

        print(f"Using sheet \"{best_sheet}\" ({best_cols} columns)")
        sdoh = pd.read_excel(xls, sheet_name=best_sheet, dtype={"ZIPCODE": str})
    else:
        sdoh = pd.read_csv(sdoh_file, dtype={"ZIPCODE": str}, low_memory=False)

    return sdoh


def main():
    if len(sys.argv) < 2:
        print("Usage: python link_sdoh.py <sdoh_file>")
        sys.exit(1)

    sdoh_file = sys.argv[1]
    print(f"Loading SDOH data from {sdoh_file}...")

    sdoh = load_sdoh_file(sdoh_file)
    print(f"Loaded {len(sdoh)} rows, {len(sdoh.columns)} columns")

    # Check that ZIPCODE column exists
    if "ZIPCODE" not in sdoh.columns:
        print("ERROR: No ZIPCODE column found. Available columns:")
        print(sdoh.columns.tolist()[:20])
        return

    # Select the SDOH columns most relevant to FQHC patient populations.
    # Names are from the 2023 AHRQ ZIP file (checked against its Layout sheet).
    sdoh_columns = {
        "ZIPCODE":                          "zip_code",
        "ACS_PCT_UNINSURED_ZC":             "pct_uninsured",
        "ACS_MEDIAN_HH_INC_ZC":             "median_household_income",
        "ACS_PCT_HH_PUB_ASSIST_ZC":         "pct_public_assistance",
        "ACS_PCT_UNEMPLOY_ZC":              "pct_unemployed",
        "ACS_PCT_LT_HS_ZC":                 "pct_less_than_high_school",
        "ACS_PCT_PERSON_INC_BELOW99_ZC":    "pct_poverty",
        "ACS_PCT_HU_NO_VEH_ZC":             "pct_no_vehicle",
        "ACS_PCT_RENTER_HU_COST_30PCT_ZC":  "pct_rent_burdened",
    }

    # Exact names only. A substring fallback used to live here, and it can
    # quietly pick a race- or age-specific breakdown (ACS_PCT_POV_WHITE_ZC)
    # in place of the overall rate. A renamed column should stop the run.
    missing = [c for c in sdoh_columns if c not in sdoh.columns]
    if missing:
        print("\nERROR: these columns are not in the file:")
        for c in missing:
            print(f"  {c} ({sdoh_columns[c]})")
        print("Look up the current names in the file's Layout sheet and "
              "update sdoh_columns.")
        sys.exit(1)

    labels = load_labels(sdoh_file)
    found_cols = sdoh_columns
    print(f"\nMatched {len(found_cols) - 1} SDOH indicator columns:")
    for orig, friendly in found_cols.items():
        if orig != "ZIPCODE":
            print(f"  {friendly:26} ← {orig:33} {labels.get(orig, '')}")

    # Extract and rename
    sdoh_slim = sdoh[list(found_cols.keys())].rename(columns=found_cols)
    sdoh_slim = sdoh_slim.drop_duplicates(subset=["zip_code"])

    # Connect to database
    conn = sqlite3.connect(DB_PATH)

    # Save SDOH table
    conn.execute("DROP TABLE IF EXISTS sdoh")
    sdoh_slim.to_sql("sdoh", conn, index=False)

    # Check how many patients matched
    matched = conn.execute("""
        SELECT COUNT(DISTINCT p.patient_id)
        FROM patients p
        JOIN sdoh s ON p.zip_code = s.zip_code
    """).fetchone()[0]

    total = conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    print(f"\nMatched {matched}/{total} patients to SDOH data by zip code")

    # Show a sample using whatever columns were saved
    if matched > 0:
        sdoh_cols = [r[1] for r in conn.execute("PRAGMA table_info(sdoh)").fetchall()
                     if r[1] != "zip_code"]
        # Build query dynamically from available columns
        select_cols = ", ".join(f"s.{c}" for c in sdoh_cols[:3])
        sample = conn.execute(f"""
            SELECT p.given_name, p.family_name, p.zip_code, {select_cols}
            FROM patients p
            JOIN sdoh s ON p.zip_code = s.zip_code
            LIMIT 3
        """).fetchall()
        print("\nSample matched patients:")
        col_labels = sdoh_cols[:3]
        for row in sample:
            details = ", ".join(f"{col_labels[i]}={row[3+i]}" for i in range(len(col_labels)))
            print(f"  {row[0]} {row[1]} (ZIP {row[2]}): {details}")

    conn.close()
    print("\nSDOH data linked successfully.")


if __name__ == "__main__":
    main()