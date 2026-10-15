# CareThread — Mentor Briefing

## What I'm building

CareThread is a tool for community health center doctors. These clinics (called FQHCs) serve people who are uninsured, on Medicaid, or live in areas where there's no other doctor around. In Maine alone, they see over 210,000 patients a year.

The problem is that these patients often get care from a bunch of different places that don't talk to each other. Someone might go to an ER in one town, see a specialist in another, fill prescriptions at a third. When they show up at their clinic, the doctor has about 15 minutes and a chart that only shows what happened at that clinic. Everything else is invisible. So tests get repeated, dangerous drug combinations get missed, and overdue screenings just never get caught.

CareThread pulls together a patient's scattered records, runs them against clinical guidelines to catch what's missing or dangerous, and then uses an LLM to write a short summary the doctor can read before the visit. Every claim in the summary links back to the actual data it came from, so the doctor doesn't have to take anything on faith.

---

## What I've actually built so far

The project has two phases done out of four total.

### Phase 1: Data pipeline and detection engine

I used Synthea (an open-source tool that generates realistic fake patient records in the standard healthcare data format called FHIR) to create about 460 patients. After filtering out deceased patients, I have 380 living patients in a SQLite database with their diagnoses, medications, lab results, visits, immunizations, and procedures all parsed out.

On top of that I built two detection systems.

The first is a **care gap detector with 13 rules**. Each rule encodes a real clinical guideline. For example, one rule says "if a patient has diabetes, they should have had an HbA1c blood test within the last 6 months." The system checks every patient against every applicable rule and flags what's overdue. Across my 380 patients, it's finding real, valid gaps like overdue diabetic eye exams, missed colonoscopies, lapsed blood pressure checks, and missing depression follow-ups.

The second is a **drug interaction checker** using DDInter, an open-access database with over 300,000 known drug interaction pairs. The tricky part was matching drug names, because Synthea writes medications like "24 HR Metformin hydrochloride 500 MG Extended Release Oral Tablet" while DDInter just says "Metformin." I built a name extraction pipeline that strips out the dosage, formulation, and salt forms to find the generic ingredient, then looks it up in DDInter. It matches 125 out of 126 unique medications in my dataset. Across all patients, it found 406 graded interactions, including 76 major ones.

### Phase 2: LLM summarization layer

I wrote an assembly script that gathers everything about one patient (demographics, conditions, meds, recent labs, care gaps, drug interactions, community-level social determinant data) into a single structured package. That package gets sent to an LLM with a detailed system prompt that says "turn this into a clinical pre-visit brief with these specific sections, cite your sources for every claim, and do not make anything up." The output is a JSON object with sections for patient summary, key conditions, medications, alerts, care gaps, social flags, and a prioritized action list. I also built a verification script that takes each generated brief and checks every claim against the source data to catch hallucinations.

My best demo patient right now is a 59-year-old woman with diabetes, hypertension, obesity, hyperlipidemia, and anxiety. She has 6 care gaps (including a high-severity missing diabetic eye exam) and a moderate drug interaction between lisinopril and metformin. The system catches all of it and writes a brief a doctor could actually scan before walking into the room.

---

## What I'm not building (and why)

The full version of this tool would include a **record linkage** component. That's the part where you take records from multiple disconnected systems and figure out which ones belong to the same person, even when the name is spelled differently or the address doesn't match. It's the hardest piece technically, and for the competition demo I don't need it. My synthetic patients already come with unified records, so I can show the gap detection and summarization working on complete data and describe record linkage as a planned feature. This was a scoping decision to fit the November 1 deadline, not a gap in the design.

---

## What's left

**Phase 3** is a web interface where a doctor can select a patient and see the brief, and a care coordinator can see a dashboard of their whole patient panel sorted by urgency.

**Phase 4** is the competition submission materials: a written barrier description and a 3 to 5 minute video demo.

---

## Technical challenges I'd like feedback on

### 1. Code fragility in care gap rules

