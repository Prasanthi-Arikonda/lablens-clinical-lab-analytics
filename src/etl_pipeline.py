"""
etl_pipeline.py  -  Module 4: the complete ETL pipeline (run this file)

    MySQL + MongoDB + Excel + CSV  ->  Python (extract, clean, reconcile)  ->  PostgreSQL

How to run (from the project folder, with .venv switched on):
    python src/etl_pipeline.py          # INCREMENTAL: only what changed since the last run
    python src/etl_pipeline.py --full   # FULL: reload everything from scratch

What happens in one run (one "batch"):
  1. A new batch is registered in etl.run_log (status RUNNING).
  2. EXTRACT   from every source. MySQL and MongoDB logs are read incrementally using
               the WATERMARK (the last change timestamp that was loaded). The two external
               files are only reloaded if their checksum (fingerprint) changed.
  3. STAGING   raw rows are copied into the staging schema exactly as extracted.
  4. TRANSFORM data-quality rules DQ01-DQ08 clean and reconcile the data
               (see etl_transform.py). Every correction is saved in etl.dq_issue.
  5. LOAD      dimensions and facts are UPSERTED into the dw star schema.
  6. CHECK     source-to-target reconciliation counts are saved in etl.reconciliation.
  7. FINISH    watermarks move forward, the materialized view is refreshed,
               and the batch is marked SUCCESS.

Restart / recovery: all loading happens in ONE database transaction. If anything fails,
everything is rolled back, the batch is marked FAILED, and the watermarks do NOT move -
so simply running the pipeline again safely redoes the same work (upserts never duplicate).
"""
import argparse
import logging
import sys
import time
from datetime import datetime

import pandas as pd

import etl_extract as ex
import etl_load as ld
import etl_transform as tf
from connectors import PROJECT_ROOT, get_pg_connection

LOG_DIR = PROJECT_ROOT / "logs"


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"etl_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger("etl"), log_file


# ---------------------------------------------------------------------------
# ETL control tables
# ---------------------------------------------------------------------------
def start_batch(conn, run_type):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO etl.run_log (run_type) VALUES (%s) RETURNING batch_id", (run_type,))
        batch_id = cur.fetchone()[0]
    conn.commit()
    return batch_id


def read_watermarks(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT source_name, last_value FROM etl.watermark")
        return dict(cur.fetchall())


def save_watermark(conn, source, value):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO etl.watermark (source_name, last_value) VALUES (%s, %s)
                       ON CONFLICT (source_name) DO UPDATE SET last_value = EXCLUDED.last_value,
                                                              updated_at = now()""", (source, str(value)))


def save_issues(conn, batch_id, issues):
    with conn.cursor() as cur:
        cur.executemany("""INSERT INTO etl.dq_issue (batch_id, rule_code, source_name, record_ref, issue, action_taken)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        [(batch_id, i["rule_code"], i["source_name"], i["record_ref"], i["issue"],
                          i["action_taken"]) for i in issues])


