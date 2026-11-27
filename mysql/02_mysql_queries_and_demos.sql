/* =====================================================================
   ADVANCED DATABASE CAPSTONE - Clinical Laboratory Data Engineering & Analytics
   Student : Prasanthi Arikonda (dal574888)
   File    : 02_mysql_queries_and_demos.sql
   Module 1: MySQL - views, business queries, and demos of the
             procedures, functions and triggers created in file 01.

   Run file 01 first. Then run this file with the first lightning bolt.
   Each SELECT opens its own "Result" tab at the bottom of Workbench
   (Result 1, Result 2, ...). Click each tab to see that query's answer.
   ===================================================================== */

USE clinical_lab_ops;


/* =====================================================================
   PART A - VIEWS (3)
   A view is a SAVED QUERY that behaves like a virtual table.
   ===================================================================== */

-- View 1: every order with patient, physician and department details
CREATE OR REPLACE VIEW vw_order_details AS
SELECT o.order_id, o.order_number, o.order_datetime, o.priority, o.order_status,
       p.patient_mrn, p.sex, p.birth_year, p.insurance_type,
       CONCAT(ph.first_name, ' ', ph.last_name) AS physician_name,
       d.department_name
  FROM lab_orders o
  JOIN patients    p  ON p.patient_id    = o.patient_id
  JOIN physicians  ph ON ph.physician_id = o.physician_id
  JOIN departments d  ON d.department_id = o.department_id;

-- View 2: turnaround time (TAT) of every resulted test, using function fn_tat_minutes
CREATE OR REPLACE VIEW vw_test_tat AS
SELECT ot.order_test_id, o.order_number, o.priority, o.order_datetime,
       tc.test_code, tc.test_name, tc.category, tc.tat_target_min,
       i.instrument_code,
       s.received_at, r.resulted_at,
       fn_tat_minutes(s.received_at, r.resulted_at)                         AS tat_min,
       IF(fn_tat_minutes(s.received_at, r.resulted_at) <= tc.tat_target_min,
          'MET', 'MISSED')                                                  AS tat_status
  FROM test_results r
  JOIN order_tests  ot ON ot.order_test_id = r.order_test_id
  JOIN lab_orders   o  ON o.order_id       = ot.order_id
  JOIN test_catalog tc ON tc.test_id       = ot.test_id
  JOIN samples      s  ON s.sample_id      = ot.sample_id
  JOIN instruments  i  ON i.instrument_id  = ot.instrument_id;

-- View 3: abnormal and critical results only
CREATE OR REPLACE VIEW vw_abnormal_results AS
SELECT r.result_id, o.order_number, p.patient_mrn, tc.test_name,
       r.result_value, tc.unit, tc.ref_low, tc.ref_high, r.result_flag, r.resulted_at
  FROM test_results r
  JOIN order_tests  ot ON ot.order_test_id = r.order_test_id
  JOIN lab_orders   o  ON o.order_id       = ot.order_id
  JOIN patients     p  ON p.patient_id     = o.patient_id
  JOIN test_catalog tc ON tc.test_id       = ot.test_id
 WHERE r.result_flag <> 'N';


/* =====================================================================
   PART B - BUSINESS QUERIES
   ===================================================================== */

-- Q1  MULTI-TABLE JOIN (6 tables)
-- "Show the 20 most recent results with patient, test, value, flag and instrument."
SELECT o.order_number, p.patient_mrn, tc.test_name, r.result_value, tc.unit,
       r.result_flag, i.instrument_code, r.resulted_at
  FROM test_results r
  JOIN order_tests  ot ON ot.order_test_id = r.order_test_id
  JOIN lab_orders   o  ON o.order_id       = ot.order_id
  JOIN patients     p  ON p.patient_id     = o.patient_id
  JOIN test_catalog tc ON tc.test_id       = ot.test_id
  JOIN instruments  i  ON i.instrument_id  = ot.instrument_id
 ORDER BY r.resulted_at DESC
 LIMIT 20;

