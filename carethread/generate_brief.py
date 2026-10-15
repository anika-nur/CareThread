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
import sqlite3

from assemble_patient import assemble_patient_data
from llm_client import call_llm, extract_json


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
  "sdoh_flags": ["Any relevant social determinant flags."],
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
"""


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
                    f"RxNorm {m['rxnorm_code']})"
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
        gap_lines = [f"  - [{g['severity'].upper()}] {g['gap']}: {g['description']} "
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
                   for ix in d["drug_interactions"]]
        sections.append(f"DETECTED DRUG INTERACTIONS ({len(d['drug_interactions'])} "
                       f"total — report exactly these and no others):\n"
                       + "\n".join(ix_lines))
    else:
        sections.append("DETECTED DRUG INTERACTIONS: NONE DETECTED. "
                       "The medication_alerts array in your output must be "
                       "empty. Do not add interactions from your own knowledge.")

    # SDOH
    if d["sdoh"]:
        sdoh_lines = [f"  - {k}: {v}" for k, v in d["sdoh"].items()
                     if v is not None and str(v).strip()]
        if sdoh_lines:
            sections.append("COMMUNITY SDOH INDICATORS (zip-level):\n"
                          + "\n".join(sdoh_lines))

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
        **meta,
    }

    return brief


# ── Standalone test ──

if __name__ == "__main__":
    conn = sqlite3.connect("carethread.db")

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