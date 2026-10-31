"""
generate_brief.py — Generates a pre-visit clinical brief for one patient
using whichever LLM backend is configured in llm_client.py.

Default backend is the Colby DAI LiteLLM proxy (qwen-3.8-27b, locally hosted).

Usage:
    # As a module:
    from generate_brief import generate_brief
    brief = generate_brief(patient_data)

    # Standalone test:
    python generate_brief.py
"""

import json
import os
import re
import sqlite3
from datetime import date

from assemble_patient import (assemble_patient_data, format_indicator,
                              valid_zip, SDOH_INDICATORS)
from llm_client import call_llm, extract_json
from config import DB_PATH


SYSTEM_PROMPT = """You are a clinical decision support system generating
pre-visit briefs for FQHC (Federally Qualified Health Center) clinicians.

Your output must be a JSON object with exactly these keys:

{
  "patient_summary": "One sentence: name, age, gender, key conditions.",
  "key_conditions": [
    {"condition": "...", "since": "...", "relevance": "..."}
  ],
  "current_medications": [
    {"medication": "...", "note": "..."}
  ],
  "medication_alerts": [
    {
      "type": "drug_interaction",
      "severity": "major|moderate|minor",
      "drugs": ["drug_a", "drug_b"],
      "recommendation": "..."
    }
  ],
  "care_gaps": [
    {
      "gap": "...",
      "severity": "high|medium|low",
      "action": "...",
      "source": "guideline name"
    }
  ],
  "recent_activity": "Brief paragraph: recent visits, labs, trends.",
  "social_history": ["Patient-level social circumstances, from PATIENT SOCIAL HISTORY only."],
  "community_context": ["Neighborhood-level indicators, from COMMUNITY SDOH INDICATORS only."],
  "priority_actions": [
    "Ranked list of what to address in today's visit, most urgent first."
  ]
}

Rules:
1. Every claim must be based ONLY on the data provided. Do not infer conditions,
   medications, or lab values that are not in the input.
2. If a field has no relevant data, use an empty list [] or "No data available."
3. For care gaps, repeat the gap exactly as detected — do not invent new ones.
4. medication_alerts must contain exactly the interactions listed under
   DETECTED DRUG INTERACTIONS — no more, no fewer. If that section says NONE
   DETECTED, output an empty array. You may know of other interactions among
   these drugs. Do not include them. The detection layer is the only source.
5. Keep the entire brief scannable in under 2 minutes.
6. Respond with ONLY the JSON object. No markdown, no explanation, no preamble.
7. Do not wrap the JSON in markdown code fences. Do not write "Here is the
   brief" or any other introduction.
8. Do not emit reasoning, analysis, or <think> blocks. Begin your response with
   the character { and end it with the character }.
9. Do not characterize a lab value as high, low, elevated, reduced, normal, or
   abnormal. The input carries no reference ranges, so you have no basis for
   that judgment. Report the value, unit, and date as given and let the
   clinician interpret. "Creatinine 2.76 mg/dL (2025-11-02)" is correct;
   "elevated creatinine" is not.
10. Social history and community context are different kinds of data. Keep
   them in their own fields and never mix them.
   - social_history: only items listed under PATIENT SOCIAL HISTORY. These are
     facts recorded about this patient. Do not list them in key_conditions.
   - community_context: only the ZIP-level figures under COMMUNITY SDOH
     INDICATORS. They describe the patient's neighborhood, not the patient.
     Phrase every entry as neighborhood-level, e.g. "In the patient's ZIP
     code, 9.84% of residents live below the federal poverty line." Never
     write or imply that a community figure is true of the patient ("the
     patient is low-income", "the patient lacks insurance" are wrong).
     Copy every number exactly as given. If that section says UNAVAILABLE,
     community_context must be an empty array.
11. Some interactions under DETECTED DRUG INTERACTIONS are marked OLD
   PRESCRIPTION: one of the drugs was last prescribed more than 5 years ago
   and the patient may no longer take it. Still include them in
   medication_alerts, but do not present them as a definite current risk.
   Their recommendation must read "Confirm the patient still takes <drug>
   before acting on this interaction.", naming the marked drug (or drugs),
   and any priority action about them must ask to confirm the drug first.
   Interactions without the mark are current: describe them as before.
   Use the "before acting on this interaction" wording only for a drug named
   in an OLD PRESCRIPTION interaction. Any other medication marked "confirm
   the patient still takes it" under ACTIVE MEDICATIONS gets just "Confirm
   the patient still takes <drug>." Say nothing about interactions for it.
12. Never state a count of days: no "overdue by N days", "N days ago",
   "N days since". A day count is wrong by the time the brief is read. Give
   the date and the interval instead: "BP last done 2026-01-27; due every
   6 months", "No HbA1c on record; due every 6 months".
"""

