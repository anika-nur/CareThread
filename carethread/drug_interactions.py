"""
drug_interactions.py — Checks for drug-drug interactions using the DDInter database.

DDInter is an open-access database of 302K+ drug-drug interactions.
Download CSVs from: https://ddinter.scbdd.com/download/
Place all 8 CSV files in a ddinter_data/ folder.

Usage:
    python drug_interactions.py
"""

import sqlite3
import pandas as pd
import os
import re


# DDInter names drugs by INN; Synthea uses the US/USAN name for some of them,
# so these never match on spelling alone. Aspirin matters most — it interacts
# with warfarin, clopidogrel and the NSAIDs.
SYNONYMS = {
    "aspirin": "acetylsalicylic acid",
    "albuterol": "salbutamol",
    "norethindrone": "norethisterone",
    "vitamin b12": "cyanocobalamin",
    "clavulanate": "clavulanic acid",
}

# DDInter labels an interaction 'Unknown' when it records the pair but has not
# graded its severity. Those outnumber the graded findings roughly 2:1 and bury
# the actionable ones, so they are excluded from the results and only counted.
EXCLUDED_SEVERITIES = {"unknown"}

# Hydration states that distinguish a DDInter entry from the name Synthea uses
# ('ferrous sulfate anhydrous' vs 'ferrous sulfate'), registered as aliases.
FORM_QUALIFIERS = {"anhydrous", "monohydrate", "dihydrate", "trihydrate",
                   "hydrate", "hydrated"}

# Dosage, route and packaging tokens — noise around the ingredient name.
NON_DRUG_TOKENS = {
    "hr", "ml", "mg", "mcg", "unt", "unit", "units", "meq", "actuat",
    "oral", "tablet", "tablets", "capsule", "capsules", "injection",
    "solution", "suspension", "inhaler", "inhalation", "patch", "topical",
    "cream", "ointment", "gel", "spray", "drop", "drops", "powder",
    "granule", "suppository", "enema", "extended", "delayed", "release",
    "metered", "dose", "prefilled", "syringe", "auto-injector", "pack",
    "day", "days", "inert", "ingredients", "abuse-deterrent", "transdermal",
    "system", "mucosal", "ophthalmic", "otic", "nasal", "sublingual",
    "chewable", "film", "coated", "irrigant", "lotion", "shampoo", "foam",
    "implant", "ring", "insert", "kit", "vaginal", "rectal", "intravenous",
    "subcutaneous", "intramuscular", "cartridge", "pen", "vial", "ampule",
    "sterile", "preservative", "free", "dry",
}


def load_ddinter(ddinter_dir="ddinter_data"):
    """Load all DDInter CSV files and build the interaction lookup + name index."""
    # Sorted so that a pair appearing in more than one file always resolves to
    # the same row — os.listdir() order is arbitrary.
    files = sorted(f for f in os.listdir(ddinter_dir) if f.endswith(".csv"))
    print(f"Loading {len(files)} DDInter CSV files...")

    frames = [pd.read_csv(os.path.join(ddinter_dir, f)) for f in files]
    combined = pd.concat(frames, ignore_index=True)

    combined["Drug_A"] = combined["Drug_A"].str.strip().str.lower()
    combined["Drug_B"] = combined["Drug_B"].str.strip().str.lower()
    combined["Level"] = combined["Level"].str.strip()
    combined = combined.drop_duplicates(subset=["Drug_A", "Drug_B"])
    print(f"Loaded {len(combined)} unique drug-drug interactions")

    # Lookup: (drug_a, drug_b) → severity level, stored both directions
    lookup = {}
    for a, b, level in zip(combined["Drug_A"], combined["Drug_B"],
                          combined["Level"]):
        lookup[(a, b)] = level
        lookup[(b, a)] = level

    all_drug_names = set(combined["Drug_A"]) | set(combined["Drug_B"])
    print(f"DDInter covers {len(all_drug_names)} unique drug names")
    return lookup, build_drug_index(all_drug_names)


