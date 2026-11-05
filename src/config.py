"""Config and cleaning log; all paths are relative to repo root"""

import pandas as pd

RAW_DIR = "data/raw"
PROCESSED_DIR = "data/processed"
OUTPUT_DIR = "outputs"

# the 9 county Bay Area FIPS codes 
COUNTY_FIPS = ["001", "013", "041", "055", "075", "081", "085", "095", "097"]
# Bay Area CRS we used in class
CRS = 32610
AIRPORTS = ["SFO", "OAK", "SJC"]
# one non-holiday weekday in october 2026 for transit routing (from our proposal)
SERVICE_DATE = "2026-10-28"
# sunday scenario; skipped nov 1 since clocks change that day
SUNDAY_DATE = "2026-11-08"
# leave home at 2 am and count airport arrivals up to 9 am, covering early shift starts
FIRST_DEPARTURE_HOUR = 2
LAST_ARRIVAL_HOUR = 9

cleaning_log = []

# to document our cleaning process
def log_step(dataset, step, rows_before, rows_after, reason):
    cleaning_log.append({"dataset": dataset, "step": step, "rows_before": rows_before, "rows_after": rows_after, "reason": reason})
    print(f"[{dataset}] {step}: {rows_before:,} -> {rows_after:,} ({reason})")


def save_log():
    pd.DataFrame(cleaning_log).to_csv(f"{OUTPUT_DIR}/cleaning_log.csv", index=False)
