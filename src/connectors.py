"""
connectors.py  -  Reusable database connection functions (used by every part of the project)

Clinical Laboratory Data Engineering & Analytics - Prasanthi Arikonda (dal574888)

Passwords are NOT written here. They are read from the hidden .env file
in the project folder (python-dotenv loads it).
"""
import os
from pathlib import Path

from dotenv import load_dotenv
import mysql.connector
from pymongo import MongoClient

# Find the project folder (one level above this "src" folder) and load .env from it
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


# ---------------------------------------------------------------------------
# MySQL  (operational lab database: clinical_lab_ops)
# ---------------------------------------------------------------------------
def get_mysql_connection():
    """Open and return a connection to the MySQL operational database."""
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "clinical_lab_ops"),
    )


def mysql_query(sql, params=None):
    """Run a SELECT on MySQL and return (column_names, rows)."""
    conn = get_mysql_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        columns = [c[0] for c in cur.description]
        rows = cur.fetchall()
        return columns, rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# MongoDB  (document database: clinical_lab_docs)
# ---------------------------------------------------------------------------
def get_mongo_db():
    """Return the MongoDB database object (collections are reached like db["equipment_logs"])."""
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                         serverSelectionTimeoutMS=5000)
    return client[os.getenv("MONGO_DATABASE", "clinical_lab_docs")]

# ---------------------------------------------------------------------------
# PostgreSQL  (analytics warehouse: clinical_lab_dw)
# ---------------------------------------------------------------------------
def get_pg_connection(read_only=False):
    """Open a connection to the PostgreSQL warehouse.

    read_only=False -> the ETL owner account (can create and load tables)
    read_only=True  -> the lab_ai_reader account (SELECT only; used by the AI assistant)
    """
    import psycopg  # imported here so MySQL/Mongo-only scripts do not need it

    user = os.getenv("PG_READONLY_USER", "lab_ai_reader") if read_only else os.getenv("PG_USER")
    password = os.getenv("PG_READONLY_PASSWORD") if read_only else os.getenv("PG_PASSWORD")
    return psycopg.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DATABASE", "clinical_lab_dw"),
        user=user,
        password=password or None,
    )