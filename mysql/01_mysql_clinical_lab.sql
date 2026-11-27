/* =====================================================================
   ADVANCED DATABASE CAPSTONE - Clinical Laboratory Data Engineering & Analytics
   Student : Prasanthi Arikonda (dal574888)
   File    : 01_mysql_clinical_lab.sql
   Module 1: MySQL Operational (transactional) database

   WHAT THIS FILE DOES (top to bottom):
     PART 1  - Creates the database  clinical_lab_ops
     PART 2  - Creates 10 related tables (primary keys, foreign keys,
               unique constraints, check constraints)
     PART 3  - Creates 2 stored FUNCTIONS
     PART 4  - Creates 3 TRIGGERS
     PART 5  - Creates 4 stored PROCEDURES (one of them generates the data)
     PART 6  - Loads fixed reference data (departments, instruments, test catalog)
     PART 7  - Generates realistic SYNTHETIC data (fake, de-identified patients)
     PART 8  - Shows row counts so you can confirm 5,000+ records

   All patient data is synthetic (computer-generated). No real patient
   information is used. Patients have only a code (MRN), birth year,
   sex and 3-digit ZIP prefix - no names, no addresses (de-identified).

   HOW TO RUN: open this file in MySQL Workbench and click the
   lightning-bolt button WITHOUT the cursor (the first one = "Execute all").
   It is safe to run again: it deletes and rebuilds the database each time.
   ===================================================================== */


/* =====================================================================
   PART 1 - CREATE THE DATABASE
   ===================================================================== */
DROP DATABASE IF EXISTS clinical_lab_ops;
CREATE DATABASE clinical_lab_ops CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE clinical_lab_ops;


/* =====================================================================
   PART 2 - TABLES
   Order of creation matters: a table must exist before another table
   can point to it with a FOREIGN KEY.
   ===================================================================== */

-- 1. Hospital departments that send lab orders (Emergency, ICU, ...)
CREATE TABLE departments (
    department_id    INT AUTO_INCREMENT PRIMARY KEY,
    department_code  VARCHAR(10)  NOT NULL UNIQUE,
    department_name  VARCHAR(60)  NOT NULL,
    location         VARCHAR(60)  NOT NULL
);

-- 2. Physicians who order lab tests
CREATE TABLE physicians (
    physician_id     INT AUTO_INCREMENT PRIMARY KEY,
    physician_code   VARCHAR(12)  NOT NULL UNIQUE,
    first_name       VARCHAR(40)  NOT NULL,
    last_name        VARCHAR(40)  NOT NULL,
    specialty        VARCHAR(40)  NOT NULL,
    department_id    INT          NOT NULL,
    email            VARCHAR(100) NOT NULL UNIQUE,
    is_active        BOOLEAN      NOT NULL DEFAULT TRUE,
    CONSTRAINT fk_physician_department
        FOREIGN KEY (department_id) REFERENCES departments(department_id)
);

-- 3. Patients (DE-IDENTIFIED: no names, only a code + basic demographics)
CREATE TABLE patients (
    patient_id       INT AUTO_INCREMENT PRIMARY KEY,
    patient_mrn      VARCHAR(12)  NOT NULL UNIQUE,      -- medical record number (fake)
    birth_year       SMALLINT     NOT NULL,
    sex              CHAR(1)      NOT NULL,
    zip3             CHAR(3)      NOT NULL,             -- only first 3 ZIP digits
    insurance_type   VARCHAR(20)  NOT NULL,
    created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT chk_patient_sex  CHECK (sex IN ('F','M','U')),
    CONSTRAINT chk_birth_year   CHECK (birth_year BETWEEN 1900 AND 2026),
    CONSTRAINT chk_insurance    CHECK (insurance_type IN ('Commercial','Medicare','Medicaid','Self-Pay'))
);

