#!/usr/bin/env python3
"""
Draft bespoke outreach emails for tracker rows with status=new_prospect,
using the Anthropic API. Writes one markdown draft per prospect to
outreach/drafts/ and advances the tracker row to status=email_drafted.

Requires:
  - ANTHROPIC_API_KEY env var (repo secret)
  - sender_config.json at repo root with real values (aborts on placeholders)

Env knobs:
  MAX_DRAFTS   cap per run (default 0 = no cap)
  MODEL        default claude-sonnet-4-6
"""

import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
TRACKER = ROOT / "outreach" / "pipeline_tracker.csv"
DRAFTS = ROOT / "outreach" / "drafts"
CONFIG = ROOT / "sender_config.json"
SCORED = ROOT / "analysis" / "prospects_scored.csv.gz"

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("MODEL", "claude-sonnet-5")
MAX_DRAFTS = int(os.environ.get("MAX_DRAFTS", "0"))
API_URL = "https://api.anthropic.com/v1/messages"


def load_year_table(year, table):
    for p in [ROOT / "data" / "current" / f"{table}_AY{year}.csv.gz",
              ROOT / f"{year}-ay-{table}.csv",
              ROOT / f"{year}-ay-{table}.zip"]:
        if p.exists():
            try:
                return pd.read_csv(p, dtype=str, low_memory=False)
            except Exception:
                pass
    return pd.DataFrame()


_cache = {}


def report_texts(report_id, year):
    """Return (findings_text_snippet, cap_snippet) for a report, truncated."""
    key = ("ft", year)
    if key not in _cache:
        _cache[key] = load_year_table(year, "findings_text")
    ft = _cache[key]
    key = ("cap", year)
    if key not in _cache:
        _cache[key] = load_year_table(year, "corrective_action_plans")
    cp = _cache[key]

    f_txt = ""
    if not ft.empty and "finding_text" in ft.columns:
        rows = ft[ft["report_id"] == report_id]["finding_text"].dropna()
        f_txt = " || ".join(rows.astype(str).tolist())[:1800]
    c_txt = ""
    if not cp.empty and "planned_action" in cp.columns:
        rows = cp[cp["report_id"] == report_id]["planned_action"].dropna()
        c_txt = " || ".join(rows.astype(str).tolist())[:1200]
    return f_txt, c_txt


