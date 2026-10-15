# CareThread — fixes to apply (October 2026)

Instructions for Claude Code. Apply in order. Each fix says how to confirm it worked.

---

## Background: why these fixes exist

CareThread has four layers:

1. A SQLite database of synthetic patients (conditions, medications, labs, encounters).
2. Rule scripts (`care_gaps.py`, `drug_interactions.py`) that detect care gaps and drug interactions deterministically.
3. An LLM (`qwen-3.8-27b` via the Colby LiteLLM proxy) that writes those findings up as a readable pre-visit brief.
4. `verify_brief.py`, which compares the brief against the database and flags anything the model asserted that the data does not support.

Layer 4 is what makes the project defensible, so a false alarm there is expensive. It makes correct output look fabricated, and it corrupts the pass-rate numbers going into the write-up.

**Every verification failure investigated so far has been a false alarm caused by string matching, not by the model fabricating anything.** Two found so far:

- Check 4 (drug interactions) tested substring containment in ONE direction only. The database stores `amlodipine`, the brief writes `amlodipine 2.5 MG Oral Tablet`, and `"amlodipine 2.5 MG Oral Tablet" in "amlodipine"` is False. Real interactions were reported as hallucinations. ALREADY FIXED.
- Check 2 (medications) has the same class of bug, described in Fix 2. NOT YET FIXED.

The aim is a verification layer that is correct and still strict. A checker that stops flagging things is worse than one that over-flags, so every change below is paired with a test proving fabrications are still caught.

---

## Fix 1: Create `test_verify.py` — do this FIRST

This file does not exist yet. It must exist before Fix 2, because it is what proves Fix 2 does not loosen verification too far.

It feeds `verify_brief.py` inputs whose correct verdict is known in advance (real interactions it must accept, fabricated ones it must reject) across all four checks, and exits 1 if any case behaves wrongly.

Create it exactly as below, at the project root next to `verify_brief.py`:

```python
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
```

Confirm:

```bash
python test_verify.py
```

Expect `12/12 cases correct` and exit code 0. If any case says `<-- WRONG`, stop and report before continuing — that means `verify_brief.py` already behaves differently from what the rest of this document assumes.

---

## Fix 2: Medication check false positive (`verify_brief.py`, check 2)

### The bug

Synthea stores combination products in pack notation. A real example from the database:

```
{5 (dienogest 2 MG / estradiol valerate 2 MG Oral Tablet) / 17 (dienogest 3 MG / estradiol
valerate 2 MG Oral Tablet) / 2 (estradiol valerate 1 MG Oral Tablet) / 2 (estradiol valerate
3 MG Oral Tablet) / 2 (inert ingredients 1 MG Oral Tablet) } Pack [Natazia 28 Day]
```

The model wrote `Natazia 28 Day (dienogest/estradiol valerate)`, which is more readable and clinically equivalent. Neither string contains the other, so check 2 flagged a real, active prescription as `hallucinated_medication` at severity high, failing the whole brief.

### The fix

Keep exact containment as the primary test. Add a fallback comparing the drug names inside each string, ignoring dose units, forms, routes, salts and esters. Also reject entries with no name-like token at all, so a string like `Oral Tablet` cannot pass by being a substring of a real prescription.

Add `import re` to the imports if it isn't there, then add this above `def verify_brief`:

```python
# Dose units, forms, routes, salts and esters — everything in a prescription
# string that is not the name of a drug.
_NON_DRUG_WORDS = {
    "tablet", "tablets", "capsule", "capsules", "oral", "inhalation",
    "solution", "injection", "topical", "extended", "release", "metered",
    "inert", "ingredients", "actuat", "sodium", "hydrochloride", "valerate",
    "propionate", "succinate", "besylate", "tartrate", "sulfate", "phosphate",
    "daily", "pack", "prefilled", "syringe", "auto", "injector",
}


def _drug_words(text):
    """Alphabetic tokens of 5+ characters that could be a drug name."""
    return {w for w in re.findall(r"[a-z]{5,}", str(text).lower())
            if w not in _NON_DRUG_WORDS}
```

Then replace the whole of check 2 with:

