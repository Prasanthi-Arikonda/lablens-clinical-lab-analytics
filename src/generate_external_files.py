"""
generate_external_files.py  -  creates the 2 EXTERNAL data files (Excel + CSV)

In a real hospital these files come from OUTSIDE the lab system:

  1. data/external/payer_fee_schedule.xlsx   (Excel, sent by the billing / payer office)
     How much each insurance type pays for each lab test.

  2. data/external/lab_staffing_roster.csv    (CSV, exported from the HR scheduling system)
     How many lab technologists were scheduled and actually present on each shift.

Like real external files, they are a bit messy on purpose (mixed date formats, "$" signs,
inconsistent spelling, blanks, duplicates). The ETL pipeline (Module 4) cleans them.

How to run (from the project folder, with .venv switched on):
    python src/generate_external_files.py
"""
import random
from datetime import date, timedelta

import pandas as pd

from connectors import PROJECT_ROOT, mysql_query

random.seed(7)
OUT_DIR = PROJECT_ROOT / "data" / "external"


def make_fee_schedule():
    """Payer fee schedule: allowed amount per test and insurance type."""
    cols, tests = mysql_query("SELECT test_code, test_name, price_usd FROM test_catalog ORDER BY test_id")
    payer_factor = {"Commercial": 1.25, "Medicare": 0.80, "Medicaid": 0.62, "Self-Pay": 1.00}
    rows = []
    for code, name, price in tests:
        for payer, factor in payer_factor.items():
            amount = round(float(price) * factor * random.uniform(0.95, 1.05), 2)
            rows.append({
                "Test Code": code,
                "Test Description": name,
                "Payer Type": payer,
                "Allowed Amount": f"${amount:,.2f}",          # money stored as text with "$"
                "Effective Date": "2026-01-01",
            })

    # ---- realistic messiness ----
    for r in random.sample(rows, 6):
        r["Test Code"] = " " + r["Test Code"].lower() + " "    # lowercase + extra spaces
    for r in random.sample(rows, 5):
        r["Payer Type"] = r["Payer Type"].upper()               # inconsistent capitalisation
    for r in random.sample(rows, 8):
        r["Effective Date"] = "01/01/2026"                       # a different date format
    random.choice(rows)["Allowed Amount"] = ""                   # missing value
    random.choice(rows)["Allowed Amount"] = "N/A"                # invalid value
    rows += [dict(r) for r in random.sample(rows, 3)]            # duplicate rows
    random.shuffle(rows)
    return pd.DataFrame(rows)


def make_staffing_roster():
    """Daily staffing per shift (Day / Evening / Night)."""
    planned = {"Day": 8, "Evening": 6, "Night": 3}
    rows = []
    d = date(2026, 1, 1)
    while d <= date(2026, 10, 31):
        for shift, scheduled in planned.items():
            if d.weekday() >= 5:                     # weekends have fewer staff
                scheduled = max(2, scheduled - 2)
            callouts = sum(random.random() < 0.06 for _ in range(scheduled))
            present = scheduled - callouts
            overtime = round(max(0, random.gauss(1.5 * callouts, 1.0)), 1)
            rows.append({
                "date": d.isoformat(),
                "shift": shift,
                "techs_scheduled": scheduled,
                "techs_present": present,
                "call_outs": callouts,
                "overtime_hours": overtime,
            })
        d += timedelta(days=1)

    # ---- realistic messiness ----
    for r in random.sample(rows, 40):
        r["date"] = pd.Timestamp(r["date"]).strftime("%m/%d/%Y")         # US date format
    spelling_variants = {"Day": ["day", "DAY", "Day shift"],
                         "Evening": ["evening", "Evening ", "EVE"],
                         "Night": ["night", "NIGHT", "Nights"]}
    for r in random.sample(rows, 30):
        r["shift"] = random.choice(spelling_variants[r["shift"]])    # inconsistent spelling
    for r in random.sample(rows, 12):
        r["techs_present"] = None                                        # missing value
    for r in random.sample(rows, 3):
        r["techs_present"] = -1                                          # impossible value
    rows += [dict(r) for r in random.sample(rows, 6)]                    # duplicates
    return pd.DataFrame(rows)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    fees = make_fee_schedule()
    fee_path = OUT_DIR / "payer_fee_schedule.xlsx"
    fees.to_excel(fee_path, index=False, sheet_name="Fee Schedule 2026")
    print(f"Created {fee_path.relative_to(PROJECT_ROOT)}  ({len(fees)} rows)")

    staff = make_staffing_roster()
    staff_path = OUT_DIR / "lab_staffing_roster.csv"
    staff.to_csv(staff_path, index=False)
    print(f"Created {staff_path.relative_to(PROJECT_ROOT)}  ({len(staff)} rows)")

    print("External files created successfully.")


if __name__ == "__main__":
    main()
