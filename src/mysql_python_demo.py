"""
mysql_python_demo.py  -  Module 1: Python connectivity to MySQL

Shows that Python can RETRIEVE, INSERT and UPDATE records in the
clinical_lab_ops MySQL database (and call a stored procedure).

How to run (from the project folder, with .venv switched on):
    python src/mysql_python_demo.py
"""
from datetime import datetime

import pandas as pd

from connectors import get_mysql_connection, mysql_query


def show(title, columns, rows):
    """Print a small table nicely."""
    print(f"\n=== {title} ===")
    print(pd.DataFrame(rows, columns=columns).to_string(index=False))


def main():
    # ------------------------------------------------------------------
    # 1. RETRIEVE: read data with a multi-table query
    # ------------------------------------------------------------------
    cols, rows = mysql_query("""
        SELECT tc.test_name, tc.category, COUNT(*) AS times_ordered
          FROM order_tests ot
          JOIN test_catalog tc ON tc.test_id = ot.test_id
         GROUP BY tc.test_name, tc.category
         ORDER BY times_ordered DESC
         LIMIT 5
    """)
    show("RETRIEVE: top 5 most-ordered tests", cols, rows)

    conn = get_mysql_connection()
    cur = conn.cursor()
    try:
        # --------------------------------------------------------------
        # 2. INSERT: add a new (fake) patient.
        #    %s placeholders = parameterized query -> safe from SQL injection
        # --------------------------------------------------------------
        new_mrn = "PY" + datetime.now().strftime("%m%d%H%M%S")   # unique each run
        cur.execute(
            """INSERT INTO patients (patient_mrn, birth_year, sex, zip3, insurance_type)
               VALUES (%s, %s, %s, %s, %s)""",
            (new_mrn, 1988, "F", "750", "Commercial"),
        )
        conn.commit()
        print(f"\n=== INSERT: new patient {new_mrn} added (patient_id = {cur.lastrowid}) ===")

        # --------------------------------------------------------------
        # 3. CALL A STORED PROCEDURE: create a STAT potassium order for that patient
        # --------------------------------------------------------------
        result = cur.callproc("sp_create_lab_order", (new_mrn, "PHY0002", "STAT", "K", 0))
        new_order_id = result[4]          # the OUT parameter
        conn.commit()
        print(f"=== PROCEDURE: sp_create_lab_order created order_id = {new_order_id} ===")

        # --------------------------------------------------------------
        # 4. UPDATE: change the patient's insurance, and the order's status
        # --------------------------------------------------------------
        cur.execute("UPDATE patients SET insurance_type = %s WHERE patient_mrn = %s",
                    ("Medicare", new_mrn))
        cur.callproc("sp_update_order_status", (new_order_id, "COLLECTED"))
        conn.commit()
        print("=== UPDATE: insurance changed to Medicare, order status changed to COLLECTED ===")
    finally:
        cur.close()
        conn.close()

    # ------------------------------------------------------------------
    # 5. RETRIEVE again to prove the changes were saved
    # ------------------------------------------------------------------
    cols, rows = mysql_query(
        """SELECT p.patient_mrn, p.insurance_type, o.order_number, o.priority, o.order_status
             FROM patients p JOIN lab_orders o ON o.patient_id = p.patient_id
            WHERE p.patient_mrn = %s""", (new_mrn,))
    show("VERIFY: the new patient and order", cols, rows)

    cols, rows = mysql_query(
        "SELECT * FROM order_status_audit WHERE order_id = %s", (new_order_id,))
    show("VERIFY: audit trigger recorded the status change", cols, rows)

    print("\nMySQL Python connectivity demo finished successfully.")


if __name__ == "__main__":
    main()