"""
sources.py — Which source system each record came from.

Every clinical row carries the id of the Encounter it was recorded in, and
every Encounter names the Organization that provided it (Synthea's
serviceProvider). That organization is the row's source system.

Three rules, all deterministic:

1. Role of an encounter: read from its class and type, never from the
   organization's name. Synthea borrows real provider names and assigns them
   loosely; an "Emergency Center" does eye exams and a home-health agency acts
   as someone's primary care, so a name says little about what happened there.

2. Organizations that are the same entity are merged into one group: names
   compared without punctuation, corporate suffixes, anything after the first
   comma ("NASHOBA VALLEY MEDICAL CENTER, A STEWARD FAMILY HOSPITAL") or a
   "- 1" site suffix, within the same state. Sites of one organization share
   one record system, so they count as one source.

3. The patient's home clinic is the organization group with the most
   primary-care exams (general examination, well child visit) in the last
   HOME_WINDOW_YEARS years, ties going to the most recent visit. Failing
   that: the same over all time, then any wellness visit, then any visit.
   "Encounter for check up" is left out of the first two tiers: Synthea
   often files it about two weeks after the yearly exam, at a hospital,
   and counting it ties the hospital with the practice.
   home_clinic_overrides.json can pin a different group per patient.

Source organizations come from Synthea's synthetic data; the home clinic is
inferred from visit history.

Usage:
    python sources.py      # (re)build the home_clinic table
"""

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from config import DB_PATH

# ── Encounter roles ──────────────────────────────────────────────────────

# Synthea emits five encounter classes. Urgent care has no class of its own:
# it is AMB with this encounter type.
URGENT_CARE_TYPES = ("urgent care clinic",)

# Encounter types Synthea uses for preventive / primary care visits
WELLNESS_TYPES = ("general examination of patient", "encounter for check up",
                  "well child visit")

# The subset that marks the patient's own primary care practice
PRIMARY_EXAM_TYPES = ("general examination of patient", "well child visit")

ROLE_LABELS = {
    "home_clinic":   "Home clinic",
    "primary_care":  "Other primary care",
    "emergency":     "Emergency department",
    "inpatient":     "Hospital inpatient",
    "urgent_care":   "Urgent care",
    "outpatient":    "Outpatient / specialty",
    "home_health":   "Home health",
    "telehealth":    "Telehealth",
}

CLASS_ROLES = {"EMER": "emergency", "IMP": "inpatient",
               "HH": "home_health", "VR": "telehealth"}


def encounter_role(enc_class, enc_type):
    """What kind of care an encounter was, from its class and type alone.
    'wellness' and 'outpatient' become 'home_clinic' / 'primary_care' only
    once the patient's home clinic is known (see display_role)."""
    if enc_class in CLASS_ROLES:
        return CLASS_ROLES[enc_class]
    t = (enc_type or "").lower()
    if any(u in t for u in URGENT_CARE_TYPES):
        return "urgent_care"
    if any(w in t for w in WELLNESS_TYPES):
        return "wellness"
    return "outpatient"


def display_role(role, at_home_clinic):
    """Label for a source panel: anything recorded at the home clinic is
    'Home clinic', unless it was an ED or inpatient stay there."""
    if at_home_clinic and role not in ("emergency", "inpatient"):
        return ROLE_LABELS["home_clinic"]
    if role == "wellness":
        return ROLE_LABELS["primary_care"]
    return ROLE_LABELS[role]


# ── Organizations ────────────────────────────────────────────────────────

CORPORATE_SUFFIXES = {"INC", "LLC", "PC", "PLLC", "LLP", "CORP", "CORPORATION",
                      "CO", "PA", "LTD", "THE"}


def org_merge_key(name, state):
    """'NASHOBA VALLEY MEDICAL CENTER, A STEWARD FAMILY HOSPITAL' and
    'NASHOBA VALLEY MEDICAL CENTER' in the same state → the same key."""
    base = str(name).upper().split(",")[0]
    base = re.sub(r"\s+-\s*\d+$", "", base)
    base = re.sub(r"[^A-Z0-9& ]", " ", base)
    words = [w for w in base.split() if w not in CORPORATE_SUFFIXES]
    return f"{' '.join(words)}|{state or ''}"


