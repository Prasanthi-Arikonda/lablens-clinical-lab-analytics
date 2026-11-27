"""
ai_demo.py  -  Module 6: run the Llama features from the Terminal (proof + testing)

    python src/ai_demo.py index                     build the RAG index (run once, and after new documents)
    python src/ai_demo.py sql "your question"       natural language -> SQL -> result -> explanation
    python src/ai_demo.py safety                    show that unsafe SQL is BLOCKED
    python src/ai_demo.py rag "your question"       answer from documents, with sources
    python src/ai_demo.py insight ["question"]      evidence-based management briefing
"""
import sys

import pandas as pd

pd.set_option("display.width", 160)
pd.set_option("display.max_columns", 20)


def line(title):
    print("\n" + "=" * 90 + f"\n{title}\n" + "=" * 90)


def cmd_index():
    import rag
    line("Building the RAG index: MongoDB documents -> embeddings -> PostgreSQL (pgvector)")
    print(rag.build_index())


def cmd_sql(question):
    import nl2sql
    line(f"QUESTION: {question}")
    r = nl2sql.ask(question)
    print(f"\nGenerated SQL (attempt {r['attempts']}):\n{r['sql']}\n")
    print(f"Validation : {r['validation']}")
    print(f"Status     : {r['status']}   ({r.get('seconds', '?')} s)")
    if r["status"] == "SUCCESS":
        print(f"\nResult ({len(r['data'])} rows):\n{r['data'].head(15).to_string(index=False)}")
        print(f"\nExplanation:\n{r['explanation']}")


def cmd_safety():
    import nl2sql
    line("SQL SAFETY TEST - each statement goes through the same validator Llama's SQL uses")
    tests = [
        "SELECT instrument, COUNT(*) AS errors FROM ai_instrument_events WHERE event_type = 'ERROR' GROUP BY instrument",
        "DELETE FROM dw.fact_lab_test",
        "DROP TABLE dw.dim_patient",
        "SELECT * FROM ai_lab_tests; DROP TABLE dw.dim_test",
        "UPDATE dw.ai_lab_tests SET revenue_usd = 0",
        "SELECT * FROM etl.run_log",
        "SELECT * FROM dw.fact_lab_test",
        "SELECT pg_sleep(30)",
        "SELECT usename, passwd FROM pg_catalog.pg_shadow",
    ]
    for sql in tests:
        r = nl2sql.run_user_sql(sql, question="safety test")
        print(f"\n{r['status']:<8} {sql}\n         -> {r['validation']}")


def cmd_rag(question):
    import rag
    line(f"DOCUMENT QUESTION: {question}")
    r = rag.answer(question)
    print(f"\nAnswer ({r.get('seconds', '?')} s):\n{r['answer']}\n\nSources:")
    for i, s in enumerate(r["sources"], start=1):
        print(f"  [{i}] {s['title']} - {s['section']}  (similarity {s['similarity']})")


def cmd_insight(question):
    import insights
    line(f"MANAGEMENT QUESTION: {question}")
    r = insights.generate_insight(question)
    print("\nEvidence used:")
    for key, (name, _) in r["facts"].items():
        print(f"  [{key}] {name}")
    print(f"  [M1] {r['model_note']}")
    for i, d in enumerate(r["documents"], start=1):
        print(f"  [D{i}] {d['title']} - {d['section']}")
    print(f"\nBriefing ({r['seconds']} s):\n{r['briefing']}")
    check = ", ".join(r["unsupported_numbers"]) if r["unsupported_numbers"] else "none (good: the facts come only from the database)"
    print(f"\nGrounding check - numbers written by the AI itself: {check}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    command, rest = sys.argv[1], " ".join(sys.argv[2:])
    if command == "index":
        cmd_index()
    elif command == "sql":
        cmd_sql(rest or "Which instrument had the most downtime in quarter 3?")
    elif command == "safety":
        cmd_safety()
    elif command == "rag":
        cmd_rag(rest or "What should staff do when a chemistry analyzer shows error E-417?")
    elif command == "insight":
        cmd_insight(rest or "Why did laboratory turnaround time get worse last quarter?")
    else:
        print(__doc__)
