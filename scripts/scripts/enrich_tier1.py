#!/usr/bin/env python3
"""
Enrich Tier 1 prospects with:
  1. The full single audit report PDF from the FAC (app.fac.gov dissemination)
  2. IRS Form 990 financial history from ProPublica Nonprofit Explorer

Input:  data/tier1_targets.csv
        Required columns: org_name, report_id, ein  (uei, state optional)

Output: enrichment/pdfs/{report_id}.pdf
        enrichment/990s/{ein}.json
        enrichment/990_summary.csv

Idempotent: existing PDFs/JSONs are skipped on later runs.
Note: 990 data applies to nonprofits only; housing authorities and
governments return not_found — that's expected.
"""

import csv
import json
import re
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ROOT / "data" / "tier1_targets.csv"
PDF_DIR = ROOT / "enrichment" / "pdfs"
IRS_DIR = ROOT / "enrichment" / "990s"
SUMMARY = ROOT / "enrichment" / "990_summary.csv"

FAC_PDF_URL = "https://app.fac.gov/dissemination/report/pdf/{report_id}"
FAC_SUMMARY_URL = "https://app.fac.gov/dissemination/summary/{report_id}"
PROPUBLICA_URL = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"

HEADERS = {"User-Agent": "COA-prospect-research/1.0 (grant compliance consulting)"}
THROTTLE_SECONDS = 3


def clean_ein(ein):
    digits = re.sub(r"\D", "", str(ein or ""))
    return digits if len(digits) == 9 else None


def fetch_pdf(report_id):
    dest = PDF_DIR / f"{report_id}.pdf"
    if dest.exists() and dest.stat().st_size > 10_000:
        return "already_have"
    try:
        r = requests.get(FAC_PDF_URL.format(report_id=report_id),
                         headers=HEADERS, timeout=180, allow_redirects=True)
        if r.status_code == 200 and r.content[:5] == b"%PDF-":
            dest.write_bytes(r.content)
            return "downloaded"
        s = requests.get(FAC_SUMMARY_URL.format(report_id=report_id),
                         headers=HEADERS, timeout=120)
        if s.status_code == 200:
            m = re.search(r'href="([^"]+\.pdf[^"]*)"', s.text)
            if m:
                url = m.group(1)
                if url.startswith("/"):
                    url = "https://app.fac.gov" + url
                r2 = requests.get(url, headers=HEADERS, timeout=180)
                if r2.status_code == 200 and r2.content[:5] == b"%PDF-":
                    dest.write_bytes(r2.content)
                    return "downloaded_via_summary"
        return f"unavailable_http_{r.status_code}"
    except requests.RequestException as e:
        return f"error_{type(e).__name__}"


def fetch_990(ein):
    dest = IRS_DIR / f"{ein}.json"
    if dest.exists():
        return "already_have", json.loads(dest.read_text())
    try:
        r = requests.get(PROPUBLICA_URL.format(ein=ein), headers=HEADERS, timeout=60)
        if r.status_code == 404:
            return "not_found", None
        r.raise_for_status()
        data = r.json()
        dest.write_text(json.dumps(data, indent=2))
        return "downloaded", data
    except requests.RequestException as e:
        return f"error_{type(e).__name__}", None


def summarize_990(org_name, ein, data):
    org = data.get("organization", {}) or {}
    filings = data.get("filings_with_data", []) or []
    filings = sorted(filings, key=lambda f: f.get("tax_prd_yr", 0), reverse=True)
    latest = filings[0] if filings else {}
    prior = filings[1] if len(filings) > 1 else {}

    def g(f, k):
        v = f.get(k)
        return v if v is not None else ""

    return {
        "org_name": org_name,
        "ein": ein,
        "irs_name": org.get("name", ""),
        "city": org.get("city", ""),
        "state": org.get("state", ""),
        "ntee_code": org.get("ntee_code", ""),
        "latest_990_year": g(latest, "tax_prd_yr"),
        "total_revenue_latest": g(latest, "totrevenue"),
        "total_revenue_prior": g(prior, "totrevenue"),
        "total_expenses_latest": g(latest, "totfuncexpns"),
        "total_assets_latest": g(latest, "totassetsend"),
        "net_assets_latest": g(latest, "totnetassetend"),
        "n_filings_available": len(filings),
        "propublica_url": f"https://projects.propublica.org/nonprofits/organizations/{ein}",
        "note": ("Officer/CFO names and comp: open the latest 990 PDF via the "
                 "ProPublica link (Part VII)."),
    }


def main():
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    IRS_DIR.mkdir(parents=True, exist_ok=True)

    if not TARGETS.exists():
        print(f"No {TARGETS} found — skipping enrichment.")
        return

    rows = list(csv.DictReader(TARGETS.open()))
    print(f"Enriching {len(rows)} Tier 1 targets...")

    summaries, log = [], []
    for row in rows:
        name = row.get("org_name", "").strip()
        rid = (row.get("report_id") or "").strip()
        ein = clean_ein(row.get("ein", ""))

        pdf_status = fetch_pdf(rid) if rid else "no_report_id"
        if pdf_status.startswith("downloaded"):
            time.sleep(THROTTLE_SECONDS)

        if ein:
            irs_status, data = fetch_990(ein)
            if irs_status == "downloaded":
                time.sleep(THROTTLE_SECONDS)
            if data:
                summaries.append(summarize_990(name, ein, data))
        else:
            irs_status = "no_valid_ein"

        log.append(f"{name}: pdf={pdf_status}, 990={irs_status}")
        print(f"  {log[-1]}")

    if summaries:
        with SUMMARY.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(summaries[0].keys()))
            w.writeheader()
            w.writerows(summaries)
        print(f"Wrote {SUMMARY} ({len(summaries)} orgs)")

    (ROOT / "enrichment" / "enrichment_log.txt").write_text("\n".join(log))
    print("Enrichment complete.")


if __name__ == "__main__":
    main()
