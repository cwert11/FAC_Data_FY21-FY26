# COA Weekly FAC Refresh + Tier 1 Enrichment

Automated pipeline for `github.com/cwert11/FAC_Data_FY21-FY26`. Runs every
Thursday at 6am ET (the FAC production API refreshes on Wednesdays), pulls new
and updated filings for audit years 2024–2026, downloads full audit PDFs and
IRS 990 data for Tier 1 prospects, and commits everything back to the repo.

## One-time setup (about 10 minutes)

1. **Get a FAC API key.** Go to fac.gov → API resources → API signup. It's a
   free api.data.gov key sent to your email. Do NOT put the key in any file in
   this repo.

2. **Add the key as a repo secret.** In GitHub: repo → Settings → Secrets and
   variables → Actions → New repository secret. Name it exactly `FAC_API_KEY`.

3. **Copy these files into the repo** preserving paths:
   - `.github/workflows/weekly_refresh.yml`
   - `scripts/fac_refresh.py`
   - `scripts/enrich_tier1.py`
   - `data/tier1_targets.csv`

4. **Populate `data/tier1_targets.csv`** with the Tier 1 list from
   `COA_FAC_Prospect_Analysis_AY21-26.xlsx` (columns: org_name, report_id,
   ein, uei, state — report_id should be each org's most recent audit).
   Claude can generate this export from the workbook in a session.

5. **First run.** Repo → Actions → "Weekly FAC Refresh + Tier 1 Enrichment" →
   Run workflow → set `full_backfill` to `true`. This seeds
   `data/current/` with all AY2024–2026 records. Subsequent scheduled runs
   are incremental (only records accepted since the last run).

## What each run produces

| Path | Contents |
|---|---|
| `data/current/*.csv` | Five core FAC tables, upserted (resubmissions replace prior rows — no double counting) |
| `data/current/delta_report.md` | Weekly alert: new HUD/HHS filers **with findings** — the BD hot list |
| `data/current/last_refresh.json` | High-water mark for incremental pulls |
| `enrichment/pdfs/{report_id}.pdf` | Full single audit reports for Tier 1 orgs |
| `enrichment/990s/{ein}.json` | ProPublica Nonprofit Explorer records |
| `enrichment/990_summary.csv` | One row per Tier 1 nonprofit: revenue trend, assets, link to filings |

## Working with Claude on the results

In a Claude session: "Pull the latest delta report and enrichment data from my
FAC repo and re-score anything that changed." Claude clones the repo
(github.com is reachable from its sandbox), diffs against the prior Tier 1/
Tier 2 workbook, and updates scores. Audit PDFs in `enrichment/pdfs/` can be
read directly for CAP-quality review, going-concern language, and management
response analysis.

## Notes and limits

- **990s are nonprofits-only.** Housing authorities and governments return
  `not_found` — that's expected, not an error. Their financials come from the
  audit PDF instead.
- **Officer/CFO names** aren't in the summary CSV; open the latest 990 PDF via
  the ProPublica link (Part VII) or have Claude read it.
- **PDF availability:** full reports are reliably downloadable for audits
  submitted since Oct 2023 (AY2023+). Older reports may be unavailable.
- **Repo size:** PDFs average 1–3 MB. If Tier 1 grows past ~300 orgs, consider
  moving `enrichment/pdfs/` to Git LFS or a Release asset.
- **Annual maintenance:** bump `TARGET_AUDIT_YEARS` in `fac_refresh.py` each
  fall when a new audit year opens.