# 'Last BMI was 401 days ago' → 'Last BMI was on 2025-09-23' (rule 12). The gap
# rules phrase descriptions as counts as of the day they ran; the model is told
# to repeat gaps exactly, so it would copy the count.
_DAYS_AGO = re.compile(r"\bwas \d+ days ago\b")
_INTERVALS = {"Due every 180 days.": "Due every 6 months."}


def dated_description(gap):
    """A care gap's description with day counts replaced by its dates."""
    text = gap["description"]
    if gap.get("last_date"):
        text = _DAYS_AGO.sub(f"was on {gap['last_date']}", text)
    for count, interval in _INTERVALS.items():
        text = text.replace(count, interval)
    return text


def old_prescription_note(ix):
    """' — OLD PRESCRIPTION: ...' for a major interaction with a drug last
    prescribed over 5 years ago and never since (rule 11); '' otherwise."""
    if ix["severity"] != "major" or ix["current"]:
        return ""
    old = [f"{ix[f'drug_{s}_generic']} last prescribed {ix[f'drug_{s}_confirm']}"
           for s in "ab" if ix[f"drug_{s}_confirm"]]
    return f" — OLD PRESCRIPTION: {'; '.join(old)}"


def build_user_prompt(patient_data):
    """Convert the assembled patient data into the user message for the LLM."""
    d = patient_data
    demo = d["demographics"]

    sections = []

    # Demographics
    sections.append(f"PATIENT: {demo['name']}, {demo['age']}yo {demo['gender']}, "
                   f"DOB {demo['birth_date']}, {demo['city']}, {demo['state']} "
                   f"{demo['zip_code']}")

    # Conditions
    if d["conditions"]:
        cond_lines = [f"  - {c['condition']} (since {c['since']}, "
                     f"SNOMED {c['snomed_code']})"
                     for c in d["conditions"]]
        sections.append("ACTIVE CONDITIONS:\n" + "\n".join(cond_lines))

    # Medications
    if d["medications"]:
        med_lines = [f"  - {m['medication']} (prescribed {m['prescribed']}, "
                    f"RxNorm {m['rxnorm_code']}"
                    + ("; started over 5 years ago and never re-prescribed — "
                       "confirm the patient still takes it"
                       if m["confirm_with_patient"] else "") + ")"
                    for m in d["medications"]]
        sections.append("ACTIVE MEDICATIONS:\n" + "\n".join(med_lines))

    # Recent encounters
    if d["recent_encounters"]:
        enc_lines = [f"  - {e['date']}: {e['type']} ({e['class']})"
                    for e in d["recent_encounters"][:5]]
        sections.append("RECENT ENCOUNTERS (last 5):\n" + "\n".join(enc_lines))

    # Recent labs
    if d["recent_labs"]:
        lab_lines = [f"  - {l['date']}: {l['test']} = {l['value']} {l['unit']} "
                    f"(LOINC {l['loinc_code']})"
                    for l in d["recent_labs"][:10]]
        sections.append("RECENT LABS (last 10):\n" + "\n".join(lab_lines))

    # Immunizations
    if d["immunizations"]:
        imm_lines = [f"  - {i['date']}: {i['vaccine']}"
                    for i in d["immunizations"][:5]]
        sections.append("RECENT IMMUNIZATIONS:\n" + "\n".join(imm_lines))

    # Care gaps (already detected by care_gaps.py). Always emit the section,
    # even when empty — an absent section reads as an omission the model
    # should fill, which is how fabricated alerts get in.
    if d["care_gaps"]:
        gap_lines = [f"  - [{g['severity'].upper()}] {g['gap']}: {dated_description(g)} "
                    f"(Guideline: {g['guideline']})"
                    for g in d["care_gaps"]]
        sections.append(f"DETECTED CARE GAPS ({len(d['care_gaps'])} total — "
                       f"your output must contain all {len(d['care_gaps'])}):\n"
                       + "\n".join(gap_lines))
    else:
        sections.append("DETECTED CARE GAPS: NONE. "
                       "The care_gaps array in your output must be empty.")

    # Drug interactions (already detected by drug_interactions.py)
    if d["drug_interactions"]:
        ix_lines = [f"  - [{ix['severity'].upper()}] {ix['drug_a_generic']} ↔ "
                   f"{ix['drug_b_generic']} "
                   f"(prescribed as: {ix['drug_a']} / {ix['drug_b']})"
                   + old_prescription_note(ix)
                   for ix in d["drug_interactions"]]
        sections.append(f"DETECTED DRUG INTERACTIONS ({len(d['drug_interactions'])} "
                       f"total — report exactly these and no others):\n"
                       + "\n".join(ix_lines))
    else:
        sections.append("DETECTED DRUG INTERACTIONS: NONE DETECTED. "
                       "The medication_alerts array in your output must be "
                       "empty. Do not add interactions from your own knowledge.")

    # Patient-level social findings, from the record
    if d["social_history"]:
        soc_lines = [f"  - {s['finding']} (recorded {s['since']}, "
                     f"SNOMED {s['snomed_code']})"
                     for s in d["social_history"]]
        sections.append("PATIENT SOCIAL HISTORY (from record):\n"
                        + "\n".join(soc_lines))
    else:
        sections.append("PATIENT SOCIAL HISTORY (from record): NONE RECORDED. "
                        "The social_history array in your output must be empty.")

    # Neighborhood-level SDOH, joined by ZIP. Always emit the section so an
    # absence is explicit rather than a gap the model fills.
    sdoh = {k: v for k, v in (d["sdoh"] or {}).items()
            if k in SDOH_INDICATORS and v is not None and str(v).strip()}
    if sdoh:
        sdoh_lines = [f"  - {format_indicator(k, sdoh[k])}"
                      for k in SDOH_INDICATORS if k in sdoh]
        sections.append(f"COMMUNITY SDOH INDICATORS (ZIP {demo['zip_code']}, "
                        f"AHRQ 2023 — describes the neighborhood, NOT this "
                        f"patient):\n" + "\n".join(sdoh_lines))
    else:
        reason = ("no valid ZIP on record" if not valid_zip(demo["zip_code"])
                  else "ZIP not in AHRQ data")
        sections.append(f"COMMUNITY SDOH INDICATORS: UNAVAILABLE ({reason}). "
                        f"The community_context array in your output must be empty.")

    return "\n\n".join(sections)


