"""
analytics.py  -  Module 5: reusable analytics and machine-learning functions

Used by the notebook (notebooks/05_analytics_and_ml.ipynb) AND later by the
Streamlit app, so the numbers are always calculated the same way.

  Data      load_lab_tests(), load_instrument_days()
  KPIs      compute_kpis()
  Model A   train_delay_model()      Random Forest: "Will this test miss its TAT target?"
            (+ Logistic Regression baseline for comparison)
  Model C   detect_instrument_anomalies()   Isolation Forest: "Is this instrument-day abnormal?"
"""
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, confusion_matrix, f1_score, precision_score,
                             recall_score, roc_auc_score)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from connectors import PROJECT_ROOT, get_pg_connection

MODEL_DIR = PROJECT_ROOT / "models"


def query_df(sql, params=None):
    """Run a SELECT on the PostgreSQL warehouse and return a pandas DataFrame."""
    with get_pg_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [c.name for c in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


# ---------------------------------------------------------------------------
# DATA
# ---------------------------------------------------------------------------
def load_lab_tests():
    """One row per ordered test, joined with every dimension + staffing for that shift."""
    df = query_df("""
        SELECT f.order_test_id, f.order_number, f.priority, f.test_status, f.sample_status,
               f.rejection_reason, f.order_datetime, f.received_at, f.resulted_at,
               f.collect_to_receive_min, f.tat_min, f.total_tat_min, f.tat_target_min, f.tat_met,
               f.result_flag, f.is_abnormal, f.is_critical, f.reimbursement_usd,
               f.transport_method,
               d.year_month, d.month, d.quarter, d.day_name, d.day_of_week, d.is_weekend,
               t.test_code, t.test_name, t.category,
               i.instrument_code,
               dp.department_code, dp.department_name,
               p.insurance_type, p.age_group,
               s.shift_name,
               st.techs_present, st.staffing_pct
          FROM dw.fact_lab_test f
          JOIN dw.dim_date d        ON d.date_key = f.order_date_key
          JOIN dw.dim_test t        ON t.test_key = f.test_key
          JOIN dw.dim_department dp ON dp.department_key = f.department_key
          JOIN dw.dim_patient p     ON p.patient_key = f.patient_key
          LEFT JOIN dw.dim_instrument i ON i.instrument_key = f.instrument_key
          LEFT JOIN dw.dim_shift s      ON s.shift_key = f.shift_key
          LEFT JOIN dw.fact_daily_staffing st
                 ON st.shift_key = f.shift_key
                AND st.date_key = CAST(TO_CHAR(f.received_at, 'YYYYMMDD') AS INT)
    """)
    for col in ["tat_min", "total_tat_min", "tat_target_min", "collect_to_receive_min",
                "reimbursement_usd", "techs_present", "staffing_pct"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_instrument_days():
    """One row per instrument per day: errors, downtime, QC failures, workload and TAT."""
    events = query_df("""
        SELECT i.instrument_code, e.event_time::date AS day,
               COUNT(*) FILTER (WHERE e.event_type = 'ERROR')                          AS error_events,
               COALESCE(SUM(e.downtime_min), 0)                                       AS downtime_min,
               COUNT(*) FILTER (WHERE e.event_type = 'QC_RUN' AND e.qc_passed = FALSE) AS qc_failures
          FROM dw.fact_instrument_event e
          JOIN dw.dim_instrument i ON i.instrument_key = e.instrument_key
         GROUP BY i.instrument_code, e.event_time::date
    """)
    work = query_df("""
        SELECT i.instrument_code, f.received_at::date AS day,
               COUNT(*) AS tests_resulted, AVG(f.tat_min) AS avg_tat_min
          FROM dw.fact_lab_test f
          JOIN dw.dim_instrument i ON i.instrument_key = f.instrument_key
         WHERE f.resulted_at IS NOT NULL
         GROUP BY i.instrument_code, f.received_at::date
    """)
    df = events.merge(work, on=["instrument_code", "day"], how="left")
    df["tests_resulted"] = df["tests_resulted"].fillna(0).astype(int)
    df["avg_tat_min"] = pd.to_numeric(df["avg_tat_min"], errors="coerce")
    # a day with no resulted tests gets that instrument's normal (median) TAT
    df["avg_tat_min"] = df["avg_tat_min"].fillna(df.groupby("instrument_code")["avg_tat_min"].transform("median"))
    for col in ["error_events", "downtime_min", "qc_failures"]:
        df[col] = pd.to_numeric(df[col]).astype(float)
    df["day"] = pd.to_datetime(df["day"])
    return df.sort_values(["instrument_code", "day"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------
def compute_kpis(df):
    resulted = df[df["resulted_at"].notna()]
    return {
        "Tests ordered": len(df),
        "Tests resulted": len(resulted),
        "Average TAT (min)": round(resulted["tat_min"].mean(), 1),
        "% within TAT target": round(100 * resulted["tat_met"].astype(bool).mean(), 1),
        "% STAT (urgent)": round(100 * (df["priority"] == "STAT").mean(), 1),
        "Sample rejection rate (%)": round(100 * (df["sample_status"] == "REJECTED").mean(), 2),
        "Abnormal result rate (%)": round(100 * resulted["is_abnormal"].astype(bool).mean(), 1),
        "Critical results": int(resulted["is_critical"].astype(bool).sum()),
        "Revenue (USD)": round(df["reimbursement_usd"].sum(), 2),
    }


# ---------------------------------------------------------------------------
# MODEL A - Random Forest: will this test miss its turnaround-time target?
# ---------------------------------------------------------------------------
DELAY_FEATURES_CAT = ["category", "priority", "instrument_code", "shift_name", "day_name",
                      "department_code", "transport_method", "month"]
DELAY_FEATURES_NUM = ["staffing_pct"]
DELAY_FEATURES = DELAY_FEATURES_CAT + DELAY_FEATURES_NUM


def delay_dataset(df):
    """Inputs = facts known when the sample ARRIVES. Target = missed the TAT target (1) or not (0).
    The actual TAT minutes are NOT an input (that would be data leakage)."""
    data = df[df["tat_met"].notna()].copy()
    data["month"] = data["month"].astype(str)
    data["transport_method"] = data["transport_method"].fillna("Unknown")
    data["staffing_pct"] = data["staffing_pct"].fillna(data["staffing_pct"].median())
    X = data[DELAY_FEATURES]
    y = (~data["tat_met"].astype(bool)).astype(int)        # 1 = LATE
    return X, y


def _preprocessor(scale_numbers=False):
    num = StandardScaler() if scale_numbers else "passthrough"
    return ColumnTransformer([
        ("categories", OneHotEncoder(handle_unknown="ignore"), DELAY_FEATURES_CAT),
        ("numbers", num, DELAY_FEATURES_NUM),
    ])


def _scores(y_true, y_pred, y_prob):
    return {"Accuracy": accuracy_score(y_true, y_pred),
            "Precision (late)": precision_score(y_true, y_pred, zero_division=0),
            "Recall (late)": recall_score(y_true, y_pred, zero_division=0),
            "F1 (late)": f1_score(y_true, y_pred, zero_division=0),
            "ROC-AUC": roc_auc_score(y_true, y_prob)}


def train_delay_model(df, random_state=42):
    X, y = delay_dataset(df)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=random_state, stratify=y)

    baseline = Pipeline([("prep", _preprocessor(scale_numbers=True)),
                         ("model", LogisticRegression(max_iter=2000, class_weight="balanced"))])
    forest = Pipeline([("prep", _preprocessor()),
                       ("model", RandomForestClassifier(n_estimators=300, min_samples_leaf=5,
                                                        class_weight="balanced",
                                                        random_state=random_state, n_jobs=-1))])
    results = {}
    for name, pipe in [("Logistic Regression (baseline)", baseline), ("Random Forest", forest)]:
        pipe.fit(X_train, y_train)
        prob = pipe.predict_proba(X_test)[:, 1]
        pred = (prob >= 0.5).astype(int)
        results[name] = _scores(y_test, pred, prob)
    metrics = pd.DataFrame(results).T.round(3)

    prob = forest.predict_proba(X_test)[:, 1]
    cm = confusion_matrix(y_test, (prob >= 0.5).astype(int))

    # Which inputs matter most? Permutation importance = how much ROC-AUC drops
    # when one input is shuffled (so the model can no longer use it).
    imp = permutation_importance(forest, X_test, y_test, scoring="roc_auc",
                                 n_repeats=5, random_state=random_state, n_jobs=-1)
    importance = (pd.Series(imp.importances_mean, index=DELAY_FEATURES)
                  .sort_values(ascending=False).round(4))

    MODEL_DIR.mkdir(exist_ok=True)
    joblib.dump(forest, MODEL_DIR / "delay_model.joblib")
    return {"model": forest, "metrics": metrics, "confusion_matrix": cm,
            "importance": importance, "late_rate": y.mean(), "n_train": len(X_train),
            "n_test": len(X_test)}


def late_risk_by_group(model, df, group):
    """Average predicted probability of being late, by one input (e.g. shift_name)."""
    X, _ = delay_dataset(df)
    out = X.copy()
    out["late_risk_pct"] = 100 * model.predict_proba(X)[:, 1]
    return out.groupby(group)["late_risk_pct"].mean().round(1).sort_values(ascending=False)


# ---------------------------------------------------------------------------
# MODEL C - Isolation Forest: is this instrument-week abnormal?
# ---------------------------------------------------------------------------
ANOMALY_FEATURES = ["error_events", "downtime_min", "qc_failures", "tests_resulted", "avg_tat_min"]


def load_instrument_weeks(days=None):
    """Roll the daily instrument data up to WEEKS. Weekly totals are much less noisy
    than single days (one random error on a quiet day is not a real problem)."""
    days = load_instrument_days() if days is None else days.copy()
    days["week"] = days["day"].dt.to_period("W").dt.start_time
    return (days.groupby(["instrument_code", "week"])
                .agg(error_events=("error_events", "sum"), downtime_min=("downtime_min", "sum"),
                     qc_failures=("qc_failures", "sum"), tests_resulted=("tests_resulted", "sum"),
                     avg_tat_min=("avg_tat_min", "mean"))
                .reset_index())


def _robust_z(series):
    """How unusual is each value compared with this instrument's NORMAL level?
    Uses the median and MAD (median absolute deviation) instead of mean/std, so a
    long problem period cannot hide itself by pulling the average up."""
    med = series.median()
    mad = (series - med).abs().median() * 1.4826
    if mad == 0:
        mad = series.std(ddof=0) or 1.0
    return ((series - med) / mad).clip(-10, 10)


def detect_instrument_anomalies(weeks, contamination=0.05, random_state=42):
    """Unsupervised (no labels). Each instrument is compared with ITS OWN normal behaviour,
    then Isolation Forest flags the instrument-weeks that are easiest to 'isolate',
    i.e. the most unusual combination of errors, downtime, QC failures, workload and TAT."""
    data = weeks.copy()
    z = data.groupby("instrument_code")[ANOMALY_FEATURES].transform(_robust_z)
    forest = IsolationForest(n_estimators=300, contamination=contamination, random_state=random_state)
    forest.fit(z)
    data["anomaly_score"] = (-forest.score_samples(z)).round(3)   # higher = more unusual
    data["is_anomaly"] = forest.predict(z) == -1
    MODEL_DIR.mkdir(exist_ok=True)
    joblib.dump(forest, MODEL_DIR / "anomaly_model.joblib")
    return data


def anomaly_evaluation(flagged, instrument="CHEM-02", start="2026-06-29", end="2026-09-30"):
    """Check the model against the KNOWN problem period (CHEM-02, Q3 2026, confirmed by the
    incident reports). This is possible only because our data has a documented incident."""
    known = (flagged["instrument_code"] == instrument) & flagged["week"].between(start, end)
    flags = flagged["is_anomaly"]
    hits = int((flags & known).sum())
    return {"Instrument-weeks analysed": len(flagged),
            "Weeks flagged as abnormal": int(flags.sum()),
            f"Known {instrument} problem weeks (Jul-Sep)": int(known.sum()),
            "Problem weeks found by the model": hits,
            "Recall: % of problem weeks found": round(100 * hits / max(int(known.sum()), 1), 1),
            "Precision: % of flags that were real problems": round(100 * hits / max(int(flags.sum()), 1), 1)}