-- 4. Test catalog: every lab test the lab can perform, with normal ranges
CREATE TABLE test_catalog (
    test_id          INT AUTO_INCREMENT PRIMARY KEY,
    test_code        VARCHAR(12)   NOT NULL UNIQUE,
    test_name        VARCHAR(60)   NOT NULL,
    category         VARCHAR(20)   NOT NULL,            -- Chemistry, Hematology, ...
    specimen_type    VARCHAR(20)   NOT NULL,            -- Serum, Whole Blood, ...
    unit             VARCHAR(15)   NOT NULL,
    ref_low          DECIMAL(10,2) NOT NULL,            -- normal range low
    ref_high         DECIMAL(10,2) NOT NULL,            -- normal range high
    critical_low     DECIMAL(10,2) NULL,                -- dangerously low
    critical_high    DECIMAL(10,2) NULL,                -- dangerously high
    tat_target_min   INT           NOT NULL,            -- turnaround-time target (minutes)
    price_usd        DECIMAL(8,2)  NOT NULL,
    is_active        BOOLEAN       NOT NULL DEFAULT TRUE,
    CONSTRAINT chk_ref_range   CHECK (ref_low <= ref_high),
    CONSTRAINT chk_price       CHECK (price_usd >= 0),
    CONSTRAINT chk_tat_target  CHECK (tat_target_min > 0),
    CONSTRAINT chk_category    CHECK (category IN ('Chemistry','Hematology','Coagulation','Immunoassay','Urinalysis'))
);

-- 5. Lab instruments (analyzers) that run the tests
CREATE TABLE instruments (
    instrument_id    INT AUTO_INCREMENT PRIMARY KEY,
    instrument_code  VARCHAR(12)  NOT NULL UNIQUE,
    instrument_name  VARCHAR(60)  NOT NULL,
    manufacturer     VARCHAR(40)  NOT NULL,
    category         VARCHAR(20)  NOT NULL,             -- which test category it runs
    install_date     DATE         NOT NULL,
    status           VARCHAR(15)  NOT NULL DEFAULT 'ACTIVE',
    CONSTRAINT chk_instrument_status CHECK (status IN ('ACTIVE','MAINTENANCE','RETIRED'))
);

-- 6. Lab orders: a physician orders tests for a patient
CREATE TABLE lab_orders (
    order_id         INT AUTO_INCREMENT PRIMARY KEY,
    order_number     VARCHAR(15)  NOT NULL UNIQUE,
    patient_id       INT          NOT NULL,
    physician_id     INT          NOT NULL,
    department_id    INT          NOT NULL,
    order_datetime   DATETIME     NOT NULL,
    priority         VARCHAR(10)  NOT NULL,
    order_status     VARCHAR(15)  NOT NULL DEFAULT 'ORDERED',
    created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_order_patient    FOREIGN KEY (patient_id)    REFERENCES patients(patient_id),
    CONSTRAINT fk_order_physician  FOREIGN KEY (physician_id)  REFERENCES physicians(physician_id),
    CONSTRAINT fk_order_department FOREIGN KEY (department_id) REFERENCES departments(department_id),
    CONSTRAINT chk_priority     CHECK (priority IN ('STAT','ROUTINE')),
    CONSTRAINT chk_order_status CHECK (order_status IN ('ORDERED','COLLECTED','IN_PROGRESS','COMPLETED','CANCELLED'))
);

-- 7. Samples (specimens): the physical tube collected for an order
CREATE TABLE samples (
    sample_id        INT AUTO_INCREMENT PRIMARY KEY,
    sample_barcode   VARCHAR(15)  NOT NULL UNIQUE,
    order_id         INT          NOT NULL,
    specimen_type    VARCHAR(20)  NOT NULL,
    collected_at     DATETIME     NOT NULL,
    received_at      DATETIME     NULL,                 -- when the lab received it
    sample_status    VARCHAR(12)  NOT NULL DEFAULT 'PENDING',
    rejection_reason VARCHAR(40)  NULL,
    updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_sample_order FOREIGN KEY (order_id) REFERENCES lab_orders(order_id),
    CONSTRAINT chk_sample_status CHECK (sample_status IN ('PENDING','RECEIVED','REJECTED'))
);

-- 8. Order tests: each individual test inside an order (an order can have several tests)
CREATE TABLE order_tests (
    order_test_id    INT AUTO_INCREMENT PRIMARY KEY,
    order_id         INT          NOT NULL,
    test_id          INT          NOT NULL,
    sample_id        INT          NULL,
    instrument_id    INT          NULL,
    test_status      VARCHAR(12)  NOT NULL DEFAULT 'PENDING',
    updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_ot_order      FOREIGN KEY (order_id)      REFERENCES lab_orders(order_id),
    CONSTRAINT fk_ot_test       FOREIGN KEY (test_id)       REFERENCES test_catalog(test_id),
    CONSTRAINT fk_ot_sample     FOREIGN KEY (sample_id)     REFERENCES samples(sample_id),
    CONSTRAINT fk_ot_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id),
    CONSTRAINT uq_order_test UNIQUE (order_id, test_id),          -- same test not twice in one order
    CONSTRAINT chk_test_status CHECK (test_status IN ('PENDING','COMPLETED','REJECTED','CANCELLED'))
);