def generate_brief(patient_data):
    """Send patient data to the configured LLM and get back a structured brief."""
    user_prompt = build_user_prompt(patient_data)

    raw_text, meta = call_llm(SYSTEM_PROMPT, user_prompt)

    # Parse the JSON response. Unlike Gemini's response_mime_type, the Qwen
    # models behind the proxy give no format guarantee — extract_json handles
    # fences, preamble, and trailing commentary.
    try:
        brief = extract_json(raw_text)
    except (json.JSONDecodeError, ValueError):
        brief = {"raw_text": raw_text, "parse_error": True}

    # Attach metadata — records which backend produced this brief, so a
    # briefs/ folder holding mixed output stays auditable.
    brief["_meta"] = {
        "patient_id": patient_data["patient_id"],
        "patient_name": patient_data["demographics"]["name"],
        # What the verifier dates the brief by (day-count check)
        "generated_on": date.today().isoformat(),
        **meta,
    }

    return brief


# ── Standalone test ──

if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)

    # Pick the top patient by gap count
    top = conn.execute("""
        SELECT p.patient_id, p.given_name, p.family_name,
               COUNT(DISTINCT g.id) as gap_count
        FROM patients p
        LEFT JOIN care_gaps g ON p.patient_id = g.patient_id
        WHERE p.deceased_date = '' OR p.deceased_date IS NULL
        GROUP BY p.patient_id
        ORDER BY gap_count DESC LIMIT 1
    """).fetchone()

    if not top:
        print("No patients found.")
        conn.close()
        exit()

    print(f"Generating brief for: {top[1]} {top[2]} ({top[3]} gaps)")
    from llm_client import BACKEND
    print(f"Calling the LLM ({BACKEND})...\n")

    data = assemble_patient_data(conn, top[0])
    brief = generate_brief(data)

    # Save to file
    os.makedirs("briefs", exist_ok=True)
    outpath = f"briefs/{top[0]}.json"
    with open(outpath, "w") as f:
        json.dump(brief, f, indent=2, default=str)

    # Print it
    if brief.get("parse_error"):
        if brief["_meta"].get("truncated"):
            print("WARNING: Output was cut off at max_tokens before the JSON closed.")
        else:
            print("WARNING: Could not parse LLM output as JSON.")
        print("Raw output:")
        print(brief["raw_text"])
    else:
        print("=== PRE-VISIT BRIEF ===\n")
        print(f"Summary: {brief.get('patient_summary', 'N/A')}\n")

        if brief.get("care_gaps"):
            print("Care Gaps:")
            for g in brief["care_gaps"]:
                print(f"  [{g.get('severity', '?').upper()}] {g.get('gap', '?')}")
                print(f"    Action: {g.get('action', '?')}")

        if brief.get("medication_alerts"):
            print("\nMedication Alerts:")
            for a in brief["medication_alerts"]:
                print(f"  [{a.get('severity', '?').upper()}] "
                      f"{' ↔ '.join(a.get('drugs', []))}")
                print(f"    {a.get('recommendation', '?')}")

        if brief.get("priority_actions"):
            print("\nPriority Actions:")
            for i, action in enumerate(brief["priority_actions"], 1):
                print(f"  {i}. {action}")

        meta = brief.get("_meta", {})
        print(f"\n--- {meta.get('model', '?')} | "
              f"tokens: {meta.get('input_tokens') or '?'} in, "
              f"{meta.get('output_tokens') or '?'} out ---")

    print(f"\nBrief saved to {outpath}")
    conn.close()