"""
mongo_load_data.py  -  Module 2: build and fill the MongoDB document database

Creates the MongoDB database  clinical_lab_docs  with 4 collections:

  1. sample_tracking   - one document per sample: chain-of-custody steps (array of
                         embedded documents) + transport details (nested document).
                         References MySQL by sample_barcode and order_number.
  2. equipment_logs    - analyzer events: daily QC runs, errors, maintenance, calibration.
  3. incident_reports  - free-text incident reports written by lab staff (used for RAG).
  4. lab_documents     - lab SOPs and policies, split into sections (used for RAG).

The sample documents are built FROM the MySQL samples, so both systems describe the
same lab. A few realistic data-quality problems are added on purpose (messy barcodes,
duplicates, temperatures typed as text, missing fields) so the ETL pipeline in
Module 4 has real cleaning work to do.

Safe to run again: it deletes and rebuilds the collections each time.

How to run (from the project folder, with .venv switched on):
    python src/mongo_load_data.py
"""
import random
from datetime import datetime, timedelta

from connectors import get_mongo_db, mysql_query

random.seed(42)   # same "random" data every run -> reproducible

START = datetime(2026, 1, 1)
END = datetime(2026, 10, 31, 23, 59)
CHEM02_PROBLEM_START = datetime(2026, 7, 1)
CHEM02_PROBLEM_END = datetime(2026, 9, 30, 23, 59)


def in_chem02_problem(instrument, when):
    """True if this is analyzer CHEM-02 during its Q3 2026 problem period."""
    return instrument == "CHEM-02" and CHEM02_PROBLEM_START <= when <= CHEM02_PROBLEM_END


