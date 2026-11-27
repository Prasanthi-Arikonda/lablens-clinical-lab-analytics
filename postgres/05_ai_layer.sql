/* =====================================================================
   ADVANCED DATABASE CAPSTONE - Clinical Laboratory Data Engineering & Analytics
   Student : Prasanthi Arikonda (dal574888)
   File    : 05_ai_layer.sql
   Module 6: the database side of the Llama AI assistant (database: clinical_lab_dw)

   WHAT THIS FILE CREATES:
     1. Three simple "AI-friendly" views in the dw schema
        (a semantic layer: one flat table per topic with readable column names,
         so the small local Llama model can write correct SQL without complex joins)
            dw.ai_lab_tests          one row per ordered test
            dw.ai_instrument_events  one row per analyzer event (QC, error, maintenance)
            dw.ai_staffing           one row per day and shift
     2. Schema ai:
            ai.rag_chunks   document passages + their embeddings (pgvector) for RAG
            ai.query_log    every AI question, generated SQL and outcome (audit trail)

   Run AFTER 03_postgres_warehouse_ddl.sql and after the ETL has loaded data.
   Safe to run again.
   ===================================================================== */

CREATE EXTENSION IF NOT EXISTS vector;


/* ---------------------------------------------------------------------
   PART 1 - AI-friendly views (the only tables the AI's SQL may use)
   --------------------------------------------------------------------- */
CREATE OR REPLACE VIEW dw.ai_lab_tests AS
SELECT f.order_number,
       f.order_datetime::date                 AS order_date,
       d.year, d.quarter, d.month, d.year_month, d.day_name,
       t.test_code, t.test_name, t.category    AS test_category,
       i.instrument_code                       AS instrument,
       dp.department_name                      AS department,
       ph.physician_name,
       p.insurance_type, p.age_group, p.sex    AS patient_sex,
       s.shift_name                            AS shift,
       f.priority,
       f.test_status, f.sample_status, f.rejection_reason,
       f.transport_method,
       f.collect_to_receive_min                AS transport_minutes,
       f.tat_min                               AS tat_minutes,
       f.tat_target_min                        AS tat_target_minutes,
       f.tat_met                               AS met_tat_target,
       f.result_value, f.result_flag,
       f.is_abnormal, f.is_critical,
       f.reimbursement_usd                     AS revenue_usd
  FROM dw.fact_lab_test f
  JOIN dw.dim_date d          ON d.date_key = f.order_date_key
  JOIN dw.dim_test t          ON t.test_key = f.test_key
  JOIN dw.dim_department dp   ON dp.department_key = f.department_key
  JOIN dw.dim_physician ph    ON ph.physician_key = f.physician_key
  JOIN dw.dim_patient p       ON p.patient_key = f.patient_key
  LEFT JOIN dw.dim_instrument i ON i.instrument_key = f.instrument_key
  LEFT JOIN dw.dim_shift s      ON s.shift_key = f.shift_key;

CREATE OR REPLACE VIEW dw.ai_instrument_events AS
SELECT i.instrument_code   AS instrument,
       i.category          AS instrument_category,
       e.event_time,
       e.event_time::date  AS event_date,
       d.year, d.quarter, d.month, d.year_month,
       e.event_type,                    -- QC_RUN, ERROR, MAINTENANCE
       e.severity,
       e.error_code, e.error_message,
       e.downtime_min      AS downtime_minutes,
       e.qc_passed,
       e.qc_rule_violated
  FROM dw.fact_instrument_event e
  JOIN dw.dim_instrument i ON i.instrument_key = e.instrument_key
  JOIN dw.dim_date d       ON d.date_key = e.date_key;

CREATE OR REPLACE VIEW dw.ai_staffing AS
SELECT d.full_date AS work_date, d.year, d.quarter, d.month, d.year_month, d.day_name,
       s.shift_name AS shift,
       st.techs_scheduled, st.techs_present, st.call_outs, st.overtime_hours, st.staffing_pct
  FROM dw.fact_daily_staffing st
  JOIN dw.dim_date d  ON d.date_key = st.date_key
  JOIN dw.dim_shift s ON s.shift_key = st.shift_key;

GRANT SELECT ON dw.ai_lab_tests, dw.ai_instrument_events, dw.ai_staffing TO lab_ai_reader;


/* ---------------------------------------------------------------------
   PART 2 - RAG store and AI audit log
   --------------------------------------------------------------------- */
CREATE SCHEMA IF NOT EXISTS ai;

CREATE TABLE IF NOT EXISTS ai.rag_chunks (
    chunk_id          SERIAL PRIMARY KEY,
    source_collection TEXT NOT NULL,          -- lab_documents or incident_reports (MongoDB)
    source_id         TEXT NOT NULL,          -- doc_id or incident_id
    title             TEXT NOT NULL,
    section           TEXT,
    content           TEXT NOT NULL,          -- the passage text given to Llama
    metadata          JSONB,
    embedding         vector(768) NOT NULL,   -- nomic-embed-text produces 768 numbers
    indexed_at        TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (source_collection, source_id, section)
);

-- HNSW index: fast "nearest meaning" search using cosine distance
CREATE INDEX IF NOT EXISTS idx_rag_embedding
    ON ai.rag_chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS ai.query_log (
    log_id            BIGSERIAL PRIMARY KEY,
    asked_at          TIMESTAMP NOT NULL DEFAULT now(),
    feature           TEXT NOT NULL,          -- NL2SQL, RAG, INSIGHT
    question          TEXT NOT NULL,
    generated_sql     TEXT,
    validation        TEXT,                   -- ALLOWED or the reason it was BLOCKED
    status            TEXT,                   -- SUCCESS, BLOCKED, SQL_ERROR, NO_ANSWER
    row_count         INT,
    duration_sec      NUMERIC(8,2),
    answer            TEXT
);

-- The AI's read-only account can NOT see the ai schema (only dw)
REVOKE ALL ON SCHEMA ai FROM lab_ai_reader;


/* ---------------------------------------------------------------------
   PART 3 - CHECK
   --------------------------------------------------------------------- */
SELECT 'dw.ai_lab_tests' AS object, COUNT(*) AS rows FROM dw.ai_lab_tests
UNION ALL SELECT 'dw.ai_instrument_events', COUNT(*) FROM dw.ai_instrument_events
UNION ALL SELECT 'dw.ai_staffing', COUNT(*) FROM dw.ai_staffing
UNION ALL SELECT 'ai.rag_chunks (filled by Python)', COUNT(*) FROM ai.rag_chunks;
