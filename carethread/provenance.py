"""
provenance.py — Resolve every element of a generated brief back to the
database rows it came from.

The model never emits source IDs. Matching happens here, deterministically,
against the same tables the prompt was built from. Anything that can't be
traced is marked unverified rather than quietly rendered as fact.

Usage:
    from provenance import annotate_brief
    annotated = annotate_brief(brief, patient_data)
"""

import re

from drug_interactions import SYNONYMS

STOP = {"of", "the", "a", "an", "and", "or", "for", "to", "in", "on", "with",
        "disorder", "finding", "situation", "history",
        "overdue", "due", "order", "schedule", "needed", "recommended"}

# Numbers that are standalone measurements, not digits inside a word like HbA1c
NUMBER = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])")


def words(text):
    return set(re.sub(r"[^a-z0-9 ]", " ", str(text).lower()).split()) - STOP


def best_match(needle, candidates, key=None, threshold=0.6):
    """
    Loose term-overlap match, scored against the shorter of the two term sets.
    `key` extracts the comparable string from each candidate.
    Returns the matching candidate, or None.

    (review_brief.py carries its own copy of this. Kept separate on purpose —
    the Phase 2 checker and the UI shouldn't break each other.)
    """
    nw = words(needle)
    if not nw:
        return None
    best, best_score = None, 0.0
    for cand in candidates:
        text = key(cand) if key else cand
        hw = words(text)
        if not hw:
            continue
        score = len(nw & hw) / min(len(nw), len(hw))
        if score > best_score:
            best, best_score = cand, score
    return best if best_score >= threshold else None


# ── Source record builders ────────────────────────────────────────────────

def _src(table, label, detail="", date="", code=""):
    return {"table": table, "label": label, "detail": detail,
            "date": date, "code": code}


def _condition_src(c):
    return _src("conditions", c["condition"],
                f"Active diagnosis since {c['since']}" if c.get("since") else
                "Active diagnosis",
                c.get("since", ""), c.get("snomed_code", ""))


def _med_src(m):
    return _src("medications", m["medication"],
                f"Prescribed {m['prescribed']}" if m.get("prescribed") else "",
                m.get("prescribed", ""), m.get("rxnorm_code", ""))


def _lab_src(l):
    return _src("observations", f"{l['test']}: {l['value']} {l['unit']}".strip(),
                f"Recorded {l['date']}", l.get("date", ""),
                l.get("loinc_code", ""))


def _enc_src(e):
    return _src("encounters", e["type"],
                f"{e['class']} encounter on {e['date']}", e.get("date", ""))


def _gap_src(g):
    bits = [g.get("guideline", "")]
    if g.get("days_overdue"):
        bits.append(f"{g['days_overdue']} days overdue")
    if g.get("last_date"):
        bits.append(f"last {g['last_date']}")
    return _src("care_gaps", g["gap"], " · ".join(b for b in bits if b),
                g.get("last_date", ""))


def _ix_src(ix):
    return _src("drug_interactions",
                f"{ix['drug_a_generic']} ↔ {ix['drug_b_generic']}",
                f"{ix['severity'].title()} · DDInter · "
                f"prescribed as {ix['drug_a']} / {ix['drug_b']}")


# ── Element-level tracing ─────────────────────────────────────────────────

def trace_conditions(brief, data):
    out = []
    for item in brief.get("key_conditions", []):
        m = best_match(item.get("condition", ""), data["conditions"],
                       key=lambda c: c["condition"])
        out.append({**item, "sources": [_condition_src(m)] if m else []})
    return out


def trace_medications(brief, data):
    out = []
    for item in brief.get("current_medications", []):
        m = best_match(item.get("medication", ""), data["medications"],
                       key=lambda x: x["medication"])
        out.append({**item, "sources": [_med_src(m)] if m else []})
    return out


def _ix_sources(ix, data):
    """Cite the interaction row, then the two prescriptions behind it."""
    sources = [_ix_src(ix)]
    for drug in (ix["drug_a"], ix["drug_b"]):
        med = best_match(drug, data["medications"], key=lambda x: x["medication"])
        if med:
            sources.append(_med_src(med))
    return sources