-- Q2  GROUPING + AGGREGATE FUNCTIONS
-- "How many orders does each department send, and what share are STAT (urgent)?"
SELECT d.department_name,
       COUNT(*)                                          AS total_orders,
       SUM(o.priority = 'STAT')                          AS stat_orders,
       ROUND(100 * SUM(o.priority = 'STAT') / COUNT(*), 1) AS pct_stat
  FROM lab_orders o
  JOIN departments d ON d.department_id = o.department_id
 GROUP BY d.department_name
 ORDER BY total_orders DESC;

-- Q3  GROUP BY + HAVING
-- "Which tests miss their turnaround-time target more than 20% of the time?"
SELECT test_name, category,
       COUNT(*)                                          AS tests_resulted,
       ROUND(AVG(tat_min), 1)                            AS avg_tat_min,
       MAX(tat_target_min)                               AS target_min,
       ROUND(100 * SUM(tat_status = 'MISSED') / COUNT(*), 1) AS pct_missed
  FROM vw_test_tat
 GROUP BY test_name, category
HAVING pct_missed > 20
 ORDER BY pct_missed DESC;

-- Q4  NESTED SUBQUERY
-- "Which instruments have an average TAT higher than the lab-wide average TAT?"
SELECT instrument_code, ROUND(AVG(tat_min), 1) AS avg_tat_min
  FROM vw_test_tat
 GROUP BY instrument_code
HAVING AVG(tat_min) > (SELECT AVG(tat_min) FROM vw_test_tat)
 ORDER BY avg_tat_min DESC;

