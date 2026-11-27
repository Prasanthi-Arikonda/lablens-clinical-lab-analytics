/* =====================================================================
   ADVANCED DATABASE CAPSTONE - Clinical Laboratory Data Engineering & Analytics
   Student : Prasanthi Arikonda (dal574888)
   File    : 04_postgres_business_queries.sql
   Module 3: 12 business questions answered from the PostgreSQL warehouse,
             plus an index / query-plan comparison.

   Run AFTER the ETL pipeline has loaded the warehouse.
   In pgAdmin: select ONE query (highlight it) and press F5, or put the cursor
   inside a query and press F5 - each answer appears in "Data Output".
   ===================================================================== */

SET search_path = dw;

-- Q1  Overall lab KPIs
SELECT COUNT(*)                                                     AS tests_ordered,
       COUNT(*) FILTER (WHERE resulted_at IS NOT NULL)              AS tests_resulted,
       ROUND(AVG(tat_min), 1)                                       AS avg_tat_min,
       ROUND(100.0 * AVG(CASE WHEN tat_met THEN 1 ELSE 0 END)
             FILTER (WHERE tat_met IS NOT NULL), 1)                 AS pct_within_target,
       ROUND(100.0 * AVG(CASE WHEN sample_status = 'REJECTED' THEN 1 ELSE 0 END), 2) AS pct_rejected,
       COUNT(*) FILTER (WHERE is_critical)                          AS critical_results,
       ROUND(SUM(reimbursement_usd), 2)                             AS total_revenue_usd
  FROM fact_lab_test;

-- Q2  Turnaround time by quarter - did performance decline in Q3?
SELECT d.year, d.quarter,
       COUNT(*)                                                     AS tests_resulted,
       ROUND(AVG(f.tat_min), 1)                                     AS avg_tat_min,
       ROUND(100.0 * AVG(CASE WHEN f.tat_met THEN 1 ELSE 0 END), 1) AS pct_within_target
  FROM fact_lab_test f
  JOIN dim_date d ON d.date_key = f.order_date_key
 WHERE f.resulted_at IS NOT NULL
 GROUP BY d.year, d.quarter
 ORDER BY d.year, d.quarter;

-- Q3  Which instrument caused the Q3 decline? (TAT by instrument and quarter)
SELECT i.instrument_code, d.quarter,
       COUNT(*)                         AS tests,
       ROUND(AVG(f.tat_min), 1)         AS avg_tat_min
  FROM fact_lab_test f
  JOIN dim_instrument i ON i.instrument_key = f.instrument_key
  JOIN dim_date d       ON d.date_key = f.order_date_key
 WHERE i.category = 'Chemistry' AND f.resulted_at IS NOT NULL
 GROUP BY i.instrument_code, d.quarter
 ORDER BY i.instrument_code, d.quarter;

-- Q4  Instrument errors, downtime and QC failures by month (from MongoDB logs)
SELECT instrument_code, year_month, error_events, downtime_min, qc_failures, avg_tat_min
  FROM vw_instrument_performance
 WHERE instrument_code IN ('CHEM-01', 'CHEM-02')
 ORDER BY instrument_code, year_month;

-- Q5  Top 5 tests by revenue (fee schedule from the external Excel file)
SELECT t.test_code, t.test_name,
       COUNT(*) FILTER (WHERE f.test_status = 'COMPLETED') AS completed_tests,
       ROUND(SUM(f.reimbursement_usd), 2)                  AS revenue_usd
  FROM fact_lab_test f
  JOIN dim_test t ON t.test_key = f.test_key
 GROUP BY t.test_code, t.test_name
 ORDER BY revenue_usd DESC
 LIMIT 5;

-- Q6  Revenue by insurance type and month (window function: running total)
SELECT d.year_month, p.insurance_type,
       ROUND(SUM(f.reimbursement_usd), 2) AS revenue_usd,
       ROUND(SUM(SUM(f.reimbursement_usd)) OVER (PARTITION BY p.insurance_type
                                                ORDER BY d.year_month), 2) AS running_total_usd
  FROM fact_lab_test f
  JOIN dim_patient p ON p.patient_key = f.patient_key
  JOIN dim_date d    ON d.date_key = f.order_date_key
 GROUP BY d.year_month, p.insurance_type
 ORDER BY p.insurance_type, d.year_month;

-- Q7  STAT performance by department (urgent tests meeting the 60-minute target)
SELECT dp.department_name,
       COUNT(*)                                                     AS stat_tests,
       ROUND(AVG(f.tat_min), 1)                                     AS avg_tat_min,
       ROUND(100.0 * AVG(CASE WHEN f.tat_met THEN 1 ELSE 0 END), 1) AS pct_within_target
  FROM fact_lab_test f
  JOIN dim_department dp ON dp.department_key = f.department_key
 WHERE f.priority = 'STAT' AND f.resulted_at IS NOT NULL
 GROUP BY dp.department_name
 ORDER BY pct_within_target;

