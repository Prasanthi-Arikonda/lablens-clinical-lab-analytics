"""
etl_load.py  -  Module 4, step 3: LOAD into PostgreSQL (staging + star schema)

  * load_staging()      replaces a staging table with this batch's raw rows
  * load_dim_date()     builds the calendar dimension
  * upsert_* functions  INSERT ... ON CONFLICT DO UPDATE  ("upsert")
                        -> new rows are added, changed rows are updated,
                           and running the pipeline twice never creates duplicates
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd


def _clean(value):
    """Convert pandas / numpy values into plain Python values PostgreSQL understands."""
    if value is None:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    if value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if np.isnan(value) else float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _rows(df, columns):
    return [tuple(_clean(v) for v in row) for row in df[columns].itertuples(index=False, name=None)]


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------
def load_staging(conn, table, df, batch_id):
    """Empty the staging table and copy this batch's raw rows into it (fast COPY)."""
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE staging.{table}")
        if df.empty:
            return 0
        columns = list(df.columns) + ["etl_batch_id"]
        data = df.copy()
        data["etl_batch_id"] = batch_id
        data = data.astype(object)
        with cur.copy(f"COPY staging.{table} ({', '.join(columns)}) FROM STDIN") as copy:
            for row in _rows(data, columns):
                copy.write_row([None if v is None else str(v) for v in row])
    return len(df)


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------
def load_dim_date(conn, start=date(2025, 12, 1), end=date(2026, 12, 31)):
    rows = []
    d = start
    while d <= end:
        rows.append((int(d.strftime("%Y%m%d")), d, d.year, (d.month - 1) // 3 + 1, d.month,
                     d.strftime("%B"), d.strftime("%Y-%m"), d.day, d.isoweekday(),
                     d.strftime("%A"), d.isoweekday() >= 6))
        d += timedelta(days=1)
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO dw.dim_date (date_key, full_date, year, quarter, month, month_name, year_month,
                                     day_of_month, day_of_week, day_name, is_weekend)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (date_key) DO NOTHING""", rows)
        cur.executemany("""
            INSERT INTO dw.dim_shift (shift_key, shift_name, start_hour, end_hour)
            VALUES (%s,%s,%s,%s) ON CONFLICT (shift_key) DO NOTHING""",
                        [(1, "Day", 7, 15), (2, "Evening", 15, 23), (3, "Night", 23, 7)])
    return len(rows)


def upsert_dimensions(conn, dims, batch_id, age_group_fn):
    p = dims["patients"].copy()
    p["age_group"] = p["birth_year"].apply(age_group_fn)
    p["etl_batch_id"] = batch_id
    t = dims["tests"].copy()
    t["etl_batch_id"] = batch_id
    i = dims["instruments"].copy()
    i["etl_batch_id"] = batch_id
    d = dims["departments"].copy()
    d["etl_batch_id"] = batch_id
    ph = dims["physicians"].copy()
    ph["etl_batch_id"] = batch_id

    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO dw.dim_patient (patient_mrn, birth_year, age_group, sex, zip3, insurance_type, etl_batch_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (patient_mrn) DO UPDATE SET
              birth_year = EXCLUDED.birth_year, age_group = EXCLUDED.age_group, sex = EXCLUDED.sex,
              zip3 = EXCLUDED.zip3, insurance_type = EXCLUDED.insurance_type,
              etl_batch_id = EXCLUDED.etl_batch_id, loaded_at = now()""",
                        _rows(p, ["patient_mrn", "birth_year", "age_group", "sex", "zip3",
                                  "insurance_type", "etl_batch_id"]))
        cur.executemany("""
            INSERT INTO dw.dim_test (test_code, test_name, category, specimen_type, unit, ref_low, ref_high,
                                     critical_low, critical_high, tat_target_min, list_price_usd, etl_batch_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (test_code) DO UPDATE SET
              test_name = EXCLUDED.test_name, category = EXCLUDED.category, unit = EXCLUDED.unit,
              ref_low = EXCLUDED.ref_low, ref_high = EXCLUDED.ref_high,
              critical_low = EXCLUDED.critical_low, critical_high = EXCLUDED.critical_high,
              tat_target_min = EXCLUDED.tat_target_min, list_price_usd = EXCLUDED.list_price_usd,
              etl_batch_id = EXCLUDED.etl_batch_id""",
                        _rows(t, ["test_code", "test_name", "category", "specimen_type", "unit", "ref_low",
                                  "ref_high", "critical_low", "critical_high", "tat_target_min",
                                  "price_usd", "etl_batch_id"]))
        cur.executemany("""
            INSERT INTO dw.dim_instrument (instrument_code, instrument_name, manufacturer, category,
                                           install_date, etl_batch_id)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (instrument_code) DO UPDATE SET
              instrument_name = EXCLUDED.instrument_name, manufacturer = EXCLUDED.manufacturer,
              category = EXCLUDED.category, install_date = EXCLUDED.install_date,
              etl_batch_id = EXCLUDED.etl_batch_id""",
                        _rows(i, ["instrument_code", "instrument_name", "manufacturer", "category",
                                  "install_date", "etl_batch_id"]))
        cur.executemany("""
            INSERT INTO dw.dim_department (department_code, department_name, location, etl_batch_id)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (department_code) DO UPDATE SET
              department_name = EXCLUDED.department_name, location = EXCLUDED.location,
              etl_batch_id = EXCLUDED.etl_batch_id""",
                        _rows(d, ["department_code", "department_name", "location", "etl_batch_id"]))
        cur.executemany("""
            INSERT INTO dw.dim_physician (physician_code, physician_name, specialty, department_code, etl_batch_id)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (physician_code) DO UPDATE SET
              physician_name = EXCLUDED.physician_name, specialty = EXCLUDED.specialty,
              department_code = EXCLUDED.department_code, etl_batch_id = EXCLUDED.etl_batch_id""",
                        _rows(ph, ["physician_code", "physician_name", "specialty", "department_code",
                                   "etl_batch_id"]))
    return len(p) + len(t) + len(i) + len(d) + len(ph)


