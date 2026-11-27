"""
nl2sql.py  -  Module 6: natural-language-to-SQL with Llama (schema-aware, validated, read-only)

  question  ->  Llama writes SQL (it is given the schema of the approved AI views)
            ->  sql_safety.validate_sql()  (blocked if unsafe)
            ->  executed as lab_ai_reader  (read-only account, 5-second timeout, max 200 rows)
            ->  Llama explains the result in plain English
            ->  everything is logged in ai.query_log
"""
import re
import time
from datetime import date

import pandas as pd

import llm
from connectors import get_pg_connection
from sql_safety import extract_sql, validate_sql

# Requests to CHANGE data are refused before Llama is even asked (LabLens is read-only).
WRITE_INTENT = re.compile(
    r"\b(delete|truncate|erase|wipe|purge|insert|alter|overwrite|drop\s+(the\s+)?table)\b"
    r"|^\s*(please\s+)?((can|could|would)\s+you\s+)?(please\s+)?"
    r"(update|change|modify|edit|set|add|create|remove|drop|replace)\b",
    re.IGNORECASE)

SCHEMA_CONTEXT = """You can query ONLY these three PostgreSQL views (schema dw):

1) ai_lab_tests  -- one row per ordered lab test (Jan-Oct 2026, plus a few November demo orders)
   order_number, order_date (date), year, quarter (1-4), month (1-12), year_month ('2026-07'), day_name,
   test_code, test_name, test_category ('Chemistry','Hematology','Coagulation','Immunoassay','Urinalysis'),
   instrument ('CHEM-01','CHEM-02','HEME-01','HEME-02','COAG-01','IMMU-01','IMMU-02','URIN-01'),
   department ('Emergency Department','Intensive Care Unit','Internal Medicine','Cardiology','Oncology',
               'Pediatrics','Outpatient Clinic','Surgery'),
   physician_name, insurance_type ('Commercial','Medicare','Medicaid','Self-Pay'), age_group, patient_sex,
   shift ('Day','Evening','Night'), priority ('STAT','ROUTINE'),
   test_status ('COMPLETED','PENDING','REJECTED','CANCELLED'), sample_status ('RECEIVED','REJECTED'),
   rejection_reason, transport_method ('Pneumatic tube','Courier','Hand delivery'),
   transport_minutes, tat_minutes (turnaround time: sample received -> result), tat_target_minutes,
   met_tat_target (boolean), result_value, result_flag ('N','L','H','LL','HH'),
   is_abnormal (boolean), is_critical (boolean), revenue_usd
   NOTE: a rejected sample has sample_status = 'REJECTED'. Each row is one ordered test.

2) ai_instrument_events  -- one row per analyzer event from the equipment logs
   instrument, instrument_category, event_time (timestamp), event_date, year, quarter, month, year_month,
   event_type ('QC_RUN','ERROR','MAINTENANCE'), severity, error_code, error_message,
   downtime_minutes, qc_passed (boolean), qc_rule_violated

3) ai_staffing  -- one row per day and shift
   work_date, year, quarter, month, year_month, day_name, shift ('Day','Evening','Night'),
   techs_scheduled, techs_present, call_outs, overtime_hours, staffing_pct
"""

RULES_FOR_RATES = """How to calculate a rate or percentage (very important):
- Use ALL the rows of each group as the denominator:
  ROUND(100.0 * AVG(CASE WHEN <condition> THEN 1 ELSE 0 END), 2)
- Do NOT put the condition in the WHERE clause when calculating its rate (that would make every rate 100%).
- Never divide COUNT(DISTINCT some_text_column) by another count.
How to choose SUM, COUNT or AVG (very important):
- Revenue -> SUM(revenue_usd). Number of tests or events -> COUNT(*). Downtime -> SUM(downtime_minutes).
- Turnaround time is ALWAYS an average -> ROUND(AVG(tat_minutes)::numeric, 1), grouped by what the
  question compares (shift, department, month, instrument ...). Never SUM turnaround times.
- Never multiply a sum, count or average by 100. Use percentages ONLY for rates, shares or percentages.
"""

EXAMPLES = """Examples:
Q: What is the abnormal result rate for each test?
SQL: SELECT test_name, ROUND(100.0 * AVG(CASE WHEN is_abnormal THEN 1 ELSE 0 END), 2) AS abnormal_rate_pct FROM ai_lab_tests WHERE result_value IS NOT NULL GROUP BY test_name ORDER BY abnormal_rate_pct DESC
Q: What is the average turnaround time by quarter?
SQL: SELECT quarter, ROUND(AVG(tat_minutes), 1) AS avg_tat_minutes FROM ai_lab_tests WHERE tat_minutes IS NOT NULL GROUP BY quarter ORDER BY quarter
Q: Which instrument had the most downtime in Q3?
SQL: SELECT instrument, SUM(downtime_minutes) AS total_downtime_minutes FROM ai_instrument_events WHERE quarter = 3 AND event_type = 'ERROR' GROUP BY instrument ORDER BY total_downtime_minutes DESC
Q: What is the total revenue for each insurance type?
SQL: SELECT insurance_type, ROUND(SUM(revenue_usd)::numeric, 2) AS total_revenue_usd FROM ai_lab_tests GROUP BY insurance_type ORDER BY total_revenue_usd DESC
Q: What percentage of STAT tests met the target in each department?
SQL: SELECT department, ROUND(100.0 * AVG(CASE WHEN met_tat_target THEN 1 ELSE 0 END), 1) AS pct_on_target FROM ai_lab_tests WHERE priority = 'STAT' AND met_tat_target IS NOT NULL GROUP BY department ORDER BY pct_on_target
"""


