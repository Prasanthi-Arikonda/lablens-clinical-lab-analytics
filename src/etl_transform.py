"""
etl_transform.py  -  Module 4, step 2: VALIDATE, CLEAN, STANDARDIZE, DEDUPLICATE, RECONCILE

Every function takes raw DataFrames and returns (clean_dataframe, issues).
`issues` is a list of data-quality problems found, which the pipeline saves
into the etl.dq_issue table so every correction is documented.

DOCUMENTED DATA-QUALITY RULES
  DQ01  Standardize identifiers   - trim spaces, upper-case barcodes and test codes
  DQ02  Remove duplicates         - keep one record per natural key
  DQ03  Parse numbers / currency  - "$19.41" -> 19.41, "22.5 C" -> 22.5, "N/A" -> NULL
  DQ04  Standardize categories    - "SELF-PAY" -> "Self-Pay", "NIGHT"/"Nights" -> "Night"
  DQ05  Standardize dates         - "01/05/2026" and "2026-01-05" -> one DATE type
  DQ06  Validate ranges           - negative staff counts, impossible temperatures,
                                    results before samples were received -> rejected / NULL
  DQ07  Reconcile across systems  - MongoDB barcodes and fee-schedule test codes must exist
                                    in MySQL; unmatched records are excluded and logged
  DQ08  Handle missing values     - missing transport / price -> NULL or documented default
"""
import re
from datetime import date, datetime

import numpy as np
import pandas as pd

BARCODE_PATTERN = re.compile(r"^SMP\d{7}$")
PAYER_CANONICAL = {"commercial": "Commercial", "medicare": "Medicare",
                   "medicaid": "Medicaid", "self-pay": "Self-Pay", "selfpay": "Self-Pay"}
SHIFT_CANONICAL = {"day": "Day", "day shift": "Day", "evening": "Evening", "eve": "Evening",
                   "night": "Night", "nights": "Night"}
SHIFT_KEYS = {"Day": 1, "Evening": 2, "Night": 3}


def issue(rule, source, ref, text, action):
    return {"rule_code": rule, "source_name": source, "record_ref": str(ref),
            "issue": text, "action_taken": action}


def parse_number(value):
    """Return a float from messy text like '$1,234.50', '22.5 C' or ' 7 '. None if impossible."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)
    match = re.search(r"-?\d+(\.\d+)?", str(value).replace(",", ""))
    return float(match.group()) if match else None


def parse_date(value):
    """Accept ISO (2026-01-05) or US (01/05/2026) dates. None if invalid."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def shift_for_hour(hour):
    """Day 07-15, Evening 15-23, Night 23-07."""
    if hour is None or pd.isna(hour):
        return None
    hour = int(hour)
    if 7 <= hour < 15:
        return 1
    if 15 <= hour < 23:
        return 2
    return 3


def age_group(birth_year, as_of=2026):
    if birth_year is None or pd.isna(birth_year):
        return None
    age = as_of - int(birth_year)
    if age < 18:
        return "0-17"
    if age < 40:
        return "18-39"
    if age < 65:
        return "40-64"
    return "65+"


