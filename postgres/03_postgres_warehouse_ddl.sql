/* =====================================================================
   ADVANCED DATABASE CAPSTONE - Clinical Laboratory Data Engineering & Analytics
   Student : Prasanthi Arikonda (dal574888)
   File    : 03_postgres_warehouse_ddl.sql
   Module 3: PostgreSQL enterprise analytics warehouse  (database: clinical_lab_dw)

   WHAT THIS FILE CREATES:
     Schema staging : raw copies of the source data, exactly as extracted
     Schema dw      : the cleaned STAR SCHEMA
                        7 dimension tables  (dim_date, dim_patient, dim_test, dim_instrument,
                                             dim_department, dim_physician, dim_shift)
                        3 fact tables       (fact_lab_test, fact_instrument_event,
                                             fact_daily_staffing)
                        analytical views + 1 materialized view
     Schema etl     : pipeline control tables (run log, watermarks, data-quality issues)
     Role lab_ai_reader : READ-ONLY account that the Llama assistant will use

   The tables are EMPTY after this script. The Python ETL pipeline (Module 4) fills them.

   HOW TO RUN: in pgAdmin, open the Query Tool on database clinical_lab_dw,
   open this file, and press F5 (Execute script). Safe to run again: it rebuilds everything.
   ===================================================================== */

-- Start clean (removes old versions of the three schemas and everything in them)
DROP SCHEMA IF EXISTS dw      CASCADE;
DROP SCHEMA IF EXISTS staging CASCADE;
DROP SCHEMA IF EXISTS etl     CASCADE;

CREATE SCHEMA staging;
CREATE SCHEMA dw;
CREATE SCHEMA etl;


/* =====================================================================
   PART 1 - STAGING TABLES (raw data, loaded as-is, replaced on every run)
   ===================================================================== */

-- One row per ordered test, flattened from the MySQL tables
CREATE TABLE staging.stg_mysql_lab_tests (
    order_test_id      INT,
    order_number       TEXT,
    order_datetime     TIMESTAMP,
    priority           TEXT,
    order_status       TEXT,
    patient_mrn        TEXT,
    physician_code     TEXT,
    department_code    TEXT,
    test_code          TEXT,
    instrument_code    TEXT,
    test_status        TEXT,
    sample_barcode     TEXT,
    sample_status      TEXT,
    rejection_reason   TEXT,
    collected_at       TIMESTAMP,
    received_at        TIMESTAMP,
    result_value       NUMERIC(10,2),
    result_flag        TEXT,
    resulted_at        TIMESTAMP,
    source_updated_at  TIMESTAMP,
    etl_batch_id       INT
);

CREATE TABLE staging.stg_mongo_sample_tracking (
    mongo_id           TEXT,
    sample_barcode_raw TEXT,
    order_number       TEXT,
    transport_method   TEXT,
    transport_minutes  TEXT,
    temperature_raw    TEXT,
    custody_steps      INT,
    etl_batch_id       INT
);

CREATE TABLE staging.stg_mongo_equipment_logs (
    mongo_id           TEXT,
    instrument_code    TEXT,
    event_type         TEXT,
    event_time         TIMESTAMP,
    severity           TEXT,
    error_code         TEXT,
    error_message      TEXT,
    downtime_min       TEXT,
    qc_passed          TEXT,
    qc_rule            TEXT,
    etl_batch_id       INT
);

CREATE TABLE staging.stg_fee_schedule (
    test_code_raw      TEXT,
    test_description   TEXT,
    payer_type_raw     TEXT,
    allowed_amount_raw TEXT,
    effective_date_raw TEXT,
    etl_batch_id       INT
);

CREATE TABLE staging.stg_staffing_roster (
    date_raw           TEXT,
    shift_raw          TEXT,
    techs_scheduled    TEXT,
    techs_present      TEXT,
    call_outs          TEXT,
    overtime_hours     TEXT,
    etl_batch_id       INT
);


/* =====================================================================
   PART 2 - DIMENSION TABLES (the "who / what / where / when")
   Every dimension has a SURROGATE KEY (..._key, generated here) and keeps
   the NATURAL KEY from the source system (lineage).
   ===================================================================== */

