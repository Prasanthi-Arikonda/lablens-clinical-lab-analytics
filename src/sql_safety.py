"""
sql_safety.py  -  Module 6: check AI-generated SQL BEFORE it is executed

The handbook rule: "Do not execute unrestricted model output."
Every query Llama writes must pass ALL of these checks:

  1. exactly ONE statement (no "SELECT ...; DROP TABLE ...")
  2. it must be a read-only SELECT (or WITH ... SELECT)
  3. no data-changing or dangerous keywords (INSERT, UPDATE, DELETE, DROP, ALTER, CREATE,
     TRUNCATE, GRANT, COPY, ...) and no dangerous functions (pg_sleep, pg_read_file, ...)
  4. it may only use the approved AI views in the dw schema
  5. a row limit is always added (max 200 rows)

The database adds two more layers on its own: the query runs as lab_ai_reader
(read-only account, SELECT on dw only) with a 5-second statement timeout.
"""
import re

import sqlparse

ALLOWED_TABLES = {"ai_lab_tests", "ai_instrument_events", "ai_staffing"}
MAX_ROWS = 200

FORBIDDEN_KEYWORDS = [
    "insert", "update", "delete", "merge", "upsert", "drop", "alter", "create", "truncate",
    "grant", "revoke", "copy", "call", "do", "execute", "prepare", "deallocate", "vacuum",
    "analyze", "cluster", "reindex", "refresh", "comment", "security", "set", "reset",
    "listen", "notify", "lock", "begin", "commit", "rollback", "savepoint", "into",
]
FORBIDDEN_FUNCTIONS = [
    "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "dblink", "set_config", "pg_terminate_backend",
    "pg_cancel_backend", "current_setting", "query_to_xml", "pg_reload_conf",
]


def extract_sql(text):
    """Pull the SQL out of Llama's reply (it sometimes wraps it in ```sql ... ``` or adds words)."""
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", text, flags=re.S | re.I)
    if fenced:
        text = fenced.group(1)
    match = re.search(r"\b(with|select)\b.*", text, flags=re.S | re.I)
    sql = match.group(0) if match else text
    return sql.strip().rstrip(";").strip()


def _strip_strings_and_comments(sql):
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def validate_sql(sql):
    """Return (is_safe, safe_sql_to_run, reason)."""
    if not sql or not sql.strip():
        return False, None, "BLOCKED: empty query"
    statements = [s for s in sqlparse.split(sql) if s.strip().strip(";").strip()]
    if len(statements) != 1:
        return False, None, "BLOCKED: more than one SQL statement"
    sql = statements[0].strip().rstrip(";").strip()

    code = _strip_strings_and_comments(sql).lower()
    if ";" in code:
        return False, None, "BLOCKED: more than one SQL statement"
    first_word = re.match(r"\s*(\w+)", code)
    if not first_word or first_word.group(1) not in ("select", "with"):
        return False, None, "BLOCKED: only SELECT queries are allowed"
    for word in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{word}\b", code):
            return False, None, f"BLOCKED: forbidden keyword '{word.upper()}'"
    for func in FORBIDDEN_FUNCTIONS:
        if func in code:
            return False, None, f"BLOCKED: forbidden function '{func}'"

    # functions like EXTRACT(MONTH FROM order_date) also contain the word FROM - ignore them
    code = re.sub(r"\b(extract|substring|trim|overlay|position)\s*\([^()]*\)", " fn() ", code)

    # every table after FROM / JOIN must be an approved AI view
    cte_names = set(re.findall(r"\b(\w+)\s+as\s*\(", code))
    referenced = re.findall(r"\b(?:from|join)\s+([a-z_][\w\.\"]*)", code)
    for ref in referenced:
        name = ref.replace('"', "")
        schema, _, table = name.rpartition(".")
        if table in cte_names and not schema:
            continue
        if schema not in ("", "dw") or table not in ALLOWED_TABLES:
            return False, None, f"BLOCKED: table '{name}' is not an approved AI view"
    if not referenced:
        return False, None, "BLOCKED: query does not read any approved table"

    safe_sql = f"SELECT * FROM (\n{sql}\n) AS ai_query\nLIMIT {MAX_ROWS}"
    return True, safe_sql, "ALLOWED: read-only SELECT on approved views"