-- 9. Test results: the measured value for one order test
CREATE TABLE test_results (
    result_id        INT AUTO_INCREMENT PRIMARY KEY,
    order_test_id    INT           NOT NULL UNIQUE,     -- one result per order test
    result_value     DECIMAL(10,2) NOT NULL,
    result_flag      VARCHAR(2)    NULL,                -- N, L, H, LL, HH (set by trigger)
    resulted_at      DATETIME      NOT NULL,
    verified_by      VARCHAR(30)   NOT NULL,
    updated_at       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_result_ordertest FOREIGN KEY (order_test_id) REFERENCES order_tests(order_test_id)
);

-- 10. Audit table: filled automatically by a trigger when an order status changes
CREATE TABLE order_status_audit (
    audit_id         INT AUTO_INCREMENT PRIMARY KEY,
    order_id         INT          NOT NULL,
    old_status       VARCHAR(15)  NOT NULL,
    new_status       VARCHAR(15)  NOT NULL,
    changed_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    changed_by       VARCHAR(80)  NOT NULL,
    CONSTRAINT fk_audit_order FOREIGN KEY (order_id) REFERENCES lab_orders(order_id)
);

-- Helpful indexes for the most common searches (by date, by patient)
CREATE INDEX idx_orders_datetime ON lab_orders(order_datetime);
CREATE INDEX idx_orders_patient  ON lab_orders(patient_id);
CREATE INDEX idx_results_time    ON test_results(resulted_at);


/* =====================================================================
   PART 3 - STORED FUNCTIONS (2)
   A function takes inputs and RETURNS one value. You can use it inside
   any SELECT, like a built-in function such as ROUND() or UPPER().
   ===================================================================== */
DELIMITER $$

-- Function 1: turnaround time (TAT) in minutes between two date-times
CREATE FUNCTION fn_tat_minutes(p_start DATETIME, p_end DATETIME)
RETURNS INT
DETERMINISTIC
NO SQL
BEGIN
    IF p_start IS NULL OR p_end IS NULL THEN
        RETURN NULL;
    END IF;
    RETURN TIMESTAMPDIFF(MINUTE, p_start, p_end);
END$$

-- Function 2: abnormal flag for a result value
--   HH = critically high, LL = critically low, H = high, L = low, N = normal
CREATE FUNCTION fn_result_flag(p_value DECIMAL(10,2),
                               p_ref_low DECIMAL(10,2),  p_ref_high DECIMAL(10,2),
                               p_crit_low DECIMAL(10,2), p_crit_high DECIMAL(10,2))
RETURNS VARCHAR(2)
DETERMINISTIC
NO SQL
BEGIN
    IF p_crit_high IS NOT NULL AND p_value >= p_crit_high THEN RETURN 'HH'; END IF;
    IF p_crit_low  IS NOT NULL AND p_value <= p_crit_low  THEN RETURN 'LL'; END IF;
    IF p_value > p_ref_high THEN RETURN 'H'; END IF;
    IF p_value < p_ref_low  THEN RETURN 'L'; END IF;
    RETURN 'N';
END$$


/* =====================================================================
   PART 4 - TRIGGERS (3)
   A trigger runs AUTOMATICALLY when a row is inserted/updated.
   ===================================================================== */

-- Trigger 1: block a sample whose "received" time is before its "collected" time
CREATE TRIGGER trg_samples_validate_times
BEFORE INSERT ON samples
FOR EACH ROW
BEGIN
    IF NEW.received_at IS NOT NULL AND NEW.received_at < NEW.collected_at THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Invalid sample: received_at cannot be earlier than collected_at';
    END IF;
END$$