def save_check(conn, batch_id, name, source_count, target_count):
    passed = source_count == target_count
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO etl.reconciliation (batch_id, check_name, source_count, target_count, passed)
                       VALUES (%s,%s,%s,%s,%s)""", (batch_id, name, source_count, target_count, passed))
    return passed


def count(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------
def run(full=False):
    log, log_file = setup_logging()
    started = time.time()
    conn = get_pg_connection()
    batch_id = None
    summary = {}
    try:
        wm = {} if full else read_watermarks(conn)
        run_type = "FULL" if (full or not wm) else "INCREMENTAL"
        batch_id = start_batch(conn, run_type)
        log.info("=" * 70)
        log.info(f"ETL batch {batch_id} started  ({run_type})")

        # ---------------- 1. EXTRACT ----------------
        mysql_since = wm.get("mysql.lab_tests")
        mongo_since = wm.get("mongodb.equipment_logs")
        log.info(f"EXTRACT  MySQL lab tests changed since: {mysql_since or 'the beginning'}")
        tests_raw = ex.extract_mysql_lab_tests(since=mysql_since)
        dims = ex.extract_mysql_dimensions()
        barcodes = ex.extract_mysql_barcodes()
        log.info(f"EXTRACT  MongoDB equipment logs since: {mongo_since or 'the beginning'}")
        tracking_raw = ex.extract_mongo_sample_tracking()
        events_raw = ex.extract_mongo_equipment_logs(
            since=datetime.fromisoformat(mongo_since) if mongo_since else None)

        fee_sum, staff_sum = ex.file_checksum(ex.FEE_FILE), ex.file_checksum(ex.STAFFING_FILE)
        fee_changed = full or wm.get("file.payer_fee_schedule") != fee_sum
        staff_changed = full or wm.get("file.lab_staffing_roster") != staff_sum
        fee_raw = ex.extract_fee_schedule()
        staff_raw = ex.extract_staffing_roster() if staff_changed else pd.DataFrame()

        summary["extracted"] = {
            "MySQL lab tests": len(tests_raw), "MongoDB sample docs": len(tracking_raw),
            "MongoDB equipment events": len(events_raw),
            "Excel fee rows": len(fee_raw) if fee_changed else "unchanged - skipped",
            "CSV staffing rows": len(staff_raw) if staff_changed else "unchanged - skipped"}
        for k, v in summary["extracted"].items():
            log.info(f"EXTRACT  {k:<26} {v}")

        # ---------------- 2. STAGING ----------------
        ld.load_staging(conn, "stg_mysql_lab_tests", tests_raw, batch_id)
        ld.load_staging(conn, "stg_mongo_sample_tracking", tracking_raw, batch_id)
        ld.load_staging(conn, "stg_mongo_equipment_logs", events_raw, batch_id)
        ld.load_staging(conn, "stg_fee_schedule", fee_raw, batch_id)
        if staff_changed:
            ld.load_staging(conn, "stg_staffing_roster", staff_raw, batch_id)
        log.info("STAGING  raw rows copied into the staging schema")

        # ---------------- 3. TRANSFORM ----------------
        issues = []
        transport, i1 = tf.clean_sample_tracking(tracking_raw, barcodes)
        fees, i2 = tf.clean_fee_schedule(fee_raw, set(dims["tests"]["test_code"]))
        events, i3 = tf.clean_equipment_logs(events_raw, set(dims["instruments"]["instrument_code"]))
        facts, i4 = tf.build_lab_test_facts(tests_raw, dims, transport, fees)
        # sample_tracking is re-read every run (to enrich facts) but its issues are
        # logged only on FULL runs, so incremental runs report only NEW problems
        issues += (i1 if run_type == "FULL" else []) + i3 + i4 + (i2 if fee_changed else [])
        staffing = pd.DataFrame()
        if staff_changed:
            staffing, i5 = tf.clean_staffing(staff_raw)
            issues += i5
        by_rule = pd.Series([i["rule_code"] for i in issues]).value_counts().sort_index().to_dict() \
            if issues else {}
        log.info(f"TRANSFORM data-quality issues found and handled: {len(issues)}  {by_rule}")

        # ---------------- 4. LOAD ----------------
        ld.load_dim_date(conn)
        n_dim = ld.upsert_dimensions(conn, dims, batch_id, tf.age_group)
        n_fee = ld.replace_fee_schedule(conn, fees, batch_id) if fee_changed else 0
        n_staff = ld.upsert_staffing(conn, staffing, batch_id) if staff_changed else 0
        n_evt = ld.upsert_instrument_events(conn, events, batch_id)
        n_fact = ld.upsert_lab_test_facts(conn, facts, batch_id)
        save_issues(conn, batch_id, issues)
        summary["loaded"] = {"dimension rows upserted": n_dim, "fee schedule rows": n_fee,
                             "staffing rows": n_staff, "instrument events": n_evt, "lab test facts": n_fact}
        for k, v in summary["loaded"].items():
            log.info(f"LOAD     {k:<26} {v}")

        with conn.cursor() as cur:
            cur.execute("REFRESH MATERIALIZED VIEW dw.mv_daily_lab_kpis")
        log.info("LOAD     materialized view dw.mv_daily_lab_kpis refreshed")

        # ---------------- 5. RECONCILIATION ----------------
        checks = [
            ("MySQL order_tests = warehouse fact_lab_test",
             ex.mysql_source_count(), count(conn, "SELECT COUNT(*) FROM dw.fact_lab_test")),
            ("Extracted this batch = facts upserted", len(tests_raw), n_fact),
            ("MongoDB equipment_logs = fact_instrument_event",
             ex.mongo_event_count(), count(conn, "SELECT COUNT(*) FROM dw.fact_instrument_event")),
            ("MySQL patients = dim_patient",
             len(dims["patients"]), count(conn, "SELECT COUNT(*) FROM dw.dim_patient")),
            ("Clean fee rows = ref_fee_schedule",
             len(fees), count(conn, "SELECT COUNT(*) FROM dw.ref_fee_schedule")),
        ]
        if staff_changed:
            checks.append(("Clean staffing rows = fact_daily_staffing",
                           len(staffing), count(conn, "SELECT COUNT(*) FROM dw.fact_daily_staffing")))
        all_passed = True
        for name, s, t in checks:
            ok = save_check(conn, batch_id, name, s, t)
            all_passed &= ok
            log.info(f"CHECK    {'PASS' if ok else 'FAIL'}  {name:<48} source={s} target={t}")

        # ---------------- 6. FINISH ----------------
        if not tests_raw.empty:
            save_watermark(conn, "mysql.lab_tests", pd.to_datetime(tests_raw["source_updated_at"]).max())
        if not events_raw.empty:
            save_watermark(conn, "mongodb.equipment_logs", pd.to_datetime(events_raw["event_time"]).max())
        save_watermark(conn, "file.payer_fee_schedule", fee_sum)
        save_watermark(conn, "file.lab_staffing_roster", staff_sum)

        with conn.cursor() as cur:
            cur.execute("""UPDATE etl.run_log SET finished_at = now(), status = %s, rows_extracted = %s,
                                  rows_loaded = %s, dq_issues = %s, message = %s WHERE batch_id = %s""",
                        ("SUCCESS", len(tests_raw) + len(events_raw), n_fact + n_evt + n_staff + n_fee,
                         len(issues), "all reconciliation checks passed" if all_passed
                         else "some reconciliation checks failed - review etl.reconciliation", batch_id))
        conn.commit()
        log.info(f"ETL batch {batch_id} finished: SUCCESS in {time.time() - started:.1f} s")
        log.info(f"Log file: {log_file.relative_to(PROJECT_ROOT)}")

    except Exception as exc:
        conn.rollback()
        log.exception(f"ETL batch {batch_id} FAILED: {exc}")
        if batch_id is not None:
            with conn.cursor() as cur:
                cur.execute("""UPDATE etl.run_log SET finished_at = now(), status = 'FAILED', message = %s
                               WHERE batch_id = %s""", (str(exc)[:500], batch_id))
            conn.commit()
        log.error("Nothing was saved and the watermarks did not move. Fix the problem and run again.")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clinical lab ETL pipeline")
    parser.add_argument("--full", action="store_true", help="reload everything (ignore watermarks)")
    run(full=parser.parse_args().full)
