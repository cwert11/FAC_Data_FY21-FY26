#!/usr/bin/env python3
"""
Estimate the API cost of pending email drafts BEFORE any spend occurs.
Writes the estimate to the GitHub Actions job summary (shown in the approval
email/run page) and exposes `pending` as a step output so the drafting job
can be skipped entirely when there is nothing to draft.
"""
import os
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TRACKER = ROOT / "outreach" / "pipeline_tracker.csv"

# USD per million tokens (input, output)
PRICES = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}
MODEL = os.environ.get("MODEL", "claude-sonnet-5")
IN_TOK, OUT_TOK = 2200, 400  # per-draft estimate

def main():
    pending = 0
    no_email = 0
    if TRACKER.exists():
        t = pd.read_csv(TRACKER, dtype=str).fillna("")
        new = t[t["status"] == "new_prospect"]
        has_email = new["contact_email"].str.contains("@", na=False)
        pending = int(has_email.sum())
        no_email = int((~has_email).sum())

    p_in, p_out = PRICES.get(MODEL, PRICES["claude-sonnet-5"])
    cost = pending * (IN_TOK * p_in + OUT_TOK * p_out) / 1_000_000
    # headroom for retries / long findings text
    cost_high = round(cost * 1.5, 2)
    cost = round(cost, 2)

    summary = f"""## Email Draft Cost Estimate

| Metric | Value |
|---|---|
| Pending prospects to draft | **{pending}** |
| Prospects lacking an email in FAC filing | {no_email} |
| Model | {MODEL} |
| Estimated tokens per draft | {IN_TOK} in / {OUT_TOK} out |
| **Estimated API cost** | **${cost} – ${cost_high}** |

Approving this deployment will generate {pending} email drafts and charge
the Anthropic API key accordingly. Rejecting or ignoring it costs nothing;
the prospects remain queued for a future run.
"""
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"pending={pending}\n")
            f.write(f"est_cost={cost}\n")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        Path(step_summary).write_text(summary)
    print(summary)

if __name__ == "__main__":
    main()