# ---------------------------------------------------------------------------
# 1. sample_tracking
# ---------------------------------------------------------------------------
def build_sample_tracking():
    cols, rows = mysql_query("""
        SELECT s.sample_barcode, MAX(s.specimen_type), MAX(s.collected_at), MAX(s.received_at),
               MAX(s.sample_status), MAX(s.rejection_reason), MAX(o.order_number),
               MAX(o.priority), MAX(d.department_code), MAX(d.location),
               GROUP_CONCAT(DISTINCT i.instrument_code), MAX(r.resulted_at)
          FROM samples s
          JOIN lab_orders  o ON o.order_id = s.order_id
          JOIN departments d ON d.department_id = o.department_id
          LEFT JOIN order_tests  ot ON ot.sample_id = s.sample_id
          LEFT JOIN instruments  i  ON i.instrument_id = ot.instrument_id
          LEFT JOIN test_results r  ON r.order_test_id = ot.order_test_id
         GROUP BY s.sample_id, s.sample_barcode
    """)
    docs = []
    for (barcode, specimen, collected, received, status, reason, order_no,
         priority, dept_code, location, instruments, resulted) in rows:
        method = "Pneumatic tube" if dept_code in ("ED", "ICU", "SURG") and random.random() < 0.8 \
            else random.choice(["Courier", "Hand delivery", "Pneumatic tube"])
        transit_min = int((received - collected).total_seconds() // 60)
        instrument_list = instruments.split(",") if instruments else []

        custody = [
            {"step": "COLLECTED", "location": location, "at": collected,
             "by": f"NURSE{random.randint(1, 30):02d}"},
            {"step": "IN_TRANSIT", "location": method, "at": collected + timedelta(minutes=2),
             "by": "TRANSPORT"},
            {"step": "RECEIVED", "location": "Core Lab - Accessioning", "at": received,
             "by": f"TECH{random.randint(1, 15):02d}"},
        ]
        if status == "REJECTED":
            custody.append({"step": "REJECTED", "location": "Core Lab - Accessioning",
                            "at": received + timedelta(minutes=5), "by": "TECH01",
                            "reason": reason})
        elif resulted is not None:
            custody.append({"step": "ANALYZED", "location": "Core Lab",
                            "instruments": instrument_list, "at": resulted, "by": "AUTOVERIFY"})

        temperature = round(random.uniform(18, 25), 1) if specimen != "Urine" or random.random() < 0.5 \
            else round(random.uniform(2, 8), 1)

        doc = {
            "sample_barcode": barcode,
            "order_number": order_no,                 # reference to MySQL lab_orders
            "specimen_type": specimen,
            "priority": priority,
            "ordering_department": dept_code,
            "current_status": status,
            "chain_of_custody": custody,              # array of embedded documents
            "transport": {                            # nested (embedded) document
                "method": method,
                "duration_min": transit_min,
                "temperature_c": temperature,
            },
            "tags": [priority.lower(), specimen.lower().replace(" ", "_")],
            "last_updated": received,
        }

        # ---- deliberate data-quality problems (for the ETL cleaning rules) ----
        r = random.random()
        if r < 0.02:
            doc["sample_barcode"] = " " + barcode.lower() + " "      # messy barcode format
        elif r < 0.04:
            doc["transport"]["temperature_c"] = f"{temperature} C"   # number typed as text
        elif r < 0.05:
            del doc["transport"]                                     # missing information
        docs.append(doc)

    # ~1% exact duplicate documents (the same scan sent twice)
    duplicates = [dict(d) for d in random.sample(docs, k=len(docs) // 100)]
    return docs + duplicates


# ---------------------------------------------------------------------------
# 2. equipment_logs
# ---------------------------------------------------------------------------
QC_ANALYTES = {
    "Chemistry": ["Glucose", "Sodium", "Potassium", "Creatinine"],
    "Hematology": ["Hemoglobin", "WBC", "Platelets"],
    "Coagulation": ["PT", "INR"],
    "Immunoassay": ["Troponin I", "TSH"],
    "Urinalysis": ["pH", "Specific gravity"],
}

ERROR_TYPES = {
    "Chemistry": [("E-417", "Reagent probe clog detected"),
                  ("E-233", "Calibration drift above tolerance"),
                  ("E-109", "Cuvette temperature out of range"),
                  ("E-512", "Sample short - insufficient volume aspirated")],
    "Hematology": [("H-210", "Flow cell bubble detected"), ("H-305", "Background count high")],
    "Coagulation": [("C-118", "Clot detection timeout"), ("C-402", "Reagent lot expired")],
    "Immunoassay": [("I-220", "Wash station pressure low"), ("I-317", "Substrate level low")],
    "Urinalysis": [("U-101", "Strip feeder jam"), ("U-207", "Camera focus error")],
}


def build_equipment_logs():
    cols, instruments = mysql_query(
        "SELECT instrument_code, category, manufacturer FROM instruments ORDER BY instrument_code")
    docs = []
    day = START
    while day <= END:
        for code, category, manufacturer in instruments:
            # --- daily QC run (2 control levels, several analytes) ---
            qc_time = day + timedelta(hours=6, minutes=random.randint(0, 50))
            fail_prob = 0.08 if in_chem02_problem(code, qc_time) else 0.007
            levels = []
            for level in ("L1", "L2"):
                for analyte in QC_ANALYTES[category]:
                    within = random.random() > fail_prob
                    levels.append({"level": level, "analyte": analyte,
                                   "z_score": round(random.gauss(0, 0.9) if within
                                                    else random.choice([-1, 1]) * random.uniform(2.1, 3.6), 2),
                                   "within_limits": within})
            passed = all(l["within_limits"] for l in levels)
            docs.append({
                "instrument_code": code, "manufacturer": manufacturer, "category": category,
                "event_type": "QC_RUN", "event_time": qc_time,
                "severity": "INFO" if passed else "WARNING",
                "operator": f"TECH{random.randint(1, 15):02d}",
                "qc": {"passed": passed, "rule_violated": None if passed else random.choice(["1-3s", "2-2s", "R-4s"]),
                       "results": levels},
            })

            # --- errors (much more frequent for CHEM-02 in Q3) ---
            err_prob = 0.55 if in_chem02_problem(code, day) else 0.04
            while random.random() < err_prob:
                err_time = day + timedelta(minutes=random.randint(360, 1430))
                if in_chem02_problem(code, err_time):
                    err_code, msg = random.choice(ERROR_TYPES["Chemistry"][:2])
                    downtime = random.randint(20, 120)
                else:
                    err_code, msg = random.choice(ERROR_TYPES[category])
                    downtime = random.randint(0, 25)
                docs.append({
                    "instrument_code": code, "manufacturer": manufacturer, "category": category,
                    "event_type": "ERROR", "event_time": err_time,
                    "severity": "CRITICAL" if downtime > 60 else "ERROR",
                    "operator": f"TECH{random.randint(1, 15):02d}",
                    "error": {"code": err_code, "message": msg, "downtime_min": downtime},
                })
                err_prob *= 0.5

        # --- weekly preventive maintenance (Mondays) ---
        if day.weekday() == 0:
            for code, category, manufacturer in instruments:
                docs.append({
                    "instrument_code": code, "manufacturer": manufacturer, "category": category,
                    "event_type": "MAINTENANCE", "event_time": day + timedelta(hours=5),
                    "severity": "INFO", "operator": "BIOMED",
                    "maintenance": {"type": "Preventive", "tasks": ["Clean probes", "Check reagent levels",
                                                                    "Run system check"], "duration_min": 45},
                })
        day += timedelta(days=1)

    # --- the vendor repair that fixed CHEM-02 at the end of September ---
    docs.append({
        "instrument_code": "CHEM-02", "manufacturer": "Roche", "category": "Chemistry",
        "event_type": "MAINTENANCE", "event_time": datetime(2026, 9, 30, 14, 0),
        "severity": "INFO", "operator": "VENDOR-ROCHE",
        "maintenance": {"type": "Corrective", "duration_min": 240,
                        "tasks": ["Replaced reagent probe assembly", "Replaced worn pump tubing",
                                  "Full recalibration", "QC verified on all levels"]},
    })
    return docs


# ---------------------------------------------------------------------------
# 3. incident_reports  (free text for RAG)
# ---------------------------------------------------------------------------
def build_incident_reports():
    reports = []
    dept_codes = ["ED", "ICU", "IM", "CARD", "ONC", "PED", "OPC", "SURG"]

    def add(when, category, severity, description, action, instrument=None, dept=None):
        reports.append({
            "incident_id": f"INC-{len(reports) + 1:04d}",
            "reported_at": when, "category": category, "severity": severity,
            "department": dept or random.choice(dept_codes),
            "instrument_code": instrument,
            "description": description, "corrective_action": action,
            "status": "CLOSED" if when < datetime(2026, 10, 15) else "OPEN",
        })

    # CHEM-02 problem reports during Q3
    chem02_texts = [
        ("Delayed result", "Chemistry results from CHEM-02 were delayed by over two hours on the evening shift. "
         "The analyzer stopped twice with error E-417 (reagent probe clog) and samples had to be re-run.",
         "Probe cleaned and samples rerouted to CHEM-01. Vendor service ticket opened."),
        ("Instrument downtime", "CHEM-02 failed morning QC on the potassium and creatinine controls (2-2s rule). "
         "Patient testing on CHEM-02 was held until recalibration passed.",
         "Recalibrated and repeated QC. Patient samples held for about 90 minutes were then released."),
        ("Delayed result", "Emergency department called about a STAT basic metabolic panel taking longer than "
         "the 60 minute target. Root cause was a backlog on CHEM-02 after repeated calibration drift alarms (E-233).",
         "STAT samples were manually prioritised on CHEM-01 for the rest of the shift."),
        ("Instrument downtime", "CHEM-02 showed repeated E-417 probe clog alarms. Technologists noticed the sample "
         "probe tubing was worn. Analyzer was taken offline for cleaning three times this week.",
         "Escalated to the vendor. Workload split between CHEM-01 and CHEM-02 until the repair."),
    ]
    day = CHEM02_PROBLEM_START
    while day <= CHEM02_PROBLEM_END:
        if random.random() < 0.45:
            cat, desc, act = random.choice(chem02_texts)
            add(day + timedelta(hours=random.randint(7, 22)), cat,
                random.choice(["Medium", "High"]), desc, act, instrument="CHEM-02")
        day += timedelta(days=1)
    add(datetime(2026, 9, 30, 18, 0), "Instrument downtime", "Medium",
        "Vendor engineer replaced the reagent probe assembly and pump tubing on CHEM-02. "
        "Full recalibration completed and QC passed on all control levels.",
        "CHEM-02 returned to full service. Monitor turnaround time for two weeks.", instrument="CHEM-02",
        dept="IM")

    # Everyday incidents spread over the whole year
    general = [
        ("Sample rejection", "Low", "Specimen received hemolyzed from the {d} unit. Potassium could not be reported.",
         "Requested recollection. Reminded staff about gentle tube inversion and correct needle size."),
        ("Sample rejection", "Low", "Blood tube received without a patient label from the {d} unit.",
         "Sample discarded per labeling policy. Recollection requested and unit manager informed."),
        ("Labeling error", "Medium", "Name on the tube did not match the order for a sample from the {d} unit.",
         "Sample rejected as mislabeled. Incident shared with nursing education."),
        ("Delayed result", "Low", "Courier delivery from the outpatient clinic arrived 75 minutes late due to traffic.",
         "Clinic informed. Courier schedule reviewed with transport team."),
        ("Sample rejection", "Low", "Coagulation tube was under-filled (insufficient volume) from the {d} unit.",
         "Recollection requested. Reinforced fill-line requirement for light blue tubes."),
        ("Critical value", "Medium", "Critical potassium result could not be phoned to the {d} unit within 15 minutes "
         "because the ordering physician was unavailable.",
         "Result read back to the charge nurse. Escalation path in the critical value policy followed."),
        ("Instrument downtime", "Low", "Urinalysis strip feeder jammed (U-101) during the day shift.",
         "Feeder cleared and analyzer restarted within 15 minutes. No results affected."),
        ("Specimen transport", "Medium", "Pneumatic tube station in the {d} unit was out of service for half a day.",
         "Samples hand-delivered. Facilities repaired the station."),
    ]
    for _ in range(140):
        when = START + timedelta(minutes=random.randint(0, int((END - START).total_seconds() // 60)))
        cat, sev, desc, act = random.choice(general)
        dept = random.choice(dept_codes)
        add(when, cat, sev, desc.format(d=dept), act, dept=dept)

    reports.sort(key=lambda r: r["reported_at"])
    for i, r in enumerate(reports, start=1):
        r["incident_id"] = f"INC-{i:04d}"
    return reports


# ---------------------------------------------------------------------------
# 4. lab_documents  (SOPs and policies for RAG)
# ---------------------------------------------------------------------------
def build_lab_documents():
    def doc(doc_id, title, doc_type, version, effective, owner, tags, sections):
        return {"doc_id": doc_id, "title": title, "doc_type": doc_type, "version": version,
                "effective_date": effective, "owner": owner, "tags": tags,
                "sections": [{"heading": h, "text": t} for h, t in sections]}

    return [
        doc("POL-001", "Specimen Acceptance and Rejection Policy", "Policy", "3.2", datetime(2026, 1, 1),
            "Laboratory Quality Manager", ["specimen", "rejection", "labeling", "quality"], [
                ("Purpose", "This policy defines when the clinical laboratory accepts or rejects a specimen so that "
                 "only reliable samples are tested and patients are not harmed by wrong results."),
                ("Rejection criteria", "A specimen must be rejected when it is unlabeled or mislabeled, when the "
                 "patient name or medical record number on the tube does not match the order, when it is hemolyzed "
                 "for potassium or LDH testing, when it is clotted for hematology or coagulation testing, when the "
                 "volume is insufficient (for coagulation tubes, below the fill line), or when it is collected in "
                 "the wrong container."),
                ("Notification", "When a specimen is rejected the technologist must notify the ordering unit within "
                 "30 minutes, document the reason in the laboratory system, and request a recollection."),
                ("Mislabeled specimens", "Mislabeled specimens may never be relabeled by the laboratory. An incident "
                 "report must be filed for every mislabeled specimen."),
            ]),
        doc("POL-002", "Turnaround Time (TAT) Policy", "Policy", "2.0", datetime(2026, 1, 1),
            "Laboratory Director", ["turnaround time", "TAT", "STAT", "performance"], [
                ("Definition", "Turnaround time is measured from the moment the specimen is received in the "
                 "laboratory until the result is verified and released."),
                ("Targets", "STAT chemistry, hematology and coagulation tests must be resulted within 60 minutes of "
                 "receipt. Troponin I must be resulted within 60 minutes. Routine chemistry tests have a target of "
                 "60 to 120 minutes. Hemoglobin A1c and TSH have a target of 4 hours, and Vitamin D 8 hours."),
                ("Monitoring", "The laboratory reviews the percentage of tests meeting the TAT target every month. "
                 "If fewer than 90 percent of STAT tests meet the target, the laboratory manager must investigate "
                 "the cause, such as instrument downtime, staffing, or specimen transport delays."),
                ("Escalation", "When an analyzer is down for more than 30 minutes, STAT specimens must be moved to a "
                 "backup analyzer and the affected clinical units informed of possible delays."),
            ]),
        doc("POL-003", "Critical Value Notification Policy", "Policy", "4.1", datetime(2026, 2, 15),
            "Laboratory Medical Director", ["critical value", "notification", "patient safety"], [
                ("Purpose", "Critical values are results that may indicate a life-threatening condition and require "
                 "immediate action by the care team."),
                ("Critical limits", "Examples include glucose below 40 or above 450 mg/dL, potassium below 2.5 or above "
                 "6.5 mmol/L, sodium below 120 or above 160 mmol/L, hemoglobin below 7 g/dL, platelets below 50 K/uL, "
                 "INR above 5 and troponin I above 0.5 ng/mL."),
                ("Notification timeline", "The technologist must phone the critical result to the ordering physician or "
                 "the responsible nurse within 15 minutes of verification and ask the receiver to read the result back."),
                ("Escalation", "If the ordering physician cannot be reached within 15 minutes, the result is given to "
                 "the charge nurse, and if needed to the attending physician on call. Every call must be documented "
                 "with the name of the receiver and the time."),
            ]),
        doc("SOP-CHEM-01", "Chemistry Analyzer Operation and Maintenance", "SOP", "5.0", datetime(2025, 11, 1),
            "Chemistry Section Supervisor", ["chemistry", "analyzer", "maintenance", "CHEM-01", "CHEM-02"], [
                ("Scope", "This procedure applies to the chemistry analyzers CHEM-01 and CHEM-02 in the core laboratory."),
                ("Daily maintenance", "Before patient testing each morning, clean the sample and reagent probes, check "
                 "reagent levels, and run two levels of quality control. Patient testing may start only after QC passes."),
                ("Weekly maintenance", "Every Monday perform preventive maintenance: clean probes and wash stations, "
                 "inspect pump tubing for wear, and run the full system check."),
                ("Troubleshooting probe clogs", "Error E-417 indicates a reagent probe clog. Stop the run, clean the probe "
                 "with the cleaning solution, and rerun affected samples. If E-417 occurs more than three times in one "
                 "week, inspect the probe assembly and pump tubing and call the vendor, because worn tubing is a common "
                 "cause of repeated clogs."),
                ("Calibration drift", "Error E-233 indicates calibration drift above tolerance. Recalibrate the affected "
                 "assay and repeat QC before releasing patient results."),
            ]),
        doc("SOP-QC-01", "Quality Control and Westgard Rules", "SOP", "3.0", datetime(2025, 9, 1),
            "Laboratory Quality Manager", ["quality control", "QC", "Westgard"], [
                ("Purpose", "Quality control confirms that an analyzer produces accurate results before patient "
                 "samples are tested."),
                ("Control levels", "Two control levels (L1 normal and L2 abnormal) are run every 24 hours and after "
                 "every calibration, reagent lot change, or major maintenance."),
                ("Rules", "A run is rejected when one control is more than 3 standard deviations from the mean (1-3s), "
                 "when two consecutive controls exceed 2 standard deviations on the same side (2-2s), or when the "
                 "difference between two controls exceeds 4 standard deviations (R-4s)."),
                ("Action on QC failure", "When QC fails, hold all patient results from that analyzer, find and fix the "
                 "cause, repeat QC, and review patient results since the last passing QC."),
            ]),
        doc("SOP-TRN-01", "Specimen Transport and Temperature Requirements", "SOP", "2.3", datetime(2026, 3, 1),
            "Pre-analytical Supervisor", ["transport", "temperature", "courier", "pneumatic tube"], [
                ("Transport methods", "Specimens arrive by pneumatic tube, hospital courier or hand delivery. STAT "
                 "specimens from the emergency department, ICU and surgery should use the pneumatic tube."),
                ("Time limits", "Specimens should reach the laboratory within 60 minutes of collection. Specimens for "
                 "potassium and glucose that take longer than 2 hours may give falsely changed results."),
                ("Temperature", "Most blood specimens are transported at room temperature, 18 to 25 degrees Celsius. "
                 "Urine specimens that cannot be tested within 2 hours must be refrigerated at 2 to 8 degrees Celsius."),
            ]),
        doc("SOP-LBL-01", "Specimen Labeling Procedure", "SOP", "1.4", datetime(2025, 12, 1),
            "Nursing and Laboratory Education", ["labeling", "barcode", "patient identification"], [
                ("Two identifiers", "Every specimen must be labeled at the bedside with two patient identifiers: full "
                 "name and medical record number, plus the collection date and time and the collector's initials."),
                ("Barcode labels", "Use the printed barcode label from the order. The barcode format is SMP followed by "
                 "seven digits. Handwritten barcodes are not accepted."),
            ]),
        doc("SOP-DT-01", "Analyzer Downtime Procedure", "SOP", "2.1", datetime(2026, 4, 1),
            "Core Laboratory Manager", ["downtime", "backup analyzer", "business continuity"], [
                ("When to use", "Use this procedure when an analyzer is unavailable for more than 30 minutes because "
                 "of failure, QC rejection, or repair."),
                ("Steps", "Move STAT and pending specimens to the backup analyzer of the same category, inform the "
                 "emergency department and ICU about expected delays, log the downtime with start and end time, and "
                 "open a vendor service ticket if the cause is a hardware failure."),
                ("Recovery", "After repair, recalibrate, run QC on all control levels, and monitor turnaround time "
                 "for two weeks to confirm the problem is resolved."),
            ]),
    ]


# ---------------------------------------------------------------------------
def main():
    db = get_mongo_db()
    builders = {
        "sample_tracking": build_sample_tracking,
        "equipment_logs": build_equipment_logs,
        "incident_reports": build_incident_reports,
        "lab_documents": build_lab_documents,
    }
    print(f"Loading MongoDB database: {db.name}")
    total = 0
    for name, build in builders.items():
        docs = build()
        db[name].drop()                    # start fresh every run
        db[name].insert_many(docs)         # CREATE (the "C" in CRUD)
        count = db[name].count_documents({})
        total += count
        print(f"  {name:<18} {count:>6} documents")
    print(f"  {'TOTAL':<18} {total:>6} documents")
    print("MongoDB load finished successfully.")


if __name__ == "__main__":
    main()