# Words kept as written, where plain title case would get them wrong
NAME_FORMS = {"UMASS": "UMass", "NYU": "NYU", "USC": "USC", "YMCA": "YMCA",
              "VNA": "VNA", "VHS": "VHS", "BHC": "BHC", "ESRD": "ESRD", "HIV": "HIV",
              "MD": "MD", "DO": "DO", "NP": "NP", "PT": "PT", "AP": "AP", "WP": "WP",
              "LA": "LA", "MA": "MA", "II": "II", "III": "III",
              "HEALTHALLIANCE": "HealthAlliance"}
NAME_SMALL_WORDS = {"of", "and", "the", "at", "for", "in", "on", "a", "an", "to"}
# Trailing words dropped from display names: legal forms and credentials.
# Display only; the merge key keeps its own list so groups don't shift.
DISPLAY_SUFFIXES = CORPORATE_SUFFIXES - {"THE"} | {"INCORPORATED"}
CREDENTIALS = {"MD", "DO", "NP", "DDS", "DMD", "DPM", "PHD", "PSYD", "RN", "APRN",
               "FNP", "PAC", "MPH", "LCSW", "FACP", "FAAFP"}


def _name_part(part):
    """One hyphen-separated piece: 'NP' → 'NP', '(WESTBURY)' → '(Westbury)'."""
    core = part.strip("(),")
    if core.upper() in NAME_FORMS:
        return part.replace(core, NAME_FORMS[core.upper()])
    return re.sub(r"[A-Za-z]", lambda m: m.group().upper(), part.lower(), count=1)


def org_display(name):
    """'CEDAR STREET FAMILY CLINIC INC' → 'Cedar Street Family Clinic',
    'JILL P. ALTMAN, M.D., P.C.' → 'Jill P. Altman', 'ARBOUR HOSPITAL, THE' →
    'The Arbour Hospital'. Keeps the whole name, drops trailing legal forms
    and credentials, then title-cases it."""
    base = re.sub(r"\s*-\s*\d*$", "", str(name).strip())   # 'INC - 1', 'CORPORATION-'
    base = re.sub(r"\s*,\s*", ", ", base)                  # 'WELLNESS,LLC', 'CARE , INC'
    words, article = base.split(), False
    while len(words) > 1:
        key = re.sub(r"[^A-Z]", "", words[-1].upper())
        if key == "THE":                                   # 'HIGHLANDS, THE', '(THE)'
            article = True
        elif key in CREDENTIALS:
            if len(words) < 3:                             # 'URGENT MD' is the name
                break
        elif key not in DISPLAY_SUFFIXES:
            break
        words.pop()
        words[-1] = words[-1].rstrip(",")
    if article:
        words.insert(0, "THE")
    out = []
    for i, word in enumerate(words):
        if re.fullmatch(r"(?:[A-Za-z]\.){2,},?|[A-Za-z]&[A-Za-z]", word):
            out.append(word.upper())                 # 'D.O.', 'A&H'
        elif i and word.lower() in NAME_SMALL_WORDS:
            out.append(word.lower())
        else:
            out.append(re.sub(r"[^-/]+", lambda m: _name_part(m.group()), word))
    return " ".join(out) or str(name)


def org_short(name):
    """Compact organization name for the small chip beside a record."""
    if name.startswith("UMass Memorial HealthAlliance Clinton Hospital"):
        return name.replace("UMass Memorial HealthAlliance Clinton Hospital",
                            "UMass Clinton Hospital", 1)
    return name


# ── Where a record came from ─────────────────────────────────────────────

# The records each gap rule reads its last-done date from, as
# (table, date column, codes). Codes are the rules' own (care_gaps.py).
def _gap_evidence():
    from care_gaps import (CERVICAL_SCREENING_CODES, COLONOSCOPY_CODES,
                           EYE_EXAM_CODES, FLU_CVX_CODES, MAMMOGRAM_CODES,
                           SMOKING_CESSATION_CODES)
    obs = "observations", "effective_date"
    proc = "procedures", "performed_date"
    return {
        "HbA1c": [(*obs, ["4548-4"])],
        "Blood Pressure": [(*obs, ["8480-6", "85354-9"])],
        "Colonoscopy": [(*proc, COLONOSCOPY_CODES)],
        "Flu Vaccine": [("immunizations", "occurrence_date", FLU_CVX_CODES)],
        "Depression Follow-Up": [(*obs, ["55758-7"])],
        "Lipid Panel": [(*obs, ["57698-3", "2093-3"])],
        "Mammogram": [(*proc, MAMMOGRAM_CODES)],
        "Cervical Screening": [(*proc, CERVICAL_SCREENING_CODES), (*obs, ["10524-7"])],
        "BMI Screening": [(*obs, ["39156-5"])],
        "Diabetic Eye Exam": [(*proc, EYE_EXAM_CODES)],
        "Nephropathy Screening": [(*obs, ["14959-1", "14957-5", "30000-4", "13705-9"])],
        "Smoking Cessation Counseling": [("care_plan_activities", "start_date",
                                          SMOKING_CESSATION_CODES)],
    }