# ---------------------------------------------------------------------------
# MongoDB sample tracking  (DQ01, DQ02, DQ03, DQ06, DQ07, DQ08)
# ---------------------------------------------------------------------------
def clean_sample_tracking(raw, valid_barcodes):
    issues = []
    df = raw.copy()
    src = "mongodb.sample_tracking"

    # DQ01 - standardize barcodes
    df["sample_barcode"] = df["sample_barcode_raw"].astype(str).str.strip().str.upper()
    changed = df[df["sample_barcode"] != df["sample_barcode_raw"]]
    for _, r in changed.iterrows():
        issues.append(issue("DQ01", src, r["mongo_id"], f"Barcode '{r['sample_barcode_raw']}' not standard",
                            f"Standardized to '{r['sample_barcode']}'"))
    bad_format = df[~df["sample_barcode"].str.match(BARCODE_PATTERN)]
    for _, r in bad_format.iterrows():
        issues.append(issue("DQ01", src, r["mongo_id"], f"Invalid barcode format '{r['sample_barcode']}'",
                            "Record excluded"))
    df = df[df["sample_barcode"].str.match(BARCODE_PATTERN)]

    # DQ02 - duplicates (same scan stored twice): keep the first
    dup_mask = df.duplicated(subset=["sample_barcode"], keep="first")
    for _, r in df[dup_mask].iterrows():
        issues.append(issue("DQ02", src, r["mongo_id"], f"Duplicate document for {r['sample_barcode']}",
                            "Duplicate removed"))
    df = df[~dup_mask]

    # DQ03 - temperature typed as text ("22.5 C") -> number
    df["transport_temp_c"] = df["temperature_raw"].apply(parse_number)
    as_text = df[df["temperature_raw"].apply(lambda v: isinstance(v, str))]
    for _, r in as_text.iterrows():
        issues.append(issue("DQ03", src, r["sample_barcode"], f"Temperature stored as text '{r['temperature_raw']}'",
                            f"Converted to {r['transport_temp_c']}"))

    # DQ06 - impossible temperatures
    bad_temp = df["transport_temp_c"].notna() & ~df["transport_temp_c"].between(-5, 40)
    for _, r in df[bad_temp].iterrows():
        issues.append(issue("DQ06", src, r["sample_barcode"], f"Temperature {r['transport_temp_c']} out of range",
                            "Set to NULL"))
    df.loc[bad_temp, "transport_temp_c"] = None

    # DQ08 - missing transport information
    for _, r in df[df["transport_method"].isna()].iterrows():
        issues.append(issue("DQ08", src, r["sample_barcode"], "Transport details missing",
                            "Kept sample; transport fields set to NULL"))

    # DQ07 - every MongoDB barcode must exist in MySQL
    unmatched = df[~df["sample_barcode"].isin(valid_barcodes)]
    for _, r in unmatched.iterrows():
        issues.append(issue("DQ07", src, r["sample_barcode"], "Barcode not found in MySQL samples",
                            "Record excluded"))
    df = df[df["sample_barcode"].isin(valid_barcodes)]

    return df[["sample_barcode", "transport_method", "transport_temp_c"]], issues


# ---------------------------------------------------------------------------
# Excel fee schedule  (DQ01, DQ02, DQ03, DQ04, DQ05, DQ07, DQ08)
# ---------------------------------------------------------------------------
def clean_fee_schedule(raw, valid_test_codes):
    issues = []
    src = "xlsx.payer_fee_schedule"
    df = raw.copy()

    df["test_code"] = df["test_code_raw"].astype(str).str.strip().str.upper()               # DQ01
    for _, r in df[df["test_code"] != df["test_code_raw"]].iterrows():
        issues.append(issue("DQ01", src, r["test_code_raw"], "Test code not standard",
                            f"Standardized to '{r['test_code']}'"))

    df["payer_type"] = df["payer_type_raw"].astype(str).str.strip().str.lower().map(PAYER_CANONICAL)  # DQ04
    for _, r in df[df["payer_type"] != df["payer_type_raw"]].iterrows():
        issues.append(issue("DQ04", src, f"{r['test_code']}/{r['payer_type_raw']}",
                            f"Payer type '{r['payer_type_raw']}' not standard",
                            f"Standardized to '{r['payer_type']}'"))

    df["allowed_amount_usd"] = df["allowed_amount_raw"].apply(parse_number)                  # DQ03
    for _, r in df[df["allowed_amount_usd"].isna()].iterrows():                              # DQ08
        issues.append(issue("DQ08", src, f"{r['test_code']}/{r['payer_type']}",
                            f"Allowed amount missing or invalid ('{r['allowed_amount_raw']}')",
                            "Set to NULL; list price used for revenue"))

    df["effective_date"] = df["effective_date_raw"].apply(parse_date)                        # DQ05
    for _, r in df[df["effective_date_raw"].astype(str).str.contains("/")].iterrows():
        issues.append(issue("DQ05", src, f"{r['test_code']}/{r['payer_type']}",
                            f"US date format '{r['effective_date_raw']}'", f"Converted to {r['effective_date']}"))

    # DQ02 - duplicates: prefer the row that has an amount
    df = df.sort_values("allowed_amount_usd", na_position="last")
    dup = df.duplicated(subset=["test_code", "payer_type"], keep="first")
    for _, r in df[dup].iterrows():
        issues.append(issue("DQ02", src, f"{r['test_code']}/{r['payer_type']}", "Duplicate fee row",
                            "Duplicate removed"))
    df = df[~dup]

    unknown = df[~df["test_code"].isin(valid_test_codes)]                                    # DQ07
    for _, r in unknown.iterrows():
        issues.append(issue("DQ07", src, r["test_code"], "Test code not in MySQL test catalog", "Row excluded"))
    df = df[df["test_code"].isin(valid_test_codes)]

    return df[["test_code", "payer_type", "allowed_amount_usd", "effective_date"]], issues