-- Trigger 2: automatically set the abnormal flag (N / L / H / LL / HH)
--            on every new result, using the test's normal range
CREATE TRIGGER trg_results_set_flag
BEFORE INSERT ON test_results
FOR EACH ROW
BEGIN
    DECLARE v_low, v_high, v_clow, v_chigh DECIMAL(10,2);

    SELECT tc.ref_low, tc.ref_high, tc.critical_low, tc.critical_high
      INTO v_low, v_high, v_clow, v_chigh
      FROM order_tests ot
      JOIN test_catalog tc ON tc.test_id = ot.test_id
     WHERE ot.order_test_id = NEW.order_test_id;

    SET NEW.result_flag = fn_result_flag(NEW.result_value, v_low, v_high, v_clow, v_chigh);
END$$

-- Trigger 3: write an audit record every time an order's status changes
CREATE TRIGGER trg_orders_status_audit
AFTER UPDATE ON lab_orders
FOR EACH ROW
BEGIN
    IF OLD.order_status <> NEW.order_status THEN
        INSERT INTO order_status_audit (order_id, old_status, new_status, changed_at, changed_by)
        VALUES (NEW.order_id, OLD.order_status, NEW.order_status, NOW(), CURRENT_USER());
    END IF;
END$$


/* =====================================================================
   PART 5 - STORED PROCEDURES (4)
   A procedure is a saved program you run with CALL procedure_name(...).
   ===================================================================== */

-- Procedure 1: create a new lab order with one test (used for demos and
--              to show incremental loading later in the ETL)
CREATE PROCEDURE sp_create_lab_order(
    IN  p_patient_mrn   VARCHAR(12),
    IN  p_physician_code VARCHAR(12),
    IN  p_priority      VARCHAR(10),
    IN  p_test_code     VARCHAR(12),
    OUT p_new_order_id  INT
)
BEGIN
    DECLARE v_patient_id, v_physician_id, v_department_id, v_test_id INT;

    SELECT patient_id INTO v_patient_id FROM patients WHERE patient_mrn = p_patient_mrn;
    SELECT physician_id, department_id INTO v_physician_id, v_department_id
      FROM physicians WHERE physician_code = p_physician_code;
    SELECT test_id INTO v_test_id FROM test_catalog WHERE test_code = p_test_code;

    IF v_patient_id IS NULL OR v_physician_id IS NULL OR v_test_id IS NULL THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Unknown patient, physician or test code';
    END IF;

    INSERT INTO lab_orders (order_number, patient_id, physician_id, department_id,
                            order_datetime, priority, order_status)
    VALUES (CONCAT('TMP', LPAD(FLOOR(RAND() * 999999999), 9, '0')), v_patient_id, v_physician_id, v_department_id,
            NOW(), p_priority, 'ORDERED');

    SET p_new_order_id = LAST_INSERT_ID();
    UPDATE lab_orders SET order_number = CONCAT('ORD', LPAD(p_new_order_id, 7, '0'))
     WHERE order_id = p_new_order_id;

    INSERT INTO order_tests (order_id, test_id, test_status)
    VALUES (p_new_order_id, v_test_id, 'PENDING');
END$$

-- Procedure 2: change the status of an order (fires the audit trigger)
CREATE PROCEDURE sp_update_order_status(
    IN p_order_id   INT,
    IN p_new_status VARCHAR(15)
)
BEGIN
    IF NOT EXISTS (SELECT 1 FROM lab_orders WHERE order_id = p_order_id) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Order not found';
    END IF;
    UPDATE lab_orders SET order_status = p_new_status WHERE order_id = p_order_id;
END$$

-- Procedure 3: turnaround-time report for a date range
--   average minutes from "sample received" to "result ready", by category and priority,
--   compared with the target
CREATE PROCEDURE sp_tat_summary(
    IN p_start_date DATE,
    IN p_end_date   DATE
)
BEGIN
    SELECT tc.category,
           o.priority,
           COUNT(*)                                                    AS tests_resulted,
           ROUND(AVG(fn_tat_minutes(s.received_at, r.resulted_at)), 1) AS avg_tat_min,
           ROUND(AVG(tc.tat_target_min), 0)                            AS avg_target_min,
           ROUND(100 * SUM(fn_tat_minutes(s.received_at, r.resulted_at) <= tc.tat_target_min)
                 / COUNT(*), 1)                                        AS pct_within_target
      FROM test_results r
      JOIN order_tests  ot ON ot.order_test_id = r.order_test_id
      JOIN test_catalog tc ON tc.test_id       = ot.test_id
      JOIN samples      s  ON s.sample_id      = ot.sample_id
      JOIN lab_orders   o  ON o.order_id       = ot.order_id
     WHERE o.order_datetime >= p_start_date
       AND o.order_datetime <  DATE_ADD(p_end_date, INTERVAL 1 DAY)
     GROUP BY tc.category, o.priority
     ORDER BY tc.category, o.priority;