```python
    # Check 2: Medications mentioned in brief
    #
    # Exact containment first. It fails on pack notation, where Synthea stores
    # a contraceptive as "{5 (dienogest 2 MG / estradiol valerate 2 MG Oral
    # Tablet) / 17 (...)} Pack [Natazia 28 Day]" and the model sensibly writes
    # "Natazia 28 Day (dienogest/estradiol valerate)". Neither string contains
    # the other, so fall back to comparing the drug names inside each.
    source_med_words = [_drug_words(m["medication"])
                        for m in patient_data["medications"]]

    for med in brief.get("current_medications", []):
        checks += 1
        med_name = med.get("medication", "").lower()
        brief_words = _drug_words(med_name)

        if not brief_words:
            # Nothing name-like in it ("Oral Tablet") — can't be a medication
            matched = False
        else:
            matched = any(med_name in sm or sm in med_name
                          for sm in source_meds)
            if not matched:
                matched = any(brief_words & sw for sw in source_med_words)
        if not matched:
            issues.append({
                "type": "hallucinated_medication",
                "severity": "high",
                "detail": f"Brief mentions '{med.get('medication')}' but not "
                         f"found in active medication list.",
            })
```

### Known limitation, accept it

Stripping salts and esters means `estradiol valerate` and plain `estradiol` now match each other. That is correct for a hallucination check and wrong for dosing logic. Do not use `_drug_words` for anything other than verification.

### Confirm

```bash
python test_verify.py        # must still be 12/12, exit 0
python verify_brief.py       # the Natazia patient should flip to PASS
```

Report the PASS/WARN/FAIL counts before and after. If the FAIL count drops to zero, say so plainly — that changes which patients are available for the demo video.

---

## Fix 3: Raise the output token limit (`llm_client.py`)

`MAX_TOKENS` is currently 3000. The highest observed usage is 2209 tokens, for a patient with 10 medication alerts. Only 40 of ~380 patients have been generated, and they were selected by care gap count rather than medication count, so the patient with the most interactions is probably not in that sample.

Change `MAX_TOKENS` to 4000.

First report what the real ceiling looks like:

```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('carethread.db')
print('top 5 patients by interaction count:')
for n, pid in conn.execute('SELECT COUNT(*), patient_id FROM drug_interactions GROUP BY patient_id ORDER BY COUNT(*) DESC LIMIT 5'):
    print(' ', n, 'interactions -', pid)
print()
print('top 5 patients by care gap count:')
for n, pid in conn.execute('SELECT COUNT(*), patient_id FROM care_gaps GROUP BY patient_id ORDER BY COUNT(*) DESC LIMIT 5'):
    print(' ', n, 'gaps -', pid)
"
```

If any patient has substantially more than 10 interactions, say so. 4000 may not be enough either, and it is cheaper to find that out now than during an overnight run.

---

## Fix 4: Untraced claims in the UI — INVESTIGATE FIRST, DO NOT CODE YET

15 claims across the generated briefs display an `unverified` badge in the web interface. `provenance.py` traces priority actions to care gaps, drug interactions, conditions, labs and medications, but has no tracer for two sources that do exist in the assembled data:

- `data["sdoh"]`, which is where actions like "Address food insecurity" come from
- `data["recent_encounters"]`, which is where "Review recent symptom encounter" comes from

A third group is genuinely untraceable: care-gap actions worded differently from the gap name, for example "Order urine microalbumin test" against a differently-named gap. Those should keep the badge. Loosening the matching threshold until paraphrases match risks attributing a claim to the wrong source row, which is worse on a clinical screen than an honest "unverified".

**Task: classify all 15, report the counts in each group, then stop.** Do not implement tracers until the counts are known. If most are the paraphrase kind, this is not worth doing before the deadline.

---

## Fix 5: Minor-severity drug interactions — DECISION NEEDED, DO NOT CHANGE YET

The database holds 406 interactions: 76 major, 252 moderate, 78 minor. One minor pair, fluticasone and salbutamol/albuterol, affects 17 patients and is a routine, intentional asthma combination. Surfacing it trains a clinician to skim past the alerts section.

Report how many patients would lose all their alerts if minor interactions were hidden, and how many alerts in the existing 40 briefs are minor. Change nothing yet — this is Anika's call and it affects how the demo looks.

If she says yes, the filter belongs in `assemble_patient.get_drug_interactions()`, not in `drug_interactions.py`, so the stored data stays complete and the prompt and the verifier keep seeing the same set.

---

## After the fixes

```bash
python test_verify.py                      # must pass
python run_pipeline.py --count 40 --force  # regenerate with the new token limit
```

Then report: PASS/WARN/FAIL counts, elapsed time, highest token usage observed, and any brief whose `_meta.finish_reason` is not `"stop"`.

---

## Constraints

- Do not modify anything in `briefs/` or `runs/` by hand. Those are generated output.
- Do not change `drug_interactions.py`, `care_gaps.py`, or `parse_fhir.py`. They were audited and found correct.
- Do not loosen any verification check beyond what Fix 2 specifies. If `test_verify.py` drops below 12/12 at any point, stop and report.
- No refactoring, renaming, or style changes. Only the edits described here.
- Report before/after numbers for every fix, not just "done".
