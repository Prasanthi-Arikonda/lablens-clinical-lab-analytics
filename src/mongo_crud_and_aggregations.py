"""
mongo_crud_and_aggregations.py  -  Module 2: CRUD, aggregation pipelines and indexes

Shows, on the clinical_lab_docs MongoDB database:
  A. CRUD with Python  (Create, Read, Update, Delete) on incident_reports
  B. Indexes           (what they are for, and proof that queries use them)
  C. 5 aggregation pipelines  (filter, group, sort, project, unwind)
  D. Extracting documents for downstream analytics / RAG (into a pandas table)

Run mongo_load_data.py first.

How to run (from the project folder, with .venv switched on):
    python src/mongo_crud_and_aggregations.py
"""
from datetime import datetime
from pprint import pprint

import pandas as pd

from connectors import get_mongo_db


def title(text):
    print("\n" + "=" * 78 + f"\n{text}\n" + "=" * 78)


def show(rows):
    """Print a list of documents as a small table."""
    print(pd.DataFrame(list(rows)).to_string(index=False))


# ---------------------------------------------------------------------------
# A. CRUD
# ---------------------------------------------------------------------------
def crud_demo(db):
    incidents = db["incident_reports"]

    title("A1. CREATE - insert a new incident report")
    new_report = {
        "incident_id": "INC-DEMO",
        "reported_at": datetime.now(),
        "category": "Sample rejection",
        "severity": "Low",
        "department": "ED",
        "instrument_code": None,
        "description": "Demo incident created from Python: clotted lavender tube from the ED.",
        "corrective_action": "Recollection requested.",
        "status": "OPEN",
    }
    result = incidents.insert_one(new_report)
    print("Inserted document _id:", result.inserted_id)

    title("A2. READ - find the document we just created")
    pprint(incidents.find_one({"incident_id": "INC-DEMO"}, {"_id": 0}))

    title("A3. UPDATE - close the incident")
    upd = incidents.update_one({"incident_id": "INC-DEMO"},
                               {"$set": {"status": "CLOSED", "closed_at": datetime.now()}})
    print("Documents modified:", upd.modified_count)
    print("Status now:", incidents.find_one({"incident_id": "INC-DEMO"})["status"])

    title("A4. DELETE - remove the demo incident")
    dele = incidents.delete_one({"incident_id": "INC-DEMO"})
    print("Documents deleted:", dele.deleted_count)

    title("A5. READ with a filter + projection + sort: last 5 CHEM-02 error events")
    cursor = (db["equipment_logs"]
              .find({"instrument_code": "CHEM-02", "event_type": "ERROR"},
                    {"_id": 0, "event_time": 1, "error.code": 1, "error.message": 1,
                     "error.downtime_min": 1})
              .sort("event_time", -1)
              .limit(5))
    for doc in cursor:
        print(doc["event_time"], "|", doc["error"]["code"], "|", doc["error"]["message"],
              "| downtime", doc["error"]["downtime_min"], "min")


# ---------------------------------------------------------------------------
# B. INDEXES
# ---------------------------------------------------------------------------
def index_demo(db):
    title("B. INDEXES - created to speed up the most common searches")
    # 1. Look up a sample by its barcode (used when joining with MySQL in the ETL)
    db["sample_tracking"].create_index("sample_barcode", name="idx_sample_barcode")
    # 2. Instrument history in time order (instrument + date range searches)
    db["equipment_logs"].create_index([("instrument_code", 1), ("event_time", -1)],
                                      name="idx_instrument_time")
    # 3. Filter logs by event type (ERROR / QC_RUN / MAINTENANCE)
    db["equipment_logs"].create_index("event_type", name="idx_event_type")
    # 4. Full-text search over incident descriptions (keyword search for RAG)
    db["incident_reports"].create_index([("description", "text"), ("corrective_action", "text")],
                                        name="idx_incident_text")
    # 5. Find documents by tag
    db["lab_documents"].create_index("tags", name="idx_doc_tags")

    for coll in ["sample_tracking", "equipment_logs", "incident_reports", "lab_documents"]:
        names = [ix["name"] for ix in db[coll].list_indexes()]
        print(f"  {coll:<18} indexes: {names}")

    print("\nProof that MongoDB uses the index (query plan of a barcode lookup):")
    try:
        plan = db["sample_tracking"].find({"sample_barcode": "SMP0000100"}).explain()

        def find_index_stage(node):
            """Search the query plan for the stage that reads the index."""
            if isinstance(node, dict):
                if node.get("stage") == "IXSCAN":
                    return node
                for value in node.values():
                    found = find_index_stage(value)
                    if found:
                        return found
            elif isinstance(node, list):
                for value in node:
                    found = find_index_stage(value)
                    if found:
                        return found
            return None

        ix = find_index_stage(plan["queryPlanner"]["winningPlan"])
        if ix:
            print(f"  plan stage = IXSCAN   index used = {ix.get('indexName')}")
        else:
            print("  plan stage = COLLSCAN (no index used)")
        print("  IXSCAN = index scan (fast, jumps straight to the matching document).")
        print("  Without the index MongoDB would do a COLLSCAN (read every document).")
    except Exception as exc:  # explain() is not available in every environment
        print("  (query plan not available:", exc, ")")