# ---------------------------------------------------------------------------
# CSV staffing roster  (DQ02, DQ03, DQ04, DQ05, DQ06, DQ08)
# ---------------------------------------------------------------------------
def clean_staffing(raw):
    issues = []
    src = "csv.lab_staffing_roster"
    df = raw.copy()

    df["work_date"] = df["date_raw"].apply(parse_date)                                       # DQ05
    for _, r in df[df["date_raw"].astype(str).str.contains("/")].iterrows():
        issues.append(issue("DQ05", src, r["date_raw"], "US date format", f"Converted to {r['work_date']}"))
    for _, r in df[df["work_date"].isna()].iterrows():
        issues.append(issue("DQ05", src, r["date_raw"], "Unreadable date", "Row excluded"))
    df = df[df["work_date"].notna()]

    df["shift_name"] = df["shift_raw"].astype(str).str.strip().str.lower().map(SHIFT_CANONICAL)  # DQ04
    for _, r in df[df["shift_name"] != df["shift_raw"]].iterrows():
        issues.append(issue("DQ04", src, f"{r['work_date']}/{r['shift_raw']}",
                            f"Shift name '{r['shift_raw']}' not standard", f"Standardized to '{r['shift_name']}'"))
    df = df[df["shift_name"].notna()]

    for col in ["techs_scheduled", "techs_present", "call_outs", "overtime_hours"]:          # DQ03
        df[col] = df[col].apply(parse_number)

    bad = df["techs_present"].notna() & ((df["techs_present"] < 0) |
                                         (df["techs_present"] > df["techs_scheduled"]))       # DQ06
    for _, r in df[bad].iterrows():
        issues.append(issue("DQ06", src, f"{r['work_date']}/{r['shift_name']}",
                            f"Impossible techs_present = {r['techs_present']}", "Set to NULL"))
    df.loc[bad, "techs_present"] = None

    missing = df["techs_present"].isna()                                                     # DQ08
    for _, r in df[missing].iterrows():
        issues.append(issue("DQ08", src, f"{r['work_date']}/{r['shift_name']}", "techs_present missing",
                            "Estimated as techs_scheduled - call_outs"))
    df.loc[missing, "techs_present"] = df.loc[missing, "techs_scheduled"] - df.loc[missing, "call_outs"]

    dup = df.duplicated(subset=["work_date", "shift_name"], keep="first")                    # DQ02
    for _, r in df[dup].iterrows():
        issues.append(issue("DQ02", src, f"{r['work_date']}/{r['shift_name']}", "Duplicate roster row",
                            "Duplicate removed"))
    df = df[~dup].copy()

    df["shift_key"] = df["shift_name"].map(SHIFT_KEYS)
    df["date_key"] = df["work_date"].apply(lambda d: int(d.strftime("%Y%m%d")))
    df["staffing_pct"] = (100 * df["techs_present"] / df["techs_scheduled"]).round(1)
    return df[["date_key", "shift_key", "techs_scheduled", "techs_present", "call_outs",
               "overtime_hours", "staffing_pct"]], issues


