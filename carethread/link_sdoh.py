"""
link_sdoh.py — Links AHRQ SDOH community-level data to patients by zip code.

Usage:
    python link_sdoh.py clh_2023_zipcode_2_0.xlsx

Requires: pip install openpyxl
"""

import sqlite3
import pandas as pd
import sys


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

    # Select the SDOH columns most relevant to FQHC patient populations
    # These use the ACS_ naming convention from the AHRQ database
    sdoh_columns = {
        "ZIPCODE":                          "zip_code",
        "ACS_PCT_UNINSURED_ZC":             "pct_uninsured",
        "ACS_MEDIAN_HH_INC_ZC":            "median_household_income",
        "ACS_PCT_HH_PUB_ASSIST_ZC":        "pct_public_assistance",
        "ACS_PCT_UNEMPLOY_ZC":             "pct_unemployed",
        "ACS_PCT_LT_HS_ZC":               "pct_less_than_high_school",
        "ACS_PCT_POVERTY_ZC":              "pct_poverty",
        "ACS_PCT_NO_VEH_ZC":              "pct_no_vehicle",
        "ACS_PCT_RENTER_HH_COST_30PCT_ZC": "pct_rent_burdened",
    }

    # Find which of these columns actually exist in the file
    # (column names can vary slightly between years — _ZC suffix may differ)
    found_cols = {}
    for orig_name, friendly_name in sdoh_columns.items():
        if orig_name in sdoh.columns:
            found_cols[orig_name] = friendly_name
        else:
            # Try without the _ZC suffix or with slight variations
            for col in sdoh.columns:
                base = orig_name.replace("_ZC", "")
                if base in col:
                    found_cols[col] = friendly_name
                    break

    if len(found_cols) <= 1:
        print("\nCould not find expected SDOH columns.")
        print("Searching for any columns with relevant keywords...\n")
        for col in sdoh.columns:
            col_upper = col.upper()
            if any(kw in col_upper for kw in [
                "UNINSURED", "POVERTY", "INCOME", "UNEMPLOY",
                "VEHICLE", "FOOD", "ASSIST", "RENTER"
            ]):
                print(f"  Found: {col}")
        print("\nUpdate the sdoh_columns dict in this script with the exact names above.")
        return

    print(f"\nMatched {len(found_cols)} SDOH columns:")
    for orig, friendly in found_cols.items():
        print(f"  {orig} → {friendly}")

    # Extract and rename
    sdoh_slim = sdoh[list(found_cols.keys())].rename(columns=found_cols)
    sdoh_slim = sdoh_slim.drop_duplicates(subset=["zip_code"])

    # Connect to database
    conn = sqlite3.connect("carethread.db")

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