END$$

-- Procedure 4: generate realistic synthetic data
--   p_patients = how many patients, p_orders = how many lab orders
CREATE PROCEDURE sp_generate_sample_data(
    IN p_patients INT,
    IN p_orders   INT
)
BEGIN
    DECLARE i, k, v_ntests, v_patient, v_physician, v_dept, v_order_id, v_sample_id INT;
    DECLARE v_n_phys, v_n_pat INT;
    DECLARE v_order_dt, v_collected, v_received DATETIME;
    DECLARE v_priority VARCHAR(10);
    DECLARE v_specimen VARCHAR(20);
    DECLARE v_rand DOUBLE;

    START TRANSACTION;

    -- ---- 40 physicians --------------------------------------------------
    SET i = 1;
    WHILE i <= 40 DO
        INSERT INTO physicians (physician_code, first_name, last_name, specialty, department_id, email)
        VALUES (
            CONCAT('PHY', LPAD(i, 4, '0')),
            ELT(1 + MOD(i * 7, 20), 'Anita','Brian','Carla','David','Elena','Farid','Grace','Hiro',
                'Irene','Jamal','Kavya','Liam','Maya','Noah','Olivia','Pedro','Quinn','Ravi','Sara','Tom'),
            ELT(1 + MOD(i * 3, 20), 'Patel','Nguyen','Garcia','Smith','Kim','Johnson','Lee','Martinez',
                'Brown','Davis','Lopez','Wilson','Reddy','Chen','Clark','Lewis','Young','Hall','Allen','King'),
            ELT(1 + MOD(i, 8), 'Emergency Medicine','Critical Care','Internal Medicine','Cardiology',
                'Oncology','Pediatrics','Family Medicine','General Surgery'),
            1 + MOD(i, 8),
            CONCAT('phy', LPAD(i, 4, '0'), '@labhospital.example')
        );
        SET i = i + 1;
    END WHILE;

    -- ---- patients (de-identified) ---------------------------------------
    SET i = 1;
    WHILE i <= p_patients DO
        SET v_rand = RAND();
        INSERT INTO patients (patient_mrn, birth_year, sex, zip3, insurance_type, created_at)
        VALUES (
            CONCAT('MRN', LPAD(i, 6, '0')),
            1935 + FLOOR(RAND() * 88),
            IF(v_rand < 0.51, 'F', IF(v_rand < 0.99, 'M', 'U')),
            ELT(1 + FLOOR(RAND() * 6), '750','751','752','760','761','762'),
            ELT(1 + FLOOR(RAND() * 10), 'Commercial','Commercial','Commercial','Commercial',
                'Medicare','Medicare','Medicare','Medicaid','Medicaid','Self-Pay'),
            '2025-12-01' + INTERVAL FLOOR(RAND() * 300) DAY
        );
        SET i = i + 1;
    END WHILE;

    SELECT COUNT(*) INTO v_n_phys FROM physicians;
    SELECT COUNT(*) INTO v_n_pat  FROM patients;

    -- ---- lab orders, samples, order tests, results ----------------------
    SET i = 1;
    WHILE i <= p_orders DO
        SET v_patient   = 1 + FLOOR(RAND() * v_n_pat);
        SET v_physician = 1 + FLOOR(RAND() * v_n_phys);
        SELECT department_id INTO v_dept FROM physicians WHERE physician_id = v_physician;

        -- order date: Jan 1 - Oct 31, 2026, any time of day
        SET v_order_dt = TIMESTAMP('2026-01-01')
                         + INTERVAL FLOOR(RAND() * 304) DAY
                         + INTERVAL FLOOR(RAND() * 1440) MINUTE;

        -- Emergency (1) and ICU (2) order STAT much more often
        SET v_priority = IF(RAND() < IF(v_dept IN (1, 2), 0.65, 0.12), 'STAT', 'ROUTINE');

        INSERT INTO lab_orders (order_number, patient_id, physician_id, department_id,
                                order_datetime, priority, order_status, created_at)
        VALUES (CONCAT('ORD', LPAD(i, 7, '0')), v_patient, v_physician, v_dept,
                v_order_dt, v_priority, 'ORDERED', v_order_dt);
        SET v_order_id = LAST_INSERT_ID();

        SET v_rand = RAND();
        IF v_rand < 0.02 THEN
            -- 2% of orders are cancelled before collection
            UPDATE lab_orders SET order_status = 'CANCELLED' WHERE order_id = v_order_id;
            INSERT INTO order_tests (order_id, test_id, test_status)
            SELECT v_order_id, test_id, 'CANCELLED'
              FROM test_catalog ORDER BY RAND() LIMIT 1;
        ELSE
            -- pick one specimen type, then 1-3 tests that use that specimen
            SET v_specimen = ELT(1 + FLOOR(RAND() * 10), 'Serum','Serum','Serum','Serum',
                                 'Whole Blood','Whole Blood','Whole Blood','Plasma','Plasma','Urine');
            SET v_ntests = 1 + FLOOR(RAND() * 3);

            SET v_collected = v_order_dt + INTERVAL
                              IF(v_priority = 'STAT', 5 + FLOOR(RAND() * 15), 10 + FLOOR(RAND() * 80)) MINUTE;
            SET v_received  = v_collected + INTERVAL
                              IF(v_priority = 'STAT', 5 + FLOOR(RAND() * 20), 15 + FLOOR(RAND() * 75)) MINUTE;

            INSERT INTO samples (sample_barcode, order_id, specimen_type, collected_at,
                                 received_at, sample_status, rejection_reason)
            VALUES (CONCAT('SMP', LPAD(i, 7, '0')), v_order_id, v_specimen, v_collected,
                    v_received, 'RECEIVED', NULL);
            SET v_sample_id = LAST_INSERT_ID();

            -- tests for this order; instrument chosen from the matching category
            INSERT INTO order_tests (order_id, test_id, sample_id, instrument_id, test_status)
            SELECT v_order_id, t.test_id, v_sample_id,
                   (SELECT ins.instrument_id FROM instruments ins
                     WHERE ins.category = t.category ORDER BY RAND() LIMIT 1),
                   'PENDING'
              FROM (SELECT test_id, category FROM test_catalog
                     WHERE specimen_type = v_specimen
                     ORDER BY RAND() LIMIT v_ntests) t;

            SET v_rand = RAND();
            IF v_rand < 0.035 THEN
                -- 3.5% of samples are rejected by the lab
                UPDATE samples
                   SET sample_status = 'REJECTED',
                       rejection_reason = ELT(1 + FLOOR(RAND() * 5), 'Hemolyzed','Clotted',
                                              'Insufficient volume','Mislabeled','Wrong container')
                 WHERE sample_id = v_sample_id;
                UPDATE order_tests SET test_status = 'REJECTED' WHERE order_id = v_order_id;
                UPDATE lab_orders  SET order_status = 'CANCELLED' WHERE order_id = v_order_id;

            ELSEIF v_order_dt >= '2026-10-31' THEN
                -- orders from the last day are still being worked on
                UPDATE lab_orders SET order_status = 'IN_PROGRESS' WHERE order_id = v_order_id;

            ELSE
                -- results: processing time depends on category, priority, night shift,
                -- and instrument CHEM-02 was slow (failing) in Jul-Sep 2026
                INSERT INTO test_results (order_test_id, result_value, resulted_at, verified_by)
                SELECT ot.order_test_id,
                       GREATEST(0, ROUND(
                           (tc.ref_low + tc.ref_high) / 2
                           + (RAND() + RAND() + RAND() + RAND() - 2) * (tc.ref_high - tc.ref_low) * 0.55
                           + IF(RAND() < 0.12, (tc.ref_high - tc.ref_low) * (0.6 + RAND()), 0)
                         , 2)),
                       v_received + INTERVAL ROUND(
                           (CASE tc.category
                                WHEN 'Chemistry'   THEN 25 + RAND() * 45
                                WHEN 'Hematology'  THEN 12 + RAND() * 25
                                WHEN 'Coagulation' THEN 20 + RAND() * 35
                                WHEN 'Immunoassay' THEN 50 + RAND() * 90
                                ELSE                    15 + RAND() * 30 END)
                           * IF(v_priority = 'STAT', 0.6, 1.0)
                           * IF(HOUR(v_received) >= 22 OR HOUR(v_received) < 6, 1.25, 1.0)
                           + IF(ins.instrument_code = 'CHEM-02'
                                AND v_received >= '2026-07-01' AND v_received < '2026-10-01',
                                40 + RAND() * 110, 0)
                       ) MINUTE,
                       CONCAT('TECH', LPAD(1 + FLOOR(RAND() * 15), 2, '0'))
                  FROM order_tests ot
                  JOIN test_catalog tc  ON tc.test_id = ot.test_id
                  JOIN instruments  ins ON ins.instrument_id = ot.instrument_id
                 WHERE ot.order_id = v_order_id;

                UPDATE order_tests SET test_status = 'COMPLETED' WHERE order_id = v_order_id;
                UPDATE lab_orders  SET order_status = 'COMPLETED' WHERE order_id = v_order_id;
            END IF;
        END IF;

        SET i = i + 1;
    END WHILE;

    -- the generator's status changes are not "real" user actions, so clear the audit log
    DELETE FROM order_status_audit WHERE audit_id > 0;
    ALTER TABLE order_status_audit AUTO_INCREMENT = 1;

    COMMIT;