-- Q8  Does staffing explain slower shifts? (lab tests joined with the external CSV)
SELECT s.shift_name,
       COUNT(*)                          AS tests_resulted,
       ROUND(AVG(f.tat_min), 1)          AS avg_tat_min,
       ROUND(AVG(st.techs_present), 1)   AS avg_techs_present,
       ROUND(AVG(st.staffing_pct), 1)    AS avg_staffing_pct
  FROM fact_lab_test f
  JOIN dim_shift s ON s.shift_key = f.shift_key
  JOIN dim_date  d ON d.full_date = f.received_at::date
  LEFT JOIN fact_daily_staffing st ON st.date_key = d.date_key AND st.shift_key = f.shift_key
 WHERE f.resulted_at IS NOT NULL
 GROUP BY s.shift_key, s.shift_name
 ORDER BY s.shift_key;

-- Q9  Abnormal and critical result rates by test (with RANK window function)
SELECT t.test_name,
       COUNT(*)                                                  AS results,
       ROUND(100.0 * AVG(CASE WHEN f.is_abnormal THEN 1 ELSE 0 END), 1) AS pct_abnormal,
       COUNT(*) FILTER (WHERE f.is_critical)                     AS critical_results,
       RANK() OVER (ORDER BY AVG(CASE WHEN f.is_abnormal THEN 1 ELSE 0 END) DESC) AS abnormal_rank
  FROM fact_lab_test f
  JOIN dim_test t ON t.test_key = f.test_key
 WHERE f.result_value IS NOT NULL
 GROUP BY t.test_name
 ORDER BY abnormal_rank
 LIMIT 10;

-- Q10 Sample rejection reasons (pre-analytical quality)
SELECT rejection_reason, COUNT(DISTINCT sample_barcode) AS rejected_samples,
       ROUND(100.0 * COUNT(DISTINCT sample_barcode)
             / SUM(COUNT(DISTINCT sample_barcode)) OVER (), 1) AS pct_of_rejections
  FROM fact_lab_test
 WHERE sample_status = 'REJECTED'
 GROUP BY rejection_reason
 ORDER BY rejected_samples DESC;

-- Q11 Transport method vs time to reach the lab (MongoDB data inside the warehouse)
SELECT transport_method, priority,
       COUNT(DISTINCT sample_barcode)        AS samples,
       ROUND(AVG(collect_to_receive_min), 1) AS avg_transport_min
  FROM fact_lab_test
 WHERE transport_method IS NOT NULL
 GROUP BY transport_method, priority
 ORDER BY transport_method, priority;

-- Q12 Month-over-month change in average TAT (LAG window function)
SELECT year_month, avg_tat_min,
       avg_tat_min - LAG(avg_tat_min) OVER (ORDER BY year_month) AS change_vs_prev_month
  FROM (SELECT d.year_month, ROUND(AVG(f.tat_min), 1) AS avg_tat_min
          FROM fact_lab_test f JOIN dim_date d ON d.date_key = f.order_date_key
         WHERE f.resulted_at IS NOT NULL
         GROUP BY d.year_month) m
 ORDER BY year_month;

-- Q13 The materialized view (precomputed daily KPIs used by the dashboard)
SELECT * FROM mv_daily_lab_kpis ORDER BY full_date DESC LIMIT 14;


/* =====================================================================
   INDEX / QUERY PLAN COMPARISON
   Question: "show all tests received during one afternoon".
   Run steps P1, P2, P3 one at a time and compare the plans and execution times.
   ===================================================================== */

-- P1  BEFORE the index: PostgreSQL must read the WHOLE table ("Seq Scan")
DROP INDEX IF EXISTS idx_flt_received;
EXPLAIN ANALYZE
SELECT order_number, received_at, tat_min
  FROM fact_lab_test
 WHERE received_at BETWEEN '2026-08-14 12:00' AND '2026-08-14 18:00';

-- P2  Create the index on received_at
CREATE INDEX idx_flt_received ON fact_lab_test (received_at);
ANALYZE fact_lab_test;

-- P3  AFTER the index: PostgreSQL jumps straight to the matching rows ("Index Scan" / "Bitmap Index Scan")
EXPLAIN ANALYZE
SELECT order_number, received_at, tat_min
  FROM fact_lab_test
 WHERE received_at BETWEEN '2026-08-14 12:00' AND '2026-08-14 18:00';
