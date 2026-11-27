"""
etl_extract.py  -  Module 4, step 1: EXTRACT data from every source

Each function reads ONE source and returns a pandas DataFrame, without changing
the values (cleaning happens in etl_transform.py).

Sources:
  MySQL    clinical_lab_ops   -> lab tests (incremental), patients, tests, instruments,
                                 departments, physicians
  MongoDB  clinical_lab_docs  -> sample_tracking, equipment_logs (incremental)
  Excel    data/external/payer_fee_schedule.xlsx
  CSV      data/external/lab_staffing_roster.csv
"""
import hashlib

import pandas as pd

from connectors import PROJECT_ROOT, get_mongo_db, mysql_query

EXTERNAL_DIR = PROJECT_ROOT / "data" / "external"
FEE_FILE = EXTERNAL_DIR / "payer_fee_schedule.xlsx"
STAFFING_FILE = EXTERNAL_DIR / "lab_staffing_roster.csv"


def _to_df(columns, rows):
    return pd.DataFrame(rows, columns=columns)


# ---------------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------------
def extract_mysql_lab_tests(since=None):
    """One row per ordered test (order + sample + test + result), flattened.

    Incremental: only rows changed AFTER `since` (the watermark = the newest change
    timestamp loaded by the previous successful run).
    A row counts as changed if the order, the order test, the sample OR the
    result was updated (GREATEST of the four updated_at columns).
    """
    sql = """
        SELECT ot.order_test_id, o.order_number, o.order_datetime, o.priority, o.order_status,
               p.patient_mrn, ph.physician_code, d.department_code,
               tc.test_code, i.instrument_code, ot.test_status,
               s.sample_barcode, s.sample_status, s.rejection_reason,
               s.collected_at, s.received_at,
               r.result_value, r.result_flag, r.resulted_at,
               GREATEST(o.updated_at, ot.updated_at,
                        COALESCE(s.updated_at, o.updated_at),
                        COALESCE(r.updated_at, o.updated_at)) AS source_updated_at
          FROM order_tests ot
          JOIN lab_orders   o  ON o.order_id = ot.order_id
          JOIN patients     p  ON p.patient_id = o.patient_id
          JOIN physicians   ph ON ph.physician_id = o.physician_id
          JOIN departments  d  ON d.department_id = o.department_id
          JOIN test_catalog tc ON tc.test_id = ot.test_id
          LEFT JOIN instruments  i ON i.instrument_id = ot.instrument_id
          LEFT JOIN samples      s ON s.sample_id = ot.sample_id
          LEFT JOIN test_results r ON r.order_test_id = ot.order_test_id
    """
    params = ()
    if since:
        sql += """
         WHERE GREATEST(o.updated_at, ot.updated_at,
                        COALESCE(s.updated_at, o.updated_at),
                        COALESCE(r.updated_at, o.updated_at)) > %s"""
        params = (since,)
    return _to_df(*mysql_query(sql, params))


def extract_mysql_dimensions():
    """Reference / master data for the dimension tables (small, read in full)."""
    return {
        "patients": _to_df(*mysql_query(
            "SELECT patient_mrn, birth_year, sex, zip3, insurance_type FROM patients")),
        "tests": _to_df(*mysql_query(
            """SELECT test_code, test_name, category, specimen_type, unit, ref_low, ref_high,
                      critical_low, critical_high, tat_target_min, price_usd FROM test_catalog""")),
        "instruments": _to_df(*mysql_query(
            """SELECT instrument_code, instrument_name, manufacturer, category, install_date
                 FROM instruments""")),
        "departments": _to_df(*mysql_query(
            "SELECT department_code, department_name, location FROM departments")),
        "physicians": _to_df(*mysql_query(
            """SELECT ph.physician_code, CONCAT(ph.first_name, ' ', ph.last_name) AS physician_name,
                      ph.specialty, d.department_code
                 FROM physicians ph JOIN departments d ON d.department_id = ph.department_id""")),
    }


def extract_mysql_barcodes():
    """All sample barcodes in MySQL (used to reconcile MongoDB documents)."""
    return {row[0] for row in mysql_query("SELECT sample_barcode FROM samples")[1]}


def mongo_event_count():
    """Total equipment log documents in MongoDB (for reconciliation)."""
    return get_mongo_db()["equipment_logs"].count_documents({})


def mysql_source_count():
    """Total order tests in MySQL (used for source-to-target reconciliation)."""
    return mysql_query("SELECT COUNT(*) FROM order_tests")[1][0][0]


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------
def extract_mongo_sample_tracking():
    """Flatten sample_tracking documents: barcode, order, transport info."""
    rows = []
    for doc in get_mongo_db()["sample_tracking"].find({}):
        transport = doc.get("transport") or {}
        rows.append({
            "mongo_id": str(doc["_id"]),
            "sample_barcode_raw": doc.get("sample_barcode"),
            "order_number": doc.get("order_number"),
            "transport_method": transport.get("method"),
            "transport_minutes": transport.get("duration_min"),
            "temperature_raw": transport.get("temperature_c"),
            "custody_steps": len(doc.get("chain_of_custody", [])),
        })
    return pd.DataFrame(rows)


def extract_mongo_equipment_logs(since=None):
    """Instrument events. Incremental on event_time (the watermark)."""
    query = {"event_time": {"$gt": since}} if since else {}
    rows = []
    for doc in get_mongo_db()["equipment_logs"].find(query):
        error = doc.get("error") or {}
        qc = doc.get("qc") or {}
        rows.append({
            "mongo_id": str(doc["_id"]),
            "instrument_code": doc.get("instrument_code"),
            "event_type": doc.get("event_type"),
            "event_time": doc.get("event_time"),
            "severity": doc.get("severity"),
            "error_code": error.get("code"),
            "error_message": error.get("message"),
            "downtime_min": error.get("downtime_min"),
            "qc_passed": qc.get("passed") if qc else None,
            "qc_rule": qc.get("rule_violated") if qc else None,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# External files
# ---------------------------------------------------------------------------
def file_checksum(path):
    """MD5 fingerprint of a file - if it has not changed, the file is not reloaded."""
    return hashlib.md5(path.read_bytes()).hexdigest()


def extract_fee_schedule():
    """Read the Excel fee schedule exactly as received (everything as text)."""
    # keep_default_na=False: otherwise pandas turns the test code "NA" (Sodium) into a missing value!
    df = pd.read_excel(FEE_FILE, dtype=str, keep_default_na=False, na_values=[""])
    return df.rename(columns={
        "Test Code": "test_code_raw", "Test Description": "test_description",
        "Payer Type": "payer_type_raw", "Allowed Amount": "allowed_amount_raw",
        "Effective Date": "effective_date_raw"})


def extract_staffing_roster():
    """Read the CSV staffing roster exactly as received (everything as text)."""
    df = pd.read_csv(STAFFING_FILE, dtype=str, keep_default_na=False, na_values=[""])
    return df.rename(columns={"date": "date_raw", "shift": "shift_raw"})