def _system_prompt():
    return f"""You are a PostgreSQL expert for a hospital clinical laboratory.
Write ONE read-only PostgreSQL SELECT query that answers the user's question.
Rules:
- Use only the views and columns listed below. Never invent columns.
- PostgreSQL syntax only. Use ROUND(x::numeric, 1) for decimals.
- Today is {date.today():%Y-%m-%d}. "Last quarter" means quarter 3 of 2026. The data covers 2026.
- Return ONLY the SQL query, with no explanation.

{SCHEMA_CONTEXT}
{RULES_FOR_RATES}
{EXAMPLES}"""


def run_readonly(sql):
    """Execute validated SQL with the READ-ONLY account."""
    with get_pg_connection(read_only=True) as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(sql)
        cols = [c.name for c in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def log_query(feature, question, sql, validation, status, rows, seconds, answer):
    try:
        with get_pg_connection() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO ai.query_log (feature, question, generated_sql, validation, status,
                                                    row_count, duration_sec, answer)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (feature, question, sql, validation, status, rows, seconds, (answer or "")[:4000]))
    except Exception:
        pass   # logging must never break the assistant


def explain_result(question, sql, df):
    if df.empty:
        return "The query ran successfully but returned no rows for this question."
    table = df.head(20).to_string(index=False)
    system = ("You explain database results to a hospital laboratory manager. "
              "Use ONLY the numbers in the result table and describe each column exactly as it is named. "
              "Do not guess causes. "
              "Answer in 2-4 short sentences of plain English.")
    user = f"Question: {question}\n\nSQL used:\n{sql}\n\nResult table:\n{table}"
    text, _ = llm.chat(system, user, max_tokens=250)
    return text


def ask(question, explain=True):
    """Full NL-to-SQL flow. Returns a dict the app (or the terminal demo) can display."""
    start = time.time()
    out = {"question": question, "sql": None, "validation": None, "status": None,
           "data": pd.DataFrame(), "explanation": None, "attempts": 0}
    if not question or len(question.strip()) < 5:
        out.update(status="INVALID_QUESTION", validation="Please ask a complete business question.")
        return out
    if WRITE_INTENT.search(question):
        out.update(status="BLOCKED", validation="LabLens is read-only: it can look at data but can never "
                                                "delete, change or add records")
        out["seconds"] = round(time.time() - start, 1)
        log_query("NL2SQL", question, None, out["validation"], out["status"], 0, out["seconds"], None)
        return out

    user_msg = f"Question: {question}\nSQL:"
    last_error = None
    for attempt in (1, 2):                       # one automatic retry if the SQL fails
        out["attempts"] = attempt
        if last_error:
            user_msg = (f"Question: {question}\nYour previous SQL failed with this error:\n{last_error}\n"
                        f"Write a corrected SQL query.\nSQL:")
        reply, _ = llm.chat(_system_prompt(), user_msg, max_tokens=400)
        sql = extract_sql(reply)
        out["sql"] = sql
        ok, safe_sql, reason = validate_sql(sql)
        out["validation"] = reason
        if not ok:
            out["status"] = "BLOCKED"
            break
        try:
            out["data"] = run_readonly(safe_sql)
            out["status"] = "SUCCESS"
            break
        except Exception as exc:
            last_error = str(exc).split("\n")[0]
            out["status"] = "SQL_ERROR"
            out["validation"] = f"{reason} | database error: {last_error}"

    if out["status"] == "SUCCESS" and explain:
        out["explanation"] = explain_result(question, out["sql"], out["data"])
    out["seconds"] = round(time.time() - start, 1)
    log_query("NL2SQL", question, out["sql"], out["validation"], out["status"],
              len(out["data"]), out["seconds"], out["explanation"])
    return out


def run_user_sql(sql, question="(SQL typed directly)"):
    """Validate and run a query typed by a person (used to DEMONSTRATE that unsafe SQL is blocked)."""
    ok, safe_sql, reason = validate_sql(extract_sql(sql) if sql.strip().lower().startswith(("select", "with")) else sql)
    status, df = ("BLOCKED", pd.DataFrame())
    if ok:
        try:
            df, status = run_readonly(safe_sql), "SUCCESS"
        except Exception as exc:
            status, reason = "SQL_ERROR", f"{reason} | database error: {str(exc).splitlines()[0]}"
    log_query("NL2SQL", question, sql, reason, status, len(df), 0, None)
    return {"sql": sql, "validation": reason, "status": status, "data": df}