# ---------------------------------------------------------------------------
# MongoDB equipment logs  (DQ03, DQ06)
# ---------------------------------------------------------------------------
def clean_equipment_logs(raw, valid_instruments):
    issues = []
    src = "mongodb.equipment_logs"
    if raw.empty:
        return raw, issues
    df = raw.copy()
    df["downtime_min"] = df["downtime_min"].apply(parse_number)
    bad = df["downtime_min"].notna() & (df["downtime_min"] < 0)
    for _, r in df[bad].iterrows():
        issues.append(issue("DQ06", src, r["mongo_id"], "Negative downtime", "Set to NULL"))
    df.loc[bad, "downtime_min"] = None
    unknown = df[~df["instrument_code"].isin(valid_instruments)]
    for _, r in unknown.iterrows():
        issues.append(issue("DQ07", src, r["mongo_id"], f"Unknown instrument {r['instrument_code']}",
                            "Record excluded"))
    df = df[df["instrument_code"].isin(valid_instruments)].copy()
    df["date_key"] = pd.to_datetime(df["event_time"]).dt.strftime("%Y%m%d").astype(int)
    return df, issues


# ---------------------------------------------------------------------------
# Build the main fact rows (MySQL lab tests + MongoDB transport + Excel fees)
# ---------------------------------------------------------------------------
def build_lab_test_facts(tests, dims, transport, fees):
    """Combine the three sources into one row per ordered test, with measures computed."""
    issues = []
    src = "mysql.lab_tests"
    df = tests.copy()
    if df.empty:
        return df, issues

    for col in ["order_datetime", "collected_at", "received_at", "resulted_at", "source_updated_at"]:
        df[col] = pd.to_datetime(df[col])

    # DQ06 - a result cannot be released before the sample arrived
    bad_time = df["resulted_at"].notna() & df["received_at"].notna() & (df["resulted_at"] < df["received_at"])
    for _, r in df[bad_time].iterrows():
        issues.append(issue("DQ06", src, r["order_test_id"], "resulted_at earlier than received_at",
                            "Timing measures set to NULL"))

    minutes = lambda a, b: ((b - a).dt.total_seconds() / 60).round()
    df["collect_to_receive_min"] = minutes(df["collected_at"], df["received_at"])
    df["tat_min"] = minutes(df["received_at"], df["resulted_at"])
    df["total_tat_min"] = minutes(df["order_datetime"], df["resulted_at"])
    df.loc[bad_time, ["tat_min", "total_tat_min"]] = np.nan

    test_info = dims["tests"].set_index("test_code")
    df["tat_target_min"] = df["test_code"].map(test_info["tat_target_min"])
    df["tat_met"] = np.where(df["tat_min"].notna(), df["tat_min"] <= df["tat_target_min"], None)
    df["is_abnormal"] = np.where(df["result_flag"].notna(), df["result_flag"] != "N", None)
    df["is_critical"] = np.where(df["result_flag"].notna(), df["result_flag"].isin(["HH", "LL"]), None)
    df["order_date_key"] = df["order_datetime"].dt.strftime("%Y%m%d").astype(int)
    df["shift_key"] = df["received_at"].dt.hour.apply(shift_for_hour)

    # Join MongoDB transport details by barcode
    df = df.merge(transport, how="left", on="sample_barcode")

    # Revenue: fee for the patient's insurance type; fall back to list price (DQ08 rule)
    insurance = dims["patients"].set_index("patient_mrn")["insurance_type"]
    df["insurance_type"] = df["patient_mrn"].map(insurance)
    fee_lookup = fees.set_index(["test_code", "payer_type"])["allowed_amount_usd"].to_dict()
    list_price = test_info["price_usd"].astype(float)

    def revenue(row):
        if row["test_status"] != "COMPLETED":
            return 0.0                                   # only completed tests are billed
        amount = fee_lookup.get((row["test_code"], row["insurance_type"]))
        if amount is None or pd.isna(amount):
            amount = list_price.get(row["test_code"])
        return round(float(amount), 2)

    df["reimbursement_usd"] = df.apply(revenue, axis=1)
    return df, issues