END$$

DELIMITER ;


/* =====================================================================
   PART 6 - FIXED REFERENCE DATA
   ===================================================================== */

INSERT INTO departments (department_code, department_name, location) VALUES
('ED',   'Emergency Department',   'Building A - Floor 1'),
('ICU',  'Intensive Care Unit',    'Building A - Floor 3'),
('IM',   'Internal Medicine',      'Building B - Floor 2'),
('CARD', 'Cardiology',             'Building B - Floor 4'),
('ONC',  'Oncology',               'Building C - Floor 2'),
('PED',  'Pediatrics',             'Building C - Floor 1'),
('OPC',  'Outpatient Clinic',      'Building D - Floor 1'),
('SURG', 'Surgery',                'Building A - Floor 2');

INSERT INTO instruments (instrument_code, instrument_name, manufacturer, category, install_date, status) VALUES
('CHEM-01', 'Chemistry Analyzer 1',     'Roche',     'Chemistry',   '2021-03-15', 'ACTIVE'),
('CHEM-02', 'Chemistry Analyzer 2',     'Roche',     'Chemistry',   '2019-08-01', 'ACTIVE'),
('HEME-01', 'Hematology Analyzer 1',    'Sysmex',    'Hematology',  '2022-01-10', 'ACTIVE'),
('HEME-02', 'Hematology Analyzer 2',    'Sysmex',    'Hematology',  '2020-06-20', 'ACTIVE'),
('COAG-01', 'Coagulation Analyzer',     'Stago',     'Coagulation', '2021-11-05', 'ACTIVE'),
('IMMU-01', 'Immunoassay Analyzer 1',   'Abbott',    'Immunoassay', '2022-09-12', 'ACTIVE'),
('IMMU-02', 'Immunoassay Analyzer 2',   'Abbott',    'Immunoassay', '2018-04-30', 'ACTIVE'),
('URIN-01', 'Urinalysis Analyzer',      'Siemens',   'Urinalysis',  '2023-02-01', 'ACTIVE');