def key_map(conn, table, natural_key, surrogate_key):
    """Return {natural key: surrogate key} for a dimension, e.g. {'GLU': 1, ...}."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {natural_key}, {surrogate_key} FROM dw.{table}")
        return dict(cur.fetchall())


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------
FACT_COLUMNS = [
    "order_test_id", "order_number", "sample_barcode", "order_date_key", "patient_key", "test_key",
    "instrument_key", "department_key", "physician_key", "shift_key", "priority", "order_status",
    "test_status", "sample_status", "rejection_reason", "order_datetime", "collected_at", "received_at",
    "resulted_at", "collect_to_receive_min", "tat_min", "total_tat_min", "tat_target_min", "tat_met",
    "result_value", "result_flag", "is_abnormal", "is_critical", "reimbursement_usd",
    "transport_method", "transport_temp_c", "source_updated_at", "etl_batch_id",
]


def upsert_lab_test_facts(conn, facts, batch_id):
    if facts.empty:
        return 0
    df = facts.copy()
    df["patient_key"] = df["patient_mrn"].map(key_map(conn, "dim_patient", "patient_mrn", "patient_key"))
    df["test_key"] = df["test_code"].map(key_map(conn, "dim_test", "test_code", "test_key"))
    df["instrument_key"] = df["instrument_code"].map(
        key_map(conn, "dim_instrument", "instrument_code", "instrument_key"))
    df["department_key"] = df["department_code"].map(
        key_map(conn, "dim_department", "department_code", "department_key"))
    df["physician_key"] = df["physician_code"].map(
        key_map(conn, "dim_physician", "physician_code", "physician_key"))
    df["etl_batch_id"] = batch_id

    placeholders = ", ".join(["%s"] * len(FACT_COLUMNS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in FACT_COLUMNS if c != "order_test_id")
    with conn.cursor() as cur:
        cur.executemany(f"""
            INSERT INTO dw.fact_lab_test ({', '.join(FACT_COLUMNS)}) VALUES ({placeholders})
            ON CONFLICT (order_test_id) DO UPDATE SET {updates}, loaded_at = now()""",
                        _rows(df.astype(object), FACT_COLUMNS))
    return len(df)


def upsert_instrument_events(conn, events, batch_id):
    if events.empty:
        return 0
    df = events.copy()
    df["instrument_key"] = df["instrument_code"].map(
        key_map(conn, "dim_instrument", "instrument_code", "instrument_key"))
    df["etl_batch_id"] = batch_id
    cols = ["mongo_id", "instrument_key", "date_key", "event_time", "event_type", "severity", "error_code",
            "error_message", "downtime_min", "qc_passed", "qc_rule", "etl_batch_id"]
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO dw.fact_instrument_event (mongo_id, instrument_key, date_key, event_time, event_type,
                   severity, error_code, error_message, downtime_min, qc_passed, qc_rule_violated, etl_batch_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (mongo_id) DO UPDATE SET
              severity = EXCLUDED.severity, downtime_min = EXCLUDED.downtime_min,
              qc_passed = EXCLUDED.qc_passed, etl_batch_id = EXCLUDED.etl_batch_id""",
                        _rows(df.astype(object), cols))
    return len(df)


def replace_fee_schedule(conn, fees, batch_id):
    df = fees.copy()
    df["etl_batch_id"] = batch_id
    with conn.cursor() as cur:
        cur.execute("DELETE FROM dw.ref_fee_schedule")
        cur.executemany("""
            INSERT INTO dw.ref_fee_schedule (test_code, payer_type, allowed_amount_usd, effective_date, etl_batch_id)
            VALUES (%s,%s,%s,%s,%s)""",
                        _rows(df.astype(object), ["test_code", "payer_type", "allowed_amount_usd",
                                                  "effective_date", "etl_batch_id"]))
    return len(df)


def upsert_staffing(conn, staffing, batch_id):
    df = staffing.copy()
    df["etl_batch_id"] = batch_id
    cols = ["date_key", "shift_key", "techs_scheduled", "techs_present", "call_outs", "overtime_hours",
            "staffing_pct", "etl_batch_id"]
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO dw.fact_daily_staffing (date_key, shift_key, techs_scheduled, techs_present, call_outs,
                                                overtime_hours, staffing_pct, etl_batch_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (date_key, shift_key) DO UPDATE SET
              techs_scheduled = EXCLUDED.techs_scheduled, techs_present = EXCLUDED.techs_present,
              call_outs = EXCLUDED.call_outs, overtime_hours = EXCLUDED.overtime_hours,
              staffing_pct = EXCLUDED.staffing_pct, etl_batch_id = EXCLUDED.etl_batch_id""",
                        _rows(df.astype(object), cols))
    return len(df)