The care gap rules use specific SNOMED, LOINC, and RxNorm codes to identify conditions, labs, and procedures. My first set of rules used textbook-correct codes that turned out to be completely wrong for Synthea's data. Hypertension was coded as 59621000 in Synthea, not 38341003 which is what clinical references say. Diabetic eye exams used 722161008 instead of the three codes I tried. About 40% of my initial gap detections were false because the rules were querying codes with zero rows. I fixed them, but in real-world data you'd hit this problem even harder because different EHR systems code the same condition differently, and some use free text instead of structured codes at all.

**Question:** How robust does the rule engine need to be, and is there a smarter approach than hardcoding code sets?

### 2. LLM hallucination and verification depth

The system prompt tells the model to only use provided data, and I built a verification script that checks claims against source records after generation. But the verification is fairly shallow right now. It checks whether a mentioned condition exists in the patient's record and whether drug interaction alerts match what the detection engine found. It doesn't catch subtler problems like the LLM saying "HbA1c trending upward" when there's only one value on record, or overstating severity.

**Question:** What would a stronger verification layer look like?

### 3. Drug name matching on real-world data

The drug interaction checker matched 125 out of 126 medications, which sounds great on synthetic data. But Synthea uses clean, standardized RxNorm display names. Real-world medication entries include typos, abbreviations, brand names, compound formulations, and free-text entries like "patient's own insulin." My name extraction pipeline strips dosage and formulation tokens to find the generic ingredient, but it hasn't been tested on messy real-world strings.

**Question:** How much would the match rate drop on real data, and what's the right approach for handling non-standard medication entries?

### 4. Synthetic-to-real domain gap

The entire pipeline runs on synthetic data. Synthea produces well-structured, standards-conformant FHIR records. Real clinical data has missing fields, inconsistent coding, free-text notes instead of structured entries, and records that mix multiple coding systems. My care gap rules and drug interaction matching would probably perform worse on real data, and I have no way to quantify how much worse without actually testing on real records.

### 5. Noisy "conditions" in the data

Some Synthea "conditions" aren't medical conditions at all. My top demo patient has "Received higher education (finding)" and "Not in labor force (finding)" listed as active conditions. These are social observations coded as SNOMED findings, and the LLM includes them in the clinical summary. I need to either filter these out or teach the prompt to handle them differently.

### 6. Completeness assumptions

The care gap rules assume the patient's record is complete. If a screening happened but wasn't captured in the data, the system flags a false gap. In the real world this would be common, especially for FQHC patients whose records are fragmented across systems. The tool would need to be transparent about this, saying "based on available records" rather than "this screening is overdue."

---

## Where would real-world data come from?

This is the hardest part of making CareThread a real product, and I don't have a clean solution yet.

### Path 1: Health Information Exchanges (HIEs)

Most states run an HIE that aggregates clinical data across providers. Maine has HealthInfoNet, which is one of the more mature state HIEs in the country and covers a large share of Maine providers. In theory, an FQHC could pull consolidated patient records from HealthInfoNet before each visit. In practice, HIE data access requires formal data sharing agreements, the data is often incomplete, and the feeds aren't always real-time.

### Path 2: Direct EHR integration

FQHCs commonly use eClinicalWorks, NextGen, athenahealth, or Epic Community Connect. Each has APIs (most now support FHIR R4 endpoints as required by the 21st Century Cures Act), but integrating with even one EHR vendor takes months of certification, security review, and compliance work. Getting data from the ER the patient visited last week, which runs a different EHR, adds another integration.

### Realistic first step

Partner with one FQHC organization that uses one EHR, pull data through that EHR's FHIR API, and prove value on single-system data before tackling cross-system aggregation. The record linkage component (which I'm omitting from the demo) becomes essential at the point where you're pulling from multiple systems.

### The investor question

The data access problem is real but it's a business problem, not a technical one. The Cures Act mandates FHIR API access. HIEs exist and are maturing. The question is whether the tool provides enough value on single-system data to get an initial contract, then expands to cross-system data as the partnerships develop.