def canonical_drugs(text):
    """Swap each US drug alias for the DDInter name the interaction table
    uses, so 'albuterol' in a brief can match 'salbutamol' in the record."""
    text = str(text).lower()
    for alias, canonical in SYNONYMS.items():
        text = re.sub(rf"\b{re.escape(alias)}\b", canonical, text)
    return text


def trace_alerts(brief, data):
    out = []
    for item in brief.get("medication_alerts", []):
        pair = canonical_drugs(" ".join(item.get("drugs", [])))
        m = best_match(pair, data["drug_interactions"],
                       key=lambda ix: canonical_drugs(
                           f"{ix['drug_a_generic']} {ix['drug_b_generic']}"))
        out.append({**item, "sources": _ix_sources(m, data) if m else []})
    return out


def trace_gaps(brief, data):
    out = []
    for item in brief.get("care_gaps", []):
        m = best_match(item.get("gap", ""), data["care_gaps"],
                       key=lambda g: g["gap"])
        sources = [_gap_src(m)] if m else []
        if m and m.get("condition"):
            cond = best_match(m["condition"], data["conditions"],
                              key=lambda c: c["condition"])
            if cond:
                sources.append(_condition_src(cond))
        out.append({**item, "sources": sources})
    return out


def _names_drug(text_words, generic):
    """True if the text names this DDInter drug in full, by its DDInter name
    or the US name it was matched from ('albuterol' for 'salbutamol')."""
    names = [generic] + [us for us, inn in SYNONYMS.items() if inn == generic]
    return any(words(n) <= text_words for n in names)


def trace_social(brief, data):
    """Social history entries trace to the patient's social findings."""
    out = []
    for text in brief.get("social_history", []):
        m = best_match(text, data.get("social_history", []),
                       key=lambda s: s["finding"])
        sources = [_src("conditions", m["finding"], f"Recorded {m['since']}",
                        m["since"], m["snomed_code"])] if m else []
        out.append({"text": text, "sources": sources})
    return out


def trace_actions(brief, data):
    """Priority actions trace to a gap, else an interaction, else a condition,
    else any lab value they quote."""
    out = []
    for text in brief.get("priority_actions", []):
        gap = best_match(text, data["care_gaps"], key=lambda g: g["gap"])
        if gap:
            out.append({"text": text, "sources": [_gap_src(gap)]})
            continue
        # Only when both drugs are named in full. best_match would accept
        # "titrate insulin glargine" for an insulin glargine ↔ metformin pair.
        tw = words(text)
        ix = next((ix for ix in data["drug_interactions"]
                   if _names_drug(tw, ix["drug_a_generic"])
                   and _names_drug(tw, ix["drug_b_generic"])), None)
        if ix:
            out.append({"text": text, "sources": _ix_sources(ix, data)})
            continue
        cond = best_match(text, data["conditions"], key=lambda c: c["condition"])
        if cond:
            out.append({"text": text, "sources": [_condition_src(cond)]})
            continue

        # Actions that quote a value ("creatinine 2.7633") cite that lab
        numbers = set(NUMBER.findall(text))
        sources = [_lab_src(l) for l in data["recent_labs"]
                   if str(l["value"]).strip() in numbers]
        # ...or name a coded result ("worsening response to cancer treatment"),
        # or a kind of visit ("recent postoperative status")
        if not sources:
            coded = [l for l in data["recent_labs"] if not NUMBER.fullmatch(
                str(l["value"]).strip())]
            obs = best_match(text, coded, key=lambda l: l["test"])
            if obs:
                sources.append(_lab_src(obs))
            enc = next((e for e in data["recent_encounters"]
                        if (ew := _encounter_words(e["type"]))
                        and ew <= words(text)), None)
            if enc:
                sources.append(_enc_src(enc))
        out.append({"text": text, "sources": sources})
    return out


# Words every encounter type shares; matching on them would tie any action
# to any visit ("check blood pressure" ↔ "Encounter for check up").
_GENERIC_ENCOUNTER = {"follow", "up", "visit", "encounter", "procedure",
                      "regime", "therapy", "check", "admission", "hospital",
                      "general", "examination", "patient"}