INSERT INTO test_catalog (test_code, test_name, category, specimen_type, unit,
                          ref_low, ref_high, critical_low, critical_high, tat_target_min, price_usd) VALUES
('GLU',   'Glucose',                     'Chemistry',   'Serum',       'mg/dL',   70,    99,    40,    450,  60,  12.00),
('NA',    'Sodium',                      'Chemistry',   'Serum',       'mmol/L',  135,   145,   120,   160,  60,  10.00),
('K',     'Potassium',                   'Chemistry',   'Serum',       'mmol/L',  3.5,   5.1,   2.5,   6.5,  60,  10.00),
('CREA',  'Creatinine',                  'Chemistry',   'Serum',       'mg/dL',   0.6,   1.3,   NULL,  10,   60,  14.00),
('BUN',   'Blood Urea Nitrogen',         'Chemistry',   'Serum',       'mg/dL',   7,     20,    NULL,  100,  60,  11.00),
('ALT',   'Alanine Aminotransferase',    'Chemistry',   'Serum',       'U/L',     7,     56,    NULL,  1000, 90,  15.00),
('AST',   'Aspartate Aminotransferase',  'Chemistry',   'Serum',       'U/L',     10,    40,    NULL,  1000, 90,  15.00),
('CHOL',  'Total Cholesterol',           'Chemistry',   'Serum',       'mg/dL',   100,   199,   NULL,  NULL, 120, 18.00),
('CRP',   'C-Reactive Protein',          'Chemistry',   'Serum',       'mg/L',    0,     10,    NULL,  NULL, 90,  20.00),
('LACT',  'Lactate',                     'Chemistry',   'Plasma',      'mmol/L',  0.5,   2.2,   NULL,  4,    45,  22.00),
('HGB',   'Hemoglobin',                  'Hematology',  'Whole Blood', 'g/dL',    12,    17.5,  7,     20,   45,  9.00),
('WBC',   'White Blood Cell Count',      'Hematology',  'Whole Blood', 'K/uL',    4.5,   11,    2,     30,   45,  9.00),
('PLT',   'Platelet Count',              'Hematology',  'Whole Blood', 'K/uL',    150,   450,   50,    1000, 45,  9.00),
('HBA1C', 'Hemoglobin A1c',              'Hematology',  'Whole Blood', '%',       4,     5.6,   NULL,  NULL, 240, 25.00),
('PT',    'Prothrombin Time',            'Coagulation', 'Plasma',      'sec',     11,    13.5,  NULL,  30,   60,  16.00),
('INR',   'International Normalized Ratio','Coagulation','Plasma',     'ratio',   0.8,   1.2,   NULL,  5,    60,  16.00),
('TROP',  'Troponin I',                  'Immunoassay', 'Plasma',      'ng/mL',   0,     0.04,  NULL,  0.5,  60,  45.00),
('TSH',   'Thyroid Stimulating Hormone', 'Immunoassay', 'Serum',       'mIU/L',   0.4,   4.0,   NULL,  NULL, 240, 35.00),
('VITD',  'Vitamin D, 25-Hydroxy',       'Immunoassay', 'Serum',       'ng/mL',   30,    100,   NULL,  NULL, 480, 55.00),
('UA-PH', 'Urinalysis pH',               'Urinalysis',  'Urine',       'pH',      4.5,   8,     NULL,  NULL, 60,  8.00),
('UA-SG', 'Urine Specific Gravity',      'Urinalysis',  'Urine',       'ratio',   1.005, 1.030, NULL,  NULL, 60,  8.00);