def gap_subject(gap_type):
    """'Smoking Cessation Counseling Missing' → 'Smoking Cessation Counseling'."""
    for suffix in (" Overdue", " Missing", " Due"):
        gap_type = gap_type.removesuffix(suffix)
    return gap_type


class SourceIndex:
    """Where one patient's records came from: organization (display name)
    and role, for medications and for the record behind a gap's date.
    Needs a database built with source tagging (build_frag_db.py)."""

    # Each row's organization group, the encounter role, and whether that
    # group is the patient's home clinic
    ORIGIN = ("JOIN encounters e ON e.encounter_id = t.encounter_id "
              "JOIN organizations o ON o.organization_id = e.organization_id "
              "JOIN organizations g ON g.organization_id = o.group_id "
              "LEFT JOIN home_clinic h ON h.patient_id = t.patient_id")
    ORIGIN_COLS = "g.name, e.role, COALESCE(o.group_id = h.group_id, 0)"

    def __init__(self, conn, patient_id):
        self.conn, self.patient_id = conn, patient_id
        self.organization_count = conn.execute(
            "SELECT COUNT(DISTINCT o.group_id) FROM encounters e "
            "JOIN organizations o ON o.organization_id = e.organization_id "
            "WHERE e.patient_id = ?", (patient_id,)
        ).fetchone()[0]
        self.meds = {}            # (display, authored_on) → source
        self.latest_med = {}      # display → source of its latest active row
        self.clinic_meds = set()  # active displays with a row at the home clinic
        for display, date, status, name, role, home in conn.execute(
                f"SELECT t.display, t.authored_on, t.status, {self.ORIGIN_COLS} "
                f"FROM medications t {self.ORIGIN} WHERE t.patient_id = ? "
                "ORDER BY t.authored_on, g.name", (patient_id,)):
            src = self._source(name, role, home)
            self.meds.setdefault((display, date), src)
            if status == "active":
                self.latest_med[display] = src
                if home:
                    self.clinic_meds.add(display)

    @staticmethod
    def _source(name, role, home):
        display = org_display(name)
        return {"org": display, "short_org": org_short(display),
                "role": display_role(role, bool(home)),
                "at_home": bool(home)}

    def medication(self, display, date=""):
        """Where this prescription was written (the latest one if no date)."""
        return self.meds.get((display, date)) or self.latest_med.get(display)

    def gap(self, gap_type, date):
        """Where the record that set a gap's last-done date was made."""
        if not date:
            return None
        subject = gap_subject(gap_type)
        if subject == "Annual Wellness Visit":
            from care_gaps import WELLNESS_ENCOUNTER_CODES
            marks = ",".join("?" * len(WELLNESS_ENCOUNTER_CODES))
            row = self.conn.execute(
                f"SELECT {self.ORIGIN_COLS} FROM encounters t {self.ORIGIN} "
                f"WHERE t.patient_id = ? AND t.start_date = ? "
                f"AND t.encounter_code IN ({marks}) "
                "ORDER BY 3 DESC, 1 LIMIT 1",
                (self.patient_id, date, *WELLNESS_ENCOUNTER_CODES)).fetchone()
            return self._source(*row) if row else None
        for table, date_col, codes in _gap_evidence().get(subject, []):
            marks = ",".join("?" * len(codes))
            row = self.conn.execute(
                f"SELECT {self.ORIGIN_COLS} FROM {table} t {self.ORIGIN} "
                f"WHERE t.patient_id = ? AND t.{date_col} = ? AND t.code IN ({marks}) "
                "ORDER BY 3 DESC, 1 LIMIT 1",
                (self.patient_id, date, *codes)).fetchone()
            if row:
                return self._source(*row)
        return None