def build_drug_index(all_drug_names):
    """Build a deterministic exact-match index of DDInter drug names.

    DDInter qualifies some names by route ('fluticasone (nasal)'), so the
    parenthetical-stripped form is registered as an alias. Unqualified exact
    names always win, and aliases are inserted in sorted order so the index
    never depends on set iteration order.
    """
    index = {}
    for name in sorted(all_drug_names):
        base = re.sub(r"\s*\([^)]*\)", "", name).strip()
        if base:
            index.setdefault(base, name)
            # 'ferrous sulfate anhydrous' also answers to 'ferrous sulfate'
            tokens = base.split()
            while len(tokens) > 1 and tokens[-1] in FORM_QUALIFIERS:
                tokens.pop()
                index.setdefault(" ".join(tokens), name)

    # Exact names override any alias that collided with them.
    for name in sorted(all_drug_names):
        index[name] = name

    for alias, canonical in SYNONYMS.items():
        if canonical in all_drug_names:
            index[alias] = canonical

    return index


def extract_generic_name(rxnorm_display):
    """
    Split a Synthea RxNorm display into candidate ingredient token lists.

    Synthea displays wrap the ingredient in brand names, NDA registration
    codes, pack notation and metered-dose prefixes. Stopping at the first
    number (as an earlier version did) drops the ingredient entirely whenever
    a count leads the string:
        '120 ACTUAT fluticasone propionate 0.11 MG/ACTUAT ... [Flovent]'
        'NDA020800 0.3 ML Epinephrine 1 MG/ML Auto-Injector'
        '{28 (norethindrone 0.35 MG Oral Tablet) } Pack [Jolivette 28 Day]'
        'Abuse-Deterrent 12 HR Oxycodone Hydrochloride 10 MG ... [Oxycontin]'

    So noise is stripped rather than used as a stopping point. Returns one
    token list per ingredient, e.g. [['metformin', 'hydrochloride']].
    """
    if not rxnorm_display:
        return []

    name = rxnorm_display
    name = re.sub(r"\[[^\]]*\]", " ", name)                       # brand names
    name = re.sub(r"\b(?:NDA|ANDA)\d+\b", " ", name, flags=re.I)  # registrations
    name = re.sub(r"[{}()]", " ", name)                           # pack notation
    name = name.replace("/", " / ")                               # MG/ML, combos

    ingredients = []
    for part in name.split(" / "):
        tokens = []
        for raw in part.split():
            token = raw.strip(",;.").lower()
            if not token or token in NON_DRUG_TOKENS:
                continue
            if re.match(r"^\d", token):      # dosage numbers
                continue
            if len(token) <= 2:              # junk fragments
                continue
            tokens.append(token)
        if tokens:
            ingredients.append(tokens)

    return ingredients


def match_drug_to_ddinter(tokens, drug_index):
    """
    Resolve one ingredient's tokens to a DDInter name, longest phrase first.

    Longest-first means ['insulin', 'glargine'] resolves to 'insulin glargine'
    rather than bare 'insulin', while ['metformin', 'hydrochloride'] falls back
    to 'metformin' once the salt form fails to match.

    Matching is exact against the index. The earlier bidirectional startswith
    test matched 'ace' to 'acenocoumarol' and 'sodium' to 'sodium ferric
    gluconate complex', and picked arbitrarily when several names shared a
    prefix — set iteration order varies per process, so results were not
    reproducible between runs.
    """
    n = len(tokens)
    for length in range(n, 0, -1):
        for start in range(0, n - length + 1):
            candidate = " ".join(tokens[start:start + length])
            if candidate in drug_index:
                return drug_index[candidate]
    return None


def resolve_ingredients(display, drug_index):
    """Return the canonical DDInter names for every ingredient in a display."""
    resolved = []
    for tokens in extract_generic_name(display):
        matched = match_drug_to_ddinter(tokens, drug_index)
        if matched and matched not in resolved:
            resolved.append(matched)
    return resolved