def _encounter_words(encounter_type):
    """Distinctive words of an encounter type: 'Postoperative follow-up
    visit (procedure)' → {'postoperative'}."""
    return {w for w in words(encounter_type)
            if w not in _GENERIC_ENCOUNTER and len(w) >= 6}


# ── Prose tracing: numbers and condition names inside sentences ──────────

def trace_prose(text, data):
    """
    Split a sentence into segments, each either plain text or a cited span.
    Numbers are matched to lab values; names are matched to conditions and
    medications. Returns [{"text": str, "source": dict|None}, ...].
    """
    if not text:
        return []

    spans = []   # (start, end, source)

    for lab in data["recent_labs"]:
        value = str(lab["value"]).strip()
        if not value:
            continue
        for m in re.finditer(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])",
                             text):
            # "66-year-old" is an age, not a heart rate that happens to be 66
            if re.match(r"[\s-]*years?\b", text[m.end():], re.I):
                continue
            spans.append((m.start(), m.end(), _lab_src(lab)))

    for cond in data["conditions"]:
        # Match the distinctive part of the name, e.g. "hypertension"
        for term in sorted(words(cond["condition"]), key=len, reverse=True)[:2]:
            if len(term) < 5:
                continue
            for m in re.finditer(rf"\b{re.escape(term)}\w*", text, re.I):
                spans.append((m.start(), m.end(), _condition_src(cond)))
            break

    for med in data["medications"]:
        generic = str(med["medication"]).split()[0].lower()
        if len(generic) < 5:
            continue
        for m in re.finditer(rf"\b{re.escape(generic)}\w*", text, re.I):
            spans.append((m.start(), m.end(), _med_src(med)))

    # Drop overlaps, earliest and longest first
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    kept, last_end = [], -1
    for start, end, src in spans:
        if start >= last_end:
            kept.append((start, end, src))
            last_end = end

    segments, cursor = [], 0
    for start, end, src in kept:
        if start > cursor:
            segments.append({"text": text[cursor:start], "source": None})
        segments.append({"text": text[start:end], "source": src})
        cursor = end
    if cursor < len(text):
        segments.append({"text": text[cursor:], "source": None})
    return segments


ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def untraced_numbers(text, data):
    """Numbers in prose that match no stored lab value — worth a flag."""
    text = ISO_DATE.sub(" ", text or "")   # 2025-11-02 is a date, not a value
    known = {str(l["value"]).strip() for l in data["recent_labs"]}
    known |= {str(data["demographics"]["age"])}
    source_text = " ".join([c["condition"] for c in data["conditions"]] +
                           [m["medication"] for m in data["medications"]])
    known |= set(NUMBER.findall(source_text))
    return [n for n in dict.fromkeys(NUMBER.findall(text or ""))
            if n not in known and not re.fullmatch(r"(19|20)\d{2}", n)]


# ── Top level ────────────────────────────────────────────────────────────

def annotate_brief(brief, data):
    """Return the brief with every element carrying its source rows."""
    conditions = trace_conditions(brief, data)
    medications = trace_medications(brief, data)
    alerts = trace_alerts(brief, data)
    gaps = trace_gaps(brief, data)
    actions = trace_actions(brief, data)
    social = trace_social(brief, data)

    traceable = conditions + medications + alerts + gaps + actions + social
    traced = sum(1 for item in traceable if item.get("sources"))

    return {
        "meta": brief.get("_meta", {}),
        "summary": trace_prose(brief.get("patient_summary", ""), data),
        "recent_activity": trace_prose(brief.get("recent_activity", ""), data),
        "key_conditions": conditions,
        "current_medications": medications,
        "medication_alerts": alerts,
        "care_gaps": gaps,
        "priority_actions": actions,
        "social_history": social,
        "community_context": brief.get("community_context", []),
        "sdoh_flags": brief.get("sdoh_flags", []),   # briefs from before the split
        "coverage": {
            "traced": traced,
            "total": len(traceable),
            "percent": round(100 * traced / len(traceable)) if traceable else 0,
        },
        "unmatched_numbers": (
            untraced_numbers(brief.get("patient_summary", ""), data) +
            untraced_numbers(brief.get("recent_activity", ""), data)),
        "parse_error": bool(brief.get("parse_error")),
    }