def call_claude(prompt: str) -> dict:
    body = {
        "model": MODEL,
        "max_tokens": 1200,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    for attempt in range(4):
        r = requests.post(API_URL, headers=headers, json=body, timeout=120)
        if r.status_code in (429, 529):
            wait = 15 * (attempt + 1)
            print(f"    API busy ({r.status_code}), waiting {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        clean = re.sub(r"```json|```", "", text).strip()
        return json.loads(clean)
    raise RuntimeError("API retries exhausted")


def build_prompt(row, scored_row, f_txt, c_txt, cfg) -> str:
    return f"""You are writing a cold outreach email for a federal grant
compliance consultant. Respond ONLY with JSON: {{"subject": "...", "body": "..."}}
No markdown fences, no preamble.

SENDER (use exactly in signature):
{json.dumps(cfg, indent=2)}

PROSPECT FACTS (from public Federal Audit Clearinghouse filings):
- Organization: {row['org_name']} ({row['state']})
- Recipient: {row['contact_name']}, {row['contact_title']}
- Latest audit year: {row['latest_audit_year']}
- Core issue: {row['top_issue']}
- Persistent requirement letters (3+ years): {scored_row.get('persistent_letters','')}
- Material weakness: {scored_row.get('material_weakness','')}
- HHS share of federal spend: {scored_row.get('hhs_share','')}
- Their auditor: {row['auditor_firm']}
- Recommended service: {row['recommended_service']}
- Findings text excerpt: {f_txt or '(none)'}
- Their CAP excerpt: {c_txt or '(none)'}

RULES:
- 140-200 words in the body. Professional, direct, zero fluff.
- Reference their SPECIFIC finding/requirement area and how many years it has
  recurred. Show we read their filing; never sound like a mail merge.
- Position as COMPLEMENTARY to their auditor {row['auditor_firm']} (independence
  rules prevent auditors from remediating their own findings) — never
  disparage the auditor.
- One concrete offer: a brief no-cost call to walk through a remediation
  roadmap for the specific issue.
- Mention the sender's relevant credential/track record in one clause max.
- If their CAP excerpt looks like unedited template language, allude gently
  ("teams stretched thin often inherit boilerplate CAPs") without quoting it.
- End with the sender's full signature block INCLUDING the physical mailing
  address, and this exact line: "If you'd prefer not to receive emails from
  me, just reply 'unsubscribe' and I won't contact you again."
- Subject line: specific and non-spammy, under 9 words, mention their program
  area or finding topic. No ALL CAPS, no exclamation marks."""


def main():
    DRAFTS.mkdir(parents=True, exist_ok=True)

    if not API_KEY:
        print("ANTHROPIC_API_KEY not set — skipping email drafting.")
        return
    if not CONFIG.exists():
        print("sender_config.json missing — skipping email drafting.")
        return
    cfg = json.loads(CONFIG.read_text())
    if any("FILL_ME" in str(v) for v in cfg.values()):
        print("sender_config.json still has FILL_ME placeholders — "
              "skipping drafting until it's completed.")
        return
    if not TRACKER.exists():
        print("No pipeline tracker yet — run score_prospects.py first.")
        return

    tracker = pd.read_csv(TRACKER, dtype=str).fillna("")
    scored = pd.read_csv(SCORED, dtype=str).fillna("") if SCORED.exists() \
        else pd.DataFrame()
    todo = tracker[tracker["status"] == "new_prospect"].copy()
    todo["score_num"] = pd.to_numeric(todo["score"], errors="coerce").fillna(0)
    todo = todo.sort_values("score_num", ascending=False)
    if MAX_DRAFTS > 0:
        todo = todo.head(MAX_DRAFTS)
    print(f"Drafting {len(todo)} emails (model={MODEL})...")

    done = 0
    for idx, row in todo.iterrows():
        if not row["contact_email"] or "@" not in row["contact_email"]:
            tracker.loc[idx, ["status", "last_action", "last_action_date"]] = \
                ["needs_contact_research", "no_email_in_fac_filing",
                 date.today().isoformat()]
            continue
        srow = {}
        if not scored.empty:
            m = scored[scored["entity_id"] == row["entity_id"]]
            if not m.empty:
                srow = m.iloc[0].to_dict()
        try:
            f_txt, c_txt = report_texts(row["latest_report_id"],
                                        row["latest_audit_year"])
            result = call_claude(build_prompt(row, srow, f_txt, c_txt, cfg))
            slug = re.sub(r"[^a-z0-9]+", "-",
                          row["org_name"].lower())[:50].strip("-")
            fname = f"{date.today().isoformat()}_{slug}.md"
            (DRAFTS / fname).write_text(
                f"# Draft — {row['org_name']}\n\n"
                f"**To:** {row['contact_name']} <{row['contact_email']}>\n"
                f"**From:** {cfg.get('email','')}\n"
                f"**Subject:** {result['subject']}\n\n---\n\n"
                f"{result['body']}\n\n---\n\n"
                f"## Prospect facts used\n"
                f"- Score: {row['score']} | {row['top_issue']}\n"
                f"- Service: {row['recommended_service']}\n"
                f"- Auditor: {row['auditor_firm']}\n"
                f"- Report: {row['latest_report_id']}\n")
            tracker.loc[idx, ["status", "last_action", "last_action_date",
                              "draft_file"]] = \
                ["email_drafted", "draft_generated",
                 date.today().isoformat(), f"outreach/drafts/{fname}"]
            done += 1
            print(f"  drafted: {row['org_name']}")
            time.sleep(1.5)
        except Exception as e:
            print(f"  FAILED {row['org_name']}: {e}")
            tracker.loc[idx, "notes"] = f"draft_error: {e}"[:200]

    tracker.drop(columns=["score_num"], errors="ignore").to_csv(
        TRACKER, index=False)
    print(f"Done: {done} drafts written to outreach/drafts/")


if __name__ == "__main__":
    main()