/* =====================================================================
   PART 7 - GENERATE SYNTHETIC DATA
   1,200 patients and 2,500 lab orders (takes about 10-60 seconds).
   ===================================================================== */
CALL sp_generate_sample_data(1200, 2500);


/* =====================================================================
   PART 8 - CHECK: how many rows are in each table?
   ===================================================================== */
SELECT 'departments'        AS table_name, COUNT(*) AS row_count FROM departments
UNION ALL SELECT 'physicians',         COUNT(*) FROM physicians
UNION ALL SELECT 'patients',           COUNT(*) FROM patients
UNION ALL SELECT 'test_catalog',       COUNT(*) FROM test_catalog
UNION ALL SELECT 'instruments',        COUNT(*) FROM instruments
UNION ALL SELECT 'lab_orders',         COUNT(*) FROM lab_orders
UNION ALL SELECT 'samples',            COUNT(*) FROM samples
UNION ALL SELECT 'order_tests',        COUNT(*) FROM order_tests
UNION ALL SELECT 'test_results',       COUNT(*) FROM test_results
UNION ALL SELECT 'order_status_audit', COUNT(*) FROM order_status_audit
UNION ALL SELECT '>>> TOTAL',
       (SELECT COUNT(*) FROM departments) + (SELECT COUNT(*) FROM physicians)
     + (SELECT COUNT(*) FROM patients)    + (SELECT COUNT(*) FROM test_catalog)
     + (SELECT COUNT(*) FROM instruments) + (SELECT COUNT(*) FROM lab_orders)
     + (SELECT COUNT(*) FROM samples)     + (SELECT COUNT(*) FROM order_tests)
     + (SELECT COUNT(*) FROM test_results);