def load_organizations(fhir_dir):
    """Organization rows from Synthea's hospitalInformation*.json files.
    group_id is the smallest id among organizations sharing a merge key."""
    orgs = {}
    for path in sorted(Path(fhir_dir).glob("hospitalInformation*.json")):
        for entry in json.loads(path.read_text()).get("entry", []):
            r = entry.get("resource", {})
            if r.get("resourceType") != "Organization":
                continue
            address = (r.get("address") or [{}])[0]
            orgs[r["id"]] = {
                "organization_id": r["id"],
                "name": r.get("name", ""),
                "city": address.get("city", ""),
                "state": address.get("state", ""),
            }

    groups = defaultdict(list)
    for org in orgs.values():
        groups[org_merge_key(org["name"], org["state"])].append(org["organization_id"])
    for ids in groups.values():
        for oid in ids:
            orgs[oid]["group_id"] = min(ids)
    return [orgs[oid] for oid in sorted(orgs)]


# ── Home clinic ──────────────────────────────────────────────────────────

HOME_WINDOW_YEARS = 3
OVERRIDES_FILE = Path(__file__).with_name("home_clinic_overrides.json")


def load_overrides(path=OVERRIDES_FILE):
    """patient_id → organization group_id, from the overrides file."""
    if not path.exists():
        return {}
    return {pid: o["group_id"] for pid, o in
            json.loads(path.read_text()).get("overrides", {}).items()}


def infer_home_clinics(conn, today=None, overrides=None):
    """[(patient_id, group_id, visits, last_visit, rule)] for every patient
    with at least one encounter. `visits` counts the visits the rule used."""
    today = today or datetime.now().date()
    window_start = today.replace(year=today.year - HOME_WINDOW_YEARS).isoformat()
    overrides = load_overrides() if overrides is None else overrides

    exams = defaultdict(lambda: defaultdict(list))    # pid → group → [dates]
    wellness = defaultdict(lambda: defaultdict(list))
    any_org = defaultdict(lambda: defaultdict(list))
    for pid, group, role, enc_type, date in conn.execute(
            "SELECT e.patient_id, o.group_id, e.role, e.encounter_type, e.start_date "
            "FROM encounters e JOIN organizations o "
            "  ON o.organization_id = e.organization_id"):
        any_org[pid][group].append(date)
        if role == "wellness":
            wellness[pid][group].append(date)
        if any(t in enc_type.lower() for t in PRIMARY_EXAM_TYPES):
            exams[pid][group].append(date)

    def pick(by_group, since=""):
        counts = {g: [d for d in dates if d >= since] for g, dates in by_group.items()}
        counts = {g: d for g, d in counts.items() if d}
        if not counts:
            return None
        # Most visits, then most recent visit, then id — never order-dependent
        g = max(counts, key=lambda g: (len(counts[g]), max(counts[g]), g))
        return g, len(counts[g]), max(counts[g])

    rows = []
    for pid in sorted(any_org):
        if pid in overrides:
            group = overrides[pid]
            dates = exams[pid].get(group, [])
            rows.append((pid, group, len(dates), max(dates, default=""), "override"))
            continue
        for by_group, since, rule in (
                (exams[pid], window_start, f"primary-care exams, last {HOME_WINDOW_YEARS} years"),
                (exams[pid], "", "primary-care exams, all time"),
                (wellness[pid], "", "wellness visits, all time"),
                (any_org[pid], "", "all visits, all time")):
            picked = pick(by_group, since)
            if picked:
                rows.append((pid, *picked, rule))
                break
    return rows


def write_home_clinics(conn):
    conn.execute("DROP TABLE IF EXISTS home_clinic")
    conn.execute("""CREATE TABLE home_clinic (
        patient_id TEXT PRIMARY KEY, group_id TEXT,
        visits INTEGER, last_visit TEXT, rule TEXT
    )""")
    rows = infer_home_clinics(conn)
    conn.executemany("INSERT INTO home_clinic VALUES (?,?,?,?,?)", rows)
    conn.commit()
    return rows


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    rows = write_home_clinics(conn)
    print(f"home_clinic: {len(rows)} patients")
    conn.close()
