# CareThread

CareThread prepares pre-visit briefs for care coordinators. For each patient it pulls together diagnoses, medications, labs and visits, flags overdue screenings (care gaps) and drug-drug interactions, adds community-level social determinants of health (SDOH) by zip code, and has an LLM write a one-page brief. A verification layer then checks every claim in the brief against the source records, and the web interface links each claim back to the record that produced it.

All patient data is synthetic, generated with [Synthea](https://github.com/synthetichealth/synthea). No real patient records are used.

## How it works

```
Synthea FHIR bundles ──► parse_fhir.py ──► carethread.db (SQLite)
                                              │
        care_gaps.py ─────── overdue screenings ┤
        drug_interactions.py ─ DDInter lookups ─┤
        link_sdoh.py ──────── AHRQ SDOH by zip ─┘
                                              │
run_pipeline.py: assemble_patient → generate_brief (LLM) → verify_brief
                                              │
                                        briefs/*.json
                                              │
                         app.py: dashboard + traceable briefs
```

| File | Purpose |
|---|---|
| `parse_fhir.py` | Reads Synthea FHIR R4 bundles into `carethread.db` |
| `care_gaps.py` | Guideline rules for overdue screenings (diabetes, hypertension, cancer screening, vaccines, …) |
| `drug_interactions.py` | Matches medication names to [DDInter](https://ddinter.scbdd.com/) and records interactions |
| `add_interactions.py` | Curated, cited interaction pairs that DDInter misses |
| `link_sdoh.py` | Joins AHRQ SDOH zip-code data to patients |
| `assemble_patient.py` | Collects one patient's data for the prompt |
| `generate_brief.py` / `llm_client.py` | Builds the prompt and calls the configured LLM backend |
| `verify_brief.py` / `provenance.py` | Checks the brief against the source data and annotates each claim |
| `run_pipeline.py` | Generates and verifies briefs in batch, logging to `runs/` |
| `app.py` | Flask web interface |
| `test_verify.py`, `validate.py`, `review_brief.py`, `check_pair.py` | Testing and debugging tools |

## Setup

Requires Python 3.13.

```bash
git clone https://github.com/anika-nur/CareThread.git
cd CareThread
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then fill in your LLM credentials
```

### Build the database

`carethread.db` and the SDOH spreadsheet are too large for the repo, so you rebuild them locally. Run these from the `carethread/` folder:

1. **Generate patients** with [Synthea](https://github.com/synthetichealth/synthea) (Java 17+), e.g. `./run_synthea -p 460`. This writes FHIR bundles to `synthea/output/fhir/`.
2. **Parse them:** `python parse_fhir.py /path/to/synthea/output/fhir/`. Delete any existing `carethread.db` first, because re-running on top of it duplicates rows.
3. **Detect care gaps:** `python care_gaps.py`
4. **Check drug interactions:** `python drug_interactions.py`. The DDInter CSVs are already in `ddinter_data/`.
5. **Link SDOH:** download `clh_2023_zipcode_2_0.xlsx` from AHRQ's [Social Determinants of Health Database](https://www.ahrq.gov/sdoh/data-analytics/sdoh-data.html) into `carethread/`, then run `python link_sdoh.py clh_2023_zipcode_2_0.xlsx`.

## Usage

From the `carethread/` folder:

```bash
python run_pipeline.py              # briefs for the 10 patients with the most care gaps
python run_pipeline.py --count 50   # top 50
python run_pipeline.py --all        # every living patient

python app.py                       # http://127.0.0.1:5001
```

The dashboard ranks patients by urgency (high-severity gaps, then major interactions). Each patient page shows the brief, with every claim linked to its source record, and the verifier's verdict (PASS / WARN / FAIL).

The repo includes the briefs from the latest run in `briefs/`. Once the database is built, the app shows them without calling the LLM.

### Testing

```bash
python test_verify.py   # checks that the verifier still rejects fabricated claims
```

Run this after any change to `verify_brief.py`. It exits with status 1 if a case fails.

## LLM backend

Set `LLM_BACKEND` in `.env`:

- `colby` (default): the Colby DAI LiteLLM proxy, which is OpenAI-compatible. Needs `LITELLM_BASE_URL` and `LITELLM_API_KEY`.
- `gemini`: Google Gemini, kept as a failover and quality baseline. Needs `GEMINI_API_KEY` and `pip install google-genai`.