def check_patient_interactions(conn, patient_id, ddinter_lookup, drug_index):
    """Check all active medication pairs for a patient against DDInter."""
    meds = conn.execute(
        "SELECT DISTINCT display FROM medications "
        "WHERE patient_id=? AND status='active'",
        (patient_id,)
    ).fetchall()

    # Resolve each display to its ingredients, keeping the original display
    med_mappings = []  # list of (original_display, matched_ddinter_name)
    for (display,) in meds:
        for matched in resolve_ingredients(display, drug_index):
            med_mappings.append((display, matched))

    # Check all pairs of matched drugs
    interactions_found = []
    seen_pairs = set()

    for i, (display_a, ddinter_a) in enumerate(med_mappings):
        for j, (display_b, ddinter_b) in enumerate(med_mappings):
            if i >= j:
                continue
            if ddinter_a == ddinter_b:
                continue

            pair_key = tuple(sorted([ddinter_a, ddinter_b]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            level = ddinter_lookup.get((ddinter_a, ddinter_b))
            if level:
                # Store the pair in a canonical order (by matched name) so the
                # same interaction aggregates as one group across patients
                # instead of splitting by whichever drug was seen first.
                (first_display, first_matched), (second_display, second_matched) = \
                    sorted([(display_a, ddinter_a), (display_b, ddinter_b)],
                           key=lambda m: m[1])

                interactions_found.append({
                    "patient_id": patient_id,
                    "drug_a": first_display,
                    "drug_b": second_display,
                    "drug_a_matched": first_matched,
                    "drug_b_matched": second_matched,
                    "severity": level.lower(),
                })

    return interactions_found


def main():
    # Load DDInter
    ddinter_lookup, drug_index = load_ddinter()

    # Connect to patient database
    conn = sqlite3.connect("carethread.db")

    patient_ids = [r[0] for r in conn.execute(
        "SELECT patient_id FROM patients "
        "WHERE deceased_date = '' OR deceased_date IS NULL"
    ).fetchall()]
    print(f"\nChecking drug interactions for {len(patient_ids)} living patients...\n")

    # Check how many medications we can match to DDInter
    all_meds = conn.execute(
        "SELECT DISTINCT display FROM medications WHERE status='active'"
    ).fetchall()
    matched_count = 0
    unmatched = []
    for (display,) in all_meds:
        if resolve_ingredients(display, drug_index):
            matched_count += 1
        else:
            unmatched.append(display)

    print(f"Medication matching: {matched_count}/{len(all_meds)} unique "
          f"active medications matched to DDInter")
    if unmatched and len(unmatched) <= 15:
        print(f"Unmatched medications:")
        for u in unmatched:
            print(f"  - {u}")
    elif unmatched:
        print(f"  ({len(unmatched)} unmatched — showing first 10)")
        for u in unmatched[:10]:
            print(f"  - {u}")

    # Run interaction checks
    detected = []
    for pid in patient_ids:
        results = check_patient_interactions(conn, pid, ddinter_lookup, drug_index)
        detected.extend(results)

    all_interactions = [ix for ix in detected
                        if ix["severity"] not in EXCLUDED_SEVERITIES]
    suppressed = len(detected) - len(all_interactions)

    print(f"\nGraded interactions found: {len(all_interactions)}")
    if suppressed:
        print(f"  ({suppressed} ungraded '"
              f"{'/'.join(sorted(EXCLUDED_SEVERITIES))}'-severity pairs excluded)")

    # Summary by severity
    from collections import Counter
    by_severity = Counter(ix["severity"] for ix in all_interactions)
    print("By severity:")
    for sev, count in by_severity.most_common():
        print(f"  {sev}: {count}")

    # Show examples
    majors = [ix for ix in all_interactions if ix["severity"] == "major"]
    if majors:
        print(f"\n--- Major Interactions (showing first 5) ---")
        for ix in majors[:5]:
            row = conn.execute(
                "SELECT given_name, family_name FROM patients WHERE patient_id=?",
                (ix["patient_id"],)
            ).fetchone()
            name = f"{row[0]} {row[1]}" if row else ix["patient_id"]
            print(f"  Patient: {name}")
            print(f"  {ix['drug_a']} ↔ {ix['drug_b']}")
            print(f"  Matched as: {ix['drug_a_matched']} ↔ {ix['drug_b_matched']}")
            print(f"  Severity: {ix['severity'].upper()}")
            print()

    # Save to database
    conn.execute("DROP TABLE IF EXISTS drug_interactions")
    conn.execute("""CREATE TABLE drug_interactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT, drug_a TEXT, drug_b TEXT,
        drug_a_matched TEXT, drug_b_matched TEXT,
        severity TEXT
    )""")

    for ix in all_interactions:
        conn.execute(
            "INSERT INTO drug_interactions "
            "(patient_id, drug_a, drug_b, drug_a_matched, drug_b_matched, severity) "
            "VALUES (?,?,?,?,?,?)",
            (ix["patient_id"], ix["drug_a"], ix["drug_b"],
             ix["drug_a_matched"], ix["drug_b_matched"], ix["severity"])
        )
    conn.commit()
    print(f"\nInteractions saved to drug_interactions table in carethread.db")
    conn.close()


if __name__ == "__main__":
    main()