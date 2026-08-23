#!/usr/bin/env python3
"""
Weekly incremental refresh from the FAC API (https://api.fac.gov).

- Pulls records for TARGET_AUDIT_YEARS accepted since the last refresh
  (or everything for those years if FULL_BACKFILL=true).
- Appends/updates five core tables stored as CSVs in data/current/.
- Writes data/current/delta_report.md summarizing new HUD/HHS-relevant
  filings with findings — the weekly "who just filed with a problem" alert.

State: data/current/last_refresh.json stores the high-water fac_accepted_date.
"""

import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://api.fac.gov"
API_KEY = os.environ.get("FAC_API_KEY", "")
FULL_BACKFILL = os.environ.get("FULL_BACKFILL", "false").lower() == "true"

# Audit years to track. Update annually.
TARGET_AUDIT_YEARS = [2024, 2025, 2026]

TABLES = [
    "general",
    "federal_awards",
    "findings",
    "findings_text",
    "corrective_action_plans",
]

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "current"
STATE_FILE = DATA_DIR / "last_refresh.json"
PAGE_SIZE = 20000  # FAC hard cap per request

HUD_HHS_PREFIXES = ("14", "93")


def api_get(endpoint: str, params: dict) -> list:
    """Paginated GET against a FAC endpoint. Returns list of dicts."""
    if not API_KEY:
        sys.exit("FAC_API_KEY is not set. Add it as a repo secret.")
    headers = {"X-Api-Key": API_KEY}
    out, offset = [], 0
    while True:
        q = dict(params)
        q["limit"] = PAGE_SIZE
        q["offset"] = offset
        resp = requests.get(f"{BASE_URL}/{endpoint}", headers=headers, params=q, timeout=120)
        if resp.status_code == 429:
            print(f"  rate-limited on {endpoint}, sleeping 60s...")
            time.sleep(60)
            continue
        resp.raise_for_status()
        batch = resp.json()
        out.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(1)
    return out


def load_state() -> str | None:
    if STATE_FILE.exists() and not FULL_BACKFILL:
        return json.loads(STATE_FILE.read_text()).get("last_accepted_date")
    return None


def save_state():
    STATE_FILE.write_text(json.dumps({
        "last_accepted_date": date.today().isoformat(),
        "run_utc": datetime.utcnow().isoformat(),
        "audit_years": TARGET_AUDIT_YEARS,
    }, indent=2))


def merge_csv(table: str, new_rows: list):
    """Upsert new rows into the stored CSV, keyed on report_id (+ row identity)."""
    if not new_rows:
        return 0
    new_df = pd.DataFrame(new_rows)
    path = DATA_DIR / f"{table}.csv"
    if path.exists():
        old_df = pd.read_csv(path, dtype=str, low_memory=False)
        new_df = new_df.astype(str)
        # Drop any prior rows for report_ids being refreshed (handles resubmissions),
        # then append. This avoids duplicate findings rows for the same report.
        refreshed_ids = set(new_df["report_id"].unique())
        old_df = old_df[~old_df["report_id"].isin(refreshed_ids)]
        merged = pd.concat([old_df, new_df], ignore_index=True)
    else:
        merged = new_df.astype(str)
    merged.to_csv(path, index=False)
    return len(new_df)


def build_delta_report(new_general: list, new_findings: list, new_awards: list):
    """Markdown alert: new/updated filings with HUD/HHS awards and findings."""
    lines = [f"# FAC Delta Report — {date.today().isoformat()}", ""]
    if not new_general:
        lines.append("No new or updated filings this week for the target audit years.")
        (DATA_DIR / "delta_report.md").write_text("\n".join(lines))
        return

    gen = pd.DataFrame(new_general)
    fin = pd.DataFrame(new_findings) if new_findings else pd.DataFrame(columns=["report_id"])
    awd = pd.DataFrame(new_awards) if new_awards else pd.DataFrame(columns=["report_id"])

    # HUD/HHS flag from ALN prefix on awards
    if not awd.empty and "federal_agency_prefix" in awd.columns:
        hud_hhs_ids = set(
            awd[awd["federal_agency_prefix"].astype(str).isin(HUD_HHS_PREFIXES)]["report_id"]
        )
    else:
        hud_hhs_ids = set()

    finding_counts = fin.groupby("report_id").size() if not fin.empty else pd.Series(dtype=int)

    lines.append(f"**New/updated filings:** {len(gen)}")
    lines.append(f"**With HUD/HHS awards:** {len(hud_hhs_ids)}")
    lines.append("")
    lines.append("## Priority alerts: HUD/HHS filers with findings")
    lines.append("")
    lines.append("| Auditee | State | Audit Year | Report ID | Findings | Fed. Expenditures |")
    lines.append("|---|---|---|---|---|---|")

    alert_count = 0
    for _, row in gen.iterrows():
        rid = row.get("report_id", "")
        n_findings = int(finding_counts.get(rid, 0))
        if rid in hud_hhs_ids and n_findings > 0:
            alert_count += 1
            lines.append(
                f"| {row.get('auditee_name','')} | {row.get('auditee_state','')} "
                f"| {row.get('audit_year','')} | {rid} | {n_findings} "
                f"| {row.get('total_amount_expended','')} |"
            )
    if alert_count == 0:
        lines.append("| _None this week_ | | | | | |")

    lines.append("")
    lines.append("_Next step: run the scoring pipeline against these report_ids and "
                 "compare against the existing Tier 1/Tier 2 workbook._")
    (DATA_DIR / "delta_report.md").write_text("\n".join(lines))
    print(f"Delta report written: {alert_count} HUD/HHS finding alerts.")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    since = load_state()
    mode = f"incremental since {since}" if since else "FULL BACKFILL"
    print(f"FAC refresh — {mode}; audit years {TARGET_AUDIT_YEARS}")

    all_new = {t: [] for t in TABLES}
    for year in TARGET_AUDIT_YEARS:
        # Step 1: which report_ids are new/updated? (general is the anchor table)
        params = {"audit_year": f"eq.{year}"}
        if since:
            params["fac_accepted_date"] = f"gte.{since}"
        gen_rows = api_get("general", params)
        print(f"  AY{year}: {len(gen_rows)} general records")
        all_new["general"].extend(gen_rows)

        # Step 2: pull child tables per year with the same acceptance filter where
        # supported; otherwise filter to the report_ids we just found.
        rids = {r["report_id"] for r in gen_rows}
        if not rids:
            continue
        for table in TABLES[1:]:
            rows = api_get(table, dict(params))
            rows = [r for r in rows if r.get("report_id") in rids]
            all_new[table].extend(rows)
            print(f"    {table}: {len(rows)} rows")

    for table in TABLES:
        n = merge_csv(table, all_new[table])
        print(f"  merged {n} rows into {table}.csv")

    build_delta_report(all_new["general"], all_new["findings"], all_new["federal_awards"])
    save_state()
    print("Refresh complete.")


if __name__ == "__main__":
    main()
