"""
schedule.py — Simulated appointment schedule for the demo dashboard.

Synthea has no appointment data, so each patient with a brief gets a slot
for "today" and a visit type, seeded by patient_id. Assignments live in
schedule.json, kept apart from the clinical tables, and are only ever added
to: a new brief never moves an existing patient's slot.

Usage:
    from schedule import load_schedule
    appts = load_schedule(patient_ids)   # {patient_id: {"time", "visit_type"}}

    python schedule.py                    # build or refresh schedule.json
"""

import hashlib
import json
from pathlib import Path

SCHEDULE_PATH = Path("schedule.json")

VISIT_TYPES = ["Follow-up", "Annual exam", "New concern", "Chronic care"]

# Fictional providers for the demo. Each sees one patient per slot.
PROVIDERS = ["Dr. A. Rivera, MD", "J. Chen, NP"]

# 8:00 to 4:30 in 15-minute slots, lunch 12:00–12:45 blocked off.
# One patient per provider per slot.
SLOTS = [f"{m // 60:02d}:{m % 60:02d}"
         for m in range(8 * 60, 16 * 60 + 31, 15)
         if not 12 * 60 <= m < 13 * 60]
PER_SLOT = len(PROVIDERS)


def _seed(patient_id, salt):
    return int(hashlib.sha256(f"{salt}:{patient_id}".encode()).hexdigest(), 16)


def _assign(patient_id, taken):
    """Seeded preferred slot, probing forward to the next one with room."""
    start = _seed(patient_id, "slot") % len(SLOTS)
    for i in range(len(SLOTS)):
        slot = SLOTS[(start + i) % len(SLOTS)]
        if taken.get(slot, 0) < PER_SLOT:
            return slot
    return SLOTS[start]   # day is full; double up rather than drop anyone


def _assign_providers(appts):
    """Give every appointment without a provider one who is free in its slot:
    the seeded choice if available, else the other. Returns True if any changed."""
    busy = {}
    for a in appts.values():
        if "provider" in a:
            busy.setdefault(a["time"], set()).add(a["provider"])
    changed = False
    for pid in sorted(appts):
        a = appts[pid]
        if "provider" in a:
            continue
        taken = busy.setdefault(a["time"], set())
        start = _seed(pid, "provider") % len(PROVIDERS)
        order = PROVIDERS[start:] + PROVIDERS[:start]
        a["provider"] = next((p for p in order if p not in taken), order[0])
        taken.add(a["provider"])
        changed = True
    return changed


def load_schedule(patient_ids):
    """Appointments for these patients, assigning and saving any new ones."""
    appts = json.loads(SCHEDULE_PATH.read_text()) if SCHEDULE_PATH.exists() else {}

    new = sorted(pid for pid in set(patient_ids) if pid not in appts)
    changed = bool(new)
    if new:
        taken = {}
        for a in appts.values():
            taken[a["time"]] = taken.get(a["time"], 0) + 1
        for pid in new:
            slot = _assign(pid, taken)
            taken[slot] = taken.get(slot, 0) + 1
            appts[pid] = {
                "time": slot,
                "visit_type": VISIT_TYPES[_seed(pid, "visit") % len(VISIT_TYPES)],
            }
    if _assign_providers(appts) or changed:
        SCHEDULE_PATH.write_text(json.dumps(appts, indent=2, sort_keys=True) + "\n")

    return {pid: appts[pid] for pid in patient_ids}


def display_time(hhmm):
    """'13:15' → '1:15 PM'."""
    h, m = map(int, hhmm.split(":"))
    return f"{(h - 1) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"


if __name__ == "__main__":
    ids = [p.stem for p in Path("briefs").glob("*.json")]
    appts = load_schedule(ids)
    for pid, a in sorted(appts.items(), key=lambda kv: kv[1]["time"]):
        print(f"{display_time(a['time']):>8}  {a['provider']:<18}  "
              f"{a['visit_type']:<12}  {pid}")
    print(f"\n{len(appts)} appointments in {SCHEDULE_PATH}")