CREATE TABLE dw.dim_date (
    date_key      INT PRIMARY KEY,                 -- e.g. 20260715
    full_date     DATE NOT NULL UNIQUE,
    year          SMALLINT NOT NULL,
    quarter       SMALLINT NOT NULL CHECK (quarter BETWEEN 1 AND 4),
    month         SMALLINT NOT NULL CHECK (month BETWEEN 1 AND 12),
    month_name    TEXT NOT NULL,
    year_month    TEXT NOT NULL,                   -- e.g. 2026-07
    day_of_month  SMALLINT NOT NULL,
    day_of_week   SMALLINT NOT NULL,               -- 1 = Monday ... 7 = Sunday
    day_name      TEXT NOT NULL,
    is_weekend    BOOLEAN NOT NULL
);

CREATE TABLE dw.dim_shift (
    shift_key     SMALLINT PRIMARY KEY,
    shift_name    TEXT NOT NULL UNIQUE,            -- Day, Evening, Night
    start_hour    SMALLINT NOT NULL,
    end_hour      SMALLINT NOT NULL
);

CREATE TABLE dw.dim_patient (
    patient_key       SERIAL PRIMARY KEY,
    patient_mrn       TEXT NOT NULL UNIQUE,        -- natural key from MySQL
    birth_year        SMALLINT,
    age_group         TEXT,
    sex               TEXT CHECK (sex IN ('F','M','U')),
    zip3              CHAR(3),
    insurance_type    TEXT,
    source_system     TEXT NOT NULL DEFAULT 'mysql.clinical_lab_ops',
    etl_batch_id      INT,
    loaded_at         TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE dw.dim_test (
    test_key          SERIAL PRIMARY KEY,
    test_code         TEXT NOT NULL UNIQUE,
    test_name         TEXT NOT NULL,
    category          TEXT NOT NULL,
    specimen_type     TEXT NOT NULL,
    unit              TEXT,
    ref_low           NUMERIC(10,2),
    ref_high          NUMERIC(10,2),
    critical_low      NUMERIC(10,2),
    critical_high     NUMERIC(10,2),
    tat_target_min    INT CHECK (tat_target_min > 0),
    list_price_usd    NUMERIC(8,2) CHECK (list_price_usd >= 0),
    source_system     TEXT NOT NULL DEFAULT 'mysql.clinical_lab_ops',
    etl_batch_id      INT
);

CREATE TABLE dw.dim_instrument (
    instrument_key    SERIAL PRIMARY KEY,
    instrument_code   TEXT NOT NULL UNIQUE,
    instrument_name   TEXT NOT NULL,
    manufacturer      TEXT,
    category          TEXT,
    install_date      DATE,
    source_system     TEXT NOT NULL DEFAULT 'mysql.clinical_lab_ops',
    etl_batch_id      INT
);

CREATE TABLE dw.dim_department (
    department_key    SERIAL PRIMARY KEY,
    department_code   TEXT NOT NULL UNIQUE,
    department_name   TEXT NOT NULL,
    location          TEXT,
    source_system     TEXT NOT NULL DEFAULT 'mysql.clinical_lab_ops',
    etl_batch_id      INT
);

CREATE TABLE dw.dim_physician (
    physician_key     SERIAL PRIMARY KEY,
    physician_code    TEXT NOT NULL UNIQUE,
    physician_name    TEXT NOT NULL,
    specialty         TEXT,
    department_code   TEXT,
    source_system     TEXT NOT NULL DEFAULT 'mysql.clinical_lab_ops',
    etl_batch_id      INT
);


/* =====================================================================
   PART 3 - FACT TABLES (the measurements)
   ===================================================================== */

-- Main fact: one row per ordered lab test (grain = MySQL order_tests row)
CREATE TABLE dw.fact_lab_test (
    lab_test_key          BIGSERIAL PRIMARY KEY,
    order_test_id         INT NOT NULL UNIQUE,            -- source key (lineage, upserts)
    order_number          TEXT NOT NULL,
    sample_barcode        TEXT,
    order_date_key        INT NOT NULL REFERENCES dw.dim_date(date_key),
    patient_key           INT NOT NULL REFERENCES dw.dim_patient(patient_key),
    test_key              INT NOT NULL REFERENCES dw.dim_test(test_key),
    instrument_key        INT REFERENCES dw.dim_instrument(instrument_key),
    department_key        INT NOT NULL REFERENCES dw.dim_department(department_key),
    physician_key         INT NOT NULL REFERENCES dw.dim_physician(physician_key),
    shift_key             SMALLINT REFERENCES dw.dim_shift(shift_key),   -- shift when received
    priority              TEXT NOT NULL CHECK (priority IN ('STAT','ROUTINE')),
    order_status          TEXT,
    test_status           TEXT,
    sample_status         TEXT,
    rejection_reason      TEXT,
    order_datetime        TIMESTAMP NOT NULL,
    collected_at          TIMESTAMP,
    received_at           TIMESTAMP,
    resulted_at           TIMESTAMP,
    collect_to_receive_min INT,                            -- transport time
    tat_min               INT,                             -- received -> resulted
    total_tat_min         INT,                             -- ordered  -> resulted
    tat_target_min        INT,
    tat_met               BOOLEAN,
    result_value          NUMERIC(10,2),
    result_flag           TEXT,
    is_abnormal           BOOLEAN,
    is_critical           BOOLEAN,
    reimbursement_usd     NUMERIC(10,2),                   -- from the external fee schedule
    transport_method      TEXT,                            -- from MongoDB sample_tracking
    transport_temp_c      NUMERIC(5,1),                    -- from MongoDB sample_tracking
    source_system         TEXT NOT NULL DEFAULT 'mysql+mongodb+xlsx',
    source_updated_at     TIMESTAMP,
    etl_batch_id          INT,
    loaded_at             TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT chk_tat_nonnegative CHECK (tat_min IS NULL OR tat_min >= 0)
);

-- Instrument events from MongoDB (QC runs, errors, maintenance)
CREATE TABLE dw.fact_instrument_event (
    event_key         BIGSERIAL PRIMARY KEY,
    mongo_id          TEXT NOT NULL UNIQUE,                -- source key (lineage)
    instrument_key    INT NOT NULL REFERENCES dw.dim_instrument(instrument_key),
    date_key          INT NOT NULL REFERENCES dw.dim_date(date_key),
    event_time        TIMESTAMP NOT NULL,
    event_type        TEXT NOT NULL,
    severity          TEXT,
    error_code        TEXT,
    error_message     TEXT,
    downtime_min      INT CHECK (downtime_min IS NULL OR downtime_min >= 0),
    qc_passed         BOOLEAN,
    qc_rule_violated  TEXT,
    source_system     TEXT NOT NULL DEFAULT 'mongodb.clinical_lab_docs',
    etl_batch_id      INT
);

-- Staffing per day and shift from the external CSV
CREATE TABLE dw.fact_daily_staffing (
    staffing_key      SERIAL PRIMARY KEY,
    date_key          INT NOT NULL REFERENCES dw.dim_date(date_key),
    shift_key         SMALLINT NOT NULL REFERENCES dw.dim_shift(shift_key),
    techs_scheduled   INT CHECK (techs_scheduled >= 0),
    techs_present     INT CHECK (techs_present >= 0),
    call_outs         INT CHECK (call_outs >= 0),
    overtime_hours    NUMERIC(5,1),
    staffing_pct      NUMERIC(5,1),                        -- present / scheduled * 100
    source_system     TEXT NOT NULL DEFAULT 'csv.lab_staffing_roster',
    etl_batch_id      INT,
    CONSTRAINT uq_staffing_day_shift UNIQUE (date_key, shift_key)
);

-- Fee schedule kept as a small lookup (cleaned from the external Excel file)
CREATE TABLE dw.ref_fee_schedule (
    test_code         TEXT NOT NULL,
    payer_type        TEXT NOT NULL,
    allowed_amount_usd NUMERIC(10,2),
    effective_date    DATE,
    source_system     TEXT NOT NULL DEFAULT 'xlsx.payer_fee_schedule',
    etl_batch_id      INT,
    PRIMARY KEY (test_code, payer_type)
);


/* =====================================================================
   PART 4 - INDEXES on the fact tables (for the most common filters / joins)
   ===================================================================== */
CREATE INDEX idx_flt_order_date  ON dw.fact_lab_test (order_date_key);
CREATE INDEX idx_flt_test        ON dw.fact_lab_test (test_key);
CREATE INDEX idx_flt_instrument  ON dw.fact_lab_test (instrument_key);
CREATE INDEX idx_flt_department  ON dw.fact_lab_test (department_key);
CREATE INDEX idx_flt_patient     ON dw.fact_lab_test (patient_key);
CREATE INDEX idx_fie_instr_time  ON dw.fact_instrument_event (instrument_key, event_time);
CREATE INDEX idx_fie_type        ON dw.fact_instrument_event (event_type);
-- (idx_flt_received is created in file 04 to compare query plans with / without it)


/* =====================================================================
   PART 5 - ETL CONTROL TABLES
   ===================================================================== */
CREATE TABLE etl.run_log (
    batch_id          SERIAL PRIMARY KEY,
    started_at        TIMESTAMP NOT NULL DEFAULT now(),
    finished_at       TIMESTAMP,
    run_type          TEXT,                                 -- FULL or INCREMENTAL
    status            TEXT NOT NULL DEFAULT 'RUNNING',      -- RUNNING, SUCCESS, FAILED
    rows_extracted    INT DEFAULT 0,
    rows_loaded       INT DEFAULT 0,
    dq_issues         INT DEFAULT 0,
    message           TEXT
);

CREATE TABLE etl.watermark (
    source_name       TEXT PRIMARY KEY,                     -- e.g. mysql.lab_tests
    last_value        TEXT,                                 -- last timestamp / checksum loaded
    updated_at        TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE etl.dq_issue (
    issue_id          BIGSERIAL PRIMARY KEY,
    batch_id          INT REFERENCES etl.run_log(batch_id),
    rule_code         TEXT NOT NULL,                        -- DQ01 ... DQ08
    source_name       TEXT NOT NULL,
    record_ref        TEXT,
    issue             TEXT NOT NULL,
    action_taken      TEXT NOT NULL,
    logged_at         TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE etl.reconciliation (
    batch_id          INT REFERENCES etl.run_log(batch_id),
    check_name        TEXT,
    source_count      INT,
    target_count      INT,
    passed            BOOLEAN,
    checked_at        TIMESTAMP NOT NULL DEFAULT now()
);


/* =====================================================================
   PART 6 - ANALYTICAL VIEWS and a MATERIALIZED VIEW
   ===================================================================== */

-- Turnaround time by month, test category and priority
CREATE VIEW dw.vw_monthly_tat AS
SELECT d.year_month, t.category, f.priority,
       COUNT(*)                                           AS tests_resulted,
       ROUND(AVG(f.tat_min), 1)                           AS avg_tat_min,
       ROUND(100.0 * AVG(CASE WHEN f.tat_met THEN 1 ELSE 0 END), 1) AS pct_within_target
  FROM dw.fact_lab_test f
  JOIN dw.dim_date d ON d.date_key = f.order_date_key
  JOIN dw.dim_test t ON t.test_key = f.test_key
 WHERE f.resulted_at IS NOT NULL
 GROUP BY d.year_month, t.category, f.priority;

-- Instrument performance: workload, TAT, errors, downtime, QC failures
CREATE VIEW dw.vw_instrument_performance AS
SELECT i.instrument_code, i.category, d.year_month,
       (SELECT COUNT(*) FROM dw.fact_lab_test f
          JOIN dw.dim_date fd ON fd.date_key = f.order_date_key
         WHERE f.instrument_key = i.instrument_key AND fd.year_month = d.year_month
           AND f.resulted_at IS NOT NULL)                                     AS tests_resulted,
       (SELECT ROUND(AVG(f.tat_min), 1) FROM dw.fact_lab_test f
          JOIN dw.dim_date fd ON fd.date_key = f.order_date_key
         WHERE f.instrument_key = i.instrument_key AND fd.year_month = d.year_month) AS avg_tat_min,
       COUNT(*) FILTER (WHERE e.event_type = 'ERROR')                         AS error_events,
       COALESCE(SUM(e.downtime_min), 0)                                       AS downtime_min,
       COUNT(*) FILTER (WHERE e.event_type = 'QC_RUN' AND e.qc_passed = FALSE) AS qc_failures
  FROM dw.fact_instrument_event e
  JOIN dw.dim_instrument i ON i.instrument_key = e.instrument_key
  JOIN dw.dim_date d       ON d.date_key = e.date_key
 GROUP BY i.instrument_key, i.instrument_code, i.category, d.year_month;

-- Department KPIs: volume, STAT share, rejection rate, abnormal rate, revenue
CREATE VIEW dw.vw_department_kpis AS
SELECT dp.department_name,
       COUNT(*)                                                         AS tests_ordered,
       ROUND(100.0 * AVG(CASE WHEN f.priority = 'STAT' THEN 1 ELSE 0 END), 1)       AS pct_stat,
       ROUND(100.0 * AVG(CASE WHEN f.sample_status = 'REJECTED' THEN 1 ELSE 0 END), 2) AS pct_rejected,
       ROUND(100.0 * AVG(CASE WHEN f.is_abnormal THEN 1 ELSE 0 END), 1)             AS pct_abnormal,
       ROUND(SUM(f.reimbursement_usd), 2)                                AS revenue_usd
  FROM dw.fact_lab_test f
  JOIN dw.dim_department dp ON dp.department_key = f.department_key
 GROUP BY dp.department_name;

-- Materialized view: daily KPI snapshot for the dashboard (stored, refreshed after each ETL run)
CREATE MATERIALIZED VIEW dw.mv_daily_lab_kpis AS
SELECT d.full_date, d.year_month, d.day_name, d.is_weekend,
       COUNT(*)                                                    AS tests_ordered,
       COUNT(*) FILTER (WHERE f.resulted_at IS NOT NULL)           AS tests_resulted,
       COUNT(*) FILTER (WHERE f.priority = 'STAT')                 AS stat_tests,
       ROUND(AVG(f.tat_min), 1)                                    AS avg_tat_min,
       ROUND(100.0 * AVG(CASE WHEN f.tat_met THEN 1 ELSE 0 END), 1) AS pct_within_target,
       COUNT(*) FILTER (WHERE f.is_critical)                       AS critical_results,
       COUNT(*) FILTER (WHERE f.sample_status = 'REJECTED')        AS rejected_tests,
       ROUND(SUM(f.reimbursement_usd), 2)                          AS revenue_usd
  FROM dw.fact_lab_test f
  JOIN dw.dim_date d ON d.date_key = f.order_date_key
 GROUP BY d.full_date, d.year_month, d.day_name, d.is_weekend
WITH NO DATA;   -- filled by: REFRESH MATERIALIZED VIEW dw.mv_daily_lab_kpis;

CREATE UNIQUE INDEX idx_mv_daily_date ON dw.mv_daily_lab_kpis (full_date);


/* =====================================================================
   PART 7 - READ-ONLY ACCOUNT FOR THE AI ASSISTANT (least privilege)
   The Llama NL-to-SQL feature connects with this account. It can only
   READ the dw schema - it cannot change data, create tables, or see
   the staging / etl schemas. Every query is also limited to 5 seconds.
   ===================================================================== */
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lab_ai_reader') THEN
        CREATE ROLE lab_ai_reader LOGIN;
    END IF;
END $$;

GRANT CONNECT ON DATABASE clinical_lab_dw TO lab_ai_reader;
GRANT USAGE  ON SCHEMA dw TO lab_ai_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA dw TO lab_ai_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA dw GRANT SELECT ON TABLES TO lab_ai_reader;
REVOKE ALL ON SCHEMA staging, etl FROM lab_ai_reader;
ALTER ROLE lab_ai_reader SET default_transaction_read_only = on;
ALTER ROLE lab_ai_reader SET statement_timeout = '5s';
ALTER ROLE lab_ai_reader SET search_path = dw;


/* =====================================================================
   PART 8 - CHECK: list the tables that were created
   ===================================================================== */
SELECT table_schema, table_name, table_type
  FROM information_schema.tables
 WHERE table_schema IN ('staging', 'dw', 'etl')
 ORDER BY table_schema, table_type, table_name;