# ---------------------------------------------------------------------------
# C. AGGREGATION PIPELINES
# ---------------------------------------------------------------------------
def aggregation_demo(db):
    logs = db["equipment_logs"]

    title("C1. Error events and total downtime per instrument  ($match, $group, $sort)")
    show(logs.aggregate([
        {"$match": {"event_type": "ERROR"}},
        {"$group": {"_id": "$instrument_code",
                    "errors": {"$sum": 1},
                    "total_downtime_min": {"$sum": "$error.downtime_min"}}},
        {"$sort": {"errors": -1}},
        {"$project": {"_id": 0, "instrument": "$_id", "errors": 1, "total_downtime_min": 1}},
    ]))

    title("C2. CHEM-02 errors by month - the Q3 problem  ($match, $group on month, $sort)")
    show(logs.aggregate([
        {"$match": {"instrument_code": "CHEM-02", "event_type": "ERROR"}},
        {"$group": {"_id": {"$month": "$event_time"}, "errors": {"$sum": 1},
                    "downtime_min": {"$sum": "$error.downtime_min"}}},
        {"$sort": {"_id": 1}},
        {"$project": {"_id": 0, "month": "$_id", "errors": 1, "downtime_min": 1}},
    ]))

    title("C3. QC failure rate per instrument  ($match, $group, $project with a calculation)")
    show(logs.aggregate([
        {"$match": {"event_type": "QC_RUN"}},
        {"$group": {"_id": "$instrument_code",
                    "qc_runs": {"$sum": 1},
                    "qc_failed": {"$sum": {"$cond": ["$qc.passed", 0, 1]}}}},
        {"$project": {"_id": 0, "instrument": "$_id", "qc_runs": 1, "qc_failed": 1,
                      "fail_rate_pct": {"$round": [{"$multiply": [
                          {"$divide": ["$qc_failed", "$qc_runs"]}, 100]}, 1]}}},
        {"$sort": {"fail_rate_pct": -1}},
    ]))

    title("C4. Which QC analytes fail most often?  ($unwind an ARRAY, then $group)")
    show(logs.aggregate([
        {"$match": {"event_type": "QC_RUN", "qc.passed": False}},
        {"$unwind": "$qc.results"},
        {"$match": {"qc.results.within_limits": False}},
        {"$group": {"_id": {"instrument": "$instrument_code", "analyte": "$qc.results.analyte"},
                    "failures": {"$sum": 1}}},
        {"$sort": {"failures": -1}},
        {"$limit": 8},
        {"$project": {"_id": 0, "instrument": "$_id.instrument", "analyte": "$_id.analyte",
                      "failures": 1}},
    ]))

    title("C5. Sample transport: average minutes by method and priority  (nested fields)")
    show(db["sample_tracking"].aggregate([
        {"$match": {"transport.method": {"$exists": True}}},
        {"$group": {"_id": {"method": "$transport.method", "priority": "$priority"},
                    "samples": {"$sum": 1},
                    "avg_transport_min": {"$avg": "$transport.duration_min"}}},
        {"$project": {"_id": 0, "method": "$_id.method", "priority": "$_id.priority", "samples": 1,
                      "avg_transport_min": {"$round": ["$avg_transport_min", 1]}}},
        {"$sort": {"method": 1, "priority": 1}},
    ]))

    title("C6. Incident reports by category and severity")
    show(db["incident_reports"].aggregate([
        {"$group": {"_id": {"category": "$category", "severity": "$severity"}, "reports": {"$sum": 1}}},
        {"$sort": {"reports": -1}},
        {"$project": {"_id": 0, "category": "$_id.category", "severity": "$_id.severity", "reports": 1}},
    ]))


# ---------------------------------------------------------------------------
# D. EXTRACT for downstream analytics / RAG
# ---------------------------------------------------------------------------
def extract_demo(db):
    title("D. EXTRACT - pull lab document sections into a table (input for RAG later)")
    rows = []
    for d in db["lab_documents"].find({}, {"_id": 0}):
        for s in d["sections"]:
            rows.append({"doc_id": d["doc_id"], "title": d["title"],
                         "section": s["heading"], "chars": len(s["text"])})
    df = pd.DataFrame(rows)
    print(df.head(10).to_string(index=False))
    print(f"... {len(df)} sections from {df['doc_id'].nunique()} documents are ready for RAG.")


def main():
    db = get_mongo_db()
    crud_demo(db)
    index_demo(db)
    aggregation_demo(db)
    extract_demo(db)
    print("\nMongoDB CRUD / index / aggregation demo finished successfully.")


if __name__ == "__main__":
    main()