-- Q5  CORRELATED SUBQUERY
-- "Which tests took more than TWICE the normal time for their own instrument?"
--  (the inner query is re-run for each row, using that row's instrument)
SELECT v.order_number, v.test_name, v.instrument_code, v.tat_min,
       (SELECT ROUND(AVG(v2.tat_min), 1)
          FROM vw_test_tat v2
         WHERE v2.instrument_code = v.instrument_code) AS instrument_avg_tat
  FROM vw_test_tat v
 WHERE v.tat_min > 2 * (SELECT AVG(v2.tat_min)
                          FROM vw_test_tat v2
                         WHERE v2.instrument_code = v.instrument_code)
 ORDER BY v.tat_min DESC
 LIMIT 25;

-- Q6  CORRELATED SUBQUERY with EXISTS
-- "Which patients have at least one CRITICAL result (HH or LL)?"
SELECT p.patient_mrn, p.sex, p.birth_year
  FROM patients p
 WHERE EXISTS (SELECT 1
                 FROM lab_orders o
                 JOIN order_tests  ot ON ot.order_id = o.order_id
                 JOIN test_results r  ON r.order_test_id = ot.order_test_id
                WHERE o.patient_id = p.patient_id
                  AND r.result_flag IN ('HH', 'LL'))
 ORDER BY p.patient_mrn;

-- Q7  UNION ALL
-- "One list of all quality problems: rejected samples AND critical results."
SELECT 'Rejected sample' AS problem_type, s.sample_barcode AS reference,
       s.rejection_reason AS detail, s.received_at AS event_time
  FROM samples s
 WHERE s.sample_status = 'REJECTED'
UNION ALL
SELECT 'Critical result', CONCAT('RES-', r.result_id),
       CONCAT(tc.test_name, ' = ', r.result_value, ' ', tc.unit), r.resulted_at
  FROM test_results r
  JOIN order_tests  ot ON ot.order_test_id = r.order_test_id
  JOIN test_catalog tc ON tc.test_id = ot.test_id
 WHERE r.result_flag IN ('HH', 'LL')
 ORDER BY event_time DESC;

-- Q8  UNION (removes duplicates)
-- "Which patients had ANY problem (a rejected sample OR a critical result)?"
--  A patient with both problems appears only once.
SELECT o.patient_id
  FROM samples s JOIN lab_orders o ON o.order_id = s.order_id
 WHERE s.sample_status = 'REJECTED'
UNION
SELECT o.patient_id
  FROM test_results r
  JOIN order_tests ot ON ot.order_test_id = r.order_test_id
  JOIN lab_orders  o  ON o.order_id = ot.order_id
 WHERE r.result_flag IN ('HH', 'LL');

-- Q9  MONTHLY TREND (the Q3 problem story)
-- "Average chemistry TAT by month for each chemistry analyzer."
SELECT DATE_FORMAT(received_at, '%Y-%m') AS month,
       instrument_code,
       COUNT(*)               AS tests,
       ROUND(AVG(tat_min), 1) AS avg_tat_min
  FROM vw_test_tat
 WHERE category = 'Chemistry'
 GROUP BY month, instrument_code
 ORDER BY month, instrument_code;

-- Q10 SAMPLE REJECTION RATE by department
SELECT d.department_name,
       COUNT(*)                                                AS samples,
       SUM(s.sample_status = 'REJECTED')                       AS rejected,
       ROUND(100 * SUM(s.sample_status = 'REJECTED') / COUNT(*), 2) AS pct_rejected
  FROM samples s
  JOIN lab_orders  o ON o.order_id = s.order_id
  JOIN departments d ON d.department_id = o.department_id
 GROUP BY d.department_name
 ORDER BY pct_rejected DESC;

-- Q11 USING THE VIEWS: abnormal results by test
SELECT test_name, result_flag, COUNT(*) AS results
  FROM vw_abnormal_results
 GROUP BY test_name, result_flag
 ORDER BY results DESC;


/* =====================================================================
   PART C - DEMOS OF PROCEDURES, FUNCTIONS AND TRIGGERS
   ===================================================================== */

-- D1  FUNCTION demo: fn_tat_minutes and fn_result_flag called directly
SELECT fn_tat_minutes('2026-03-01 08:00:00', '2026-03-01 09:45:00') AS tat_example_min,
       fn_result_flag(250, 70, 99, 40, 450)  AS glucose_250_flag,
       fn_result_flag(500, 70, 99, 40, 450)  AS glucose_500_flag,
       fn_result_flag(85,  70, 99, 40, 450)  AS glucose_85_flag;

-- D2  PROCEDURE demo: turnaround-time report for Q3 2026 (Jul - Sep)
CALL sp_tat_summary('2026-07-01', '2026-09-30');

-- D3  PROCEDURE demo: create a new STAT glucose order for patient MRN000001
CALL sp_create_lab_order('MRN000001', 'PHY0001', 'STAT', 'GLU', @new_order_id);
SELECT * FROM lab_orders WHERE order_id = @new_order_id;

-- D4  PROCEDURE + TRIGGER demo: change the status -> audit trigger writes a row
CALL sp_update_order_status(@new_order_id, 'COLLECTED');
CALL sp_update_order_status(@new_order_id, 'IN_PROGRESS');
SELECT * FROM order_status_audit WHERE order_id = @new_order_id;

-- D5  TRIGGER demo: insert a glucose result of 520 -> trigger sets the flag to 'HH'
INSERT INTO test_results (order_test_id, result_value, resulted_at, verified_by)
SELECT order_test_id, 520, NOW(), 'TECH01'
  FROM order_tests WHERE order_id = @new_order_id;
SELECT r.*, tc.test_name, tc.ref_low, tc.ref_high
  FROM test_results r
  JOIN order_tests  ot ON ot.order_test_id = r.order_test_id
  JOIN test_catalog tc ON tc.test_id = ot.test_id
 WHERE ot.order_id = @new_order_id;


/* =====================================================================
   D6  TRIGGER demo (sample time check) - RUN THIS ONE SEPARATELY
   This statement is SUPPOSED to fail: the trigger blocks a sample that
   was "received" before it was "collected". It is inside a comment so
   it does not stop the rest of the file.
   To run it: select (highlight) ONLY the INSERT statement below
   (without the slash-star comment marks), then click the first lightning bolt.
   Expected: red error "Invalid sample: received_at cannot be earlier than collected_at"

INSERT INTO samples (sample_barcode, order_id, specimen_type, collected_at, received_at)
VALUES ('TEST-BAD-01', 1, 'Serum', '2026-05-01 10:00:00', '2026-05-01 09:00:00');

   ===================================================================== */
