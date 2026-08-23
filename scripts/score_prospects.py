#!/usr/bin/env python3
"""
COA prospect scoring engine — rebuilds the full AY2021-2026 longitudinal
analysis every week from files already in the repo.

Data sources (per audit year, first match wins):
  1. data/current/{table}_AY{year}.csv.gz   (weekly API refresh — freshest)
  2. {year}-ay-{table}.csv / .zip           (bulk downloads at repo root)

Methodology (0-100 Remediation Opportunity Score):
  30  severity of current findings
  25  repeat / persistent findings (incl. same-requirement-letter 3+ years)
  15  HHS relevance
  15  size & complexity fit for a small consulting firm
  10  apparent outsourceable need (CAP quality, breadth, risk posture)
   5  recency

Outputs:
  analysis/prospects_scored.csv.gz   full scored universe
  analysis/top50.csv                 ranked Top 50 with narrative rationale
  analysis/summary.md                tier counts + methodology notes
  data/tier1_targets.csv             feeds the PDF/990 enrichment step
  outreach/pipeline_tracker.csv      NEW Tier 1 prospects appended as
                                     status=new_prospect (existing rows and
                                     statuses are never modified)
"""

import re
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CURRENT = ROOT / "data" / "current"
ANALYSIS = ROOT / "analysis"
OUTREACH = ROOT / "outreach"
TRACKER = OUTREACH / "pipeline_tracker.csv"

YEARS = [2021, 2022, 2023, 2024, 2025, 2026]
# Target agency ALN prefixes. HHS = "93". (Add "14" to re-include HUD.)
TARGET_AGENCIES = ("93",)

TRACKER_COLS = [
    "entity_id", "org_name", "state", "ein", "uei", "latest_report_id",
    "latest_audit_year", "score", "tier", "top_issue", "recommended_service",
    "contact_name", "contact_title", "contact_email", "contact_phone",
    "auditor_firm", "status", "date_added", "last_action", "last_action_date",
    "draft_file", "notes",
]

SERVICE_MAP = {
    "A": "Grant compliance review (activities allowed/unallowed)",
    "B": "Allowable-cost review and cost policy development",
    "C": "Cash management compliance and drawdown procedures",
    "E": "Eligibility determination controls and documentation",
    "F": "Equipment/real property management procedures",
    "G": "Matching and level-of-effort compliance support",
    "H": "Period of performance controls",
    "I": "Procurement compliance and policy development",
    "J": "Program income accounting procedures",
    "L": "Grant reporting controls and report preparation support",
    "M": "Subrecipient monitoring program development",
    "N": "Special tests compliance remediation",
    "P": "Audit finding remediation and CAP implementation",
}

TEMPLATE_PHRASES = [
    "the auditee will", "management will implement", "we concur",
    "corrective action plan", "will be implemented", "n/a", "none",
]


def find_file(year: int, table: str) -> Path | None:
    candidates = [
        CURRENT / f"{table}_AY{year}.csv.gz",
        ROOT / f"{year}-ay-{table}.csv",
        ROOT / f"{year}-ay-{table}.zip",
    ]
    for c in candidates:
        if c.exists() and c.stat().st_size > 100:
            return c
    return None


def load(year: int, table: str, usecols=None) -> pd.DataFrame:
    path = find_file(year, table)
    if path is None:
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, dtype=str, low_memory=False,
                         usecols=lambda c: (usecols is None or c in usecols))
        df["audit_year"] = str(year)
        return df
    except Exception as e:
        print(f"  WARNING: failed to read {path.name}: {e}")
        return pd.DataFrame()


def yn(series) -> pd.Series:
    return series.astype(str).str.strip().str.upper().isin(["Y", "YES", "TRUE"])


def norm_name(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", "", str(s).lower())
    s = re.sub(r"\b(inc|incorporated|corp|corporation|llc|the|of|and|dba)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- entity link
class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def link_entities(gen: pd.DataFrame) -> pd.DataFrame:
    """Assign entity_id via union-find on UEI, EIN, and normalized name+state."""
    g = gen.copy()
    # Pre-clean key columns BEFORE the loop
    g["k_uei"] = g["auditee_uei"].astype(str).str.strip().str.upper()
    g.loc[g["k_uei"].isin(["", "NAN", "NONE", "GSA_MIGRATION"]), "k_uei"] = ""
    g["k_ein"] = g["auditee_ein"].astype(str).str.replace(r"\D", "", regex=True)
    g.loc[g["k_ein"].str.len() != 9, "k_ein"] = ""
    g["k_name"] = (g["auditee_name"].map(norm_name) + "|" +
                   g["auditee_state"].astype(str).str.upper())

    uf = UnionFind()
    for _, row in g[["report_id", "k_uei", "k_ein", "k_name"]].iterrows():
        rid = "R:" + row["report_id"]
        for prefix, key in (("U:", row["k_uei"]), ("E:", row["k_ein"]),
                            ("N:", row["k_name"])):
            if key:
                uf.union(rid, prefix + key)
    g["entity_id"] = ("R:" + g["report_id"]).map(uf.find)
    return g


# -------------------------------------------------------------------- scoring
def cap_quality_penalty(planned: str) -> bool:
    """True if CAP text looks like unedited template / minimal effort."""
    t = str(planned).strip().lower()
    if len(t) < 120:
        return True
    hits = sum(1 for p in TEMPLATE_PHRASES if p in t)
    return hits >= 2 and len(t) < 400


def score_entity(e: dict) -> dict:
    latest = e["latest_finding_year"]  # last year WITH findings, may be None
    newest = e["latest_year"]

    # --- Severity (30): based on most recent year with findings
    sev = 0
    if latest is not None:
        y = e["by_year"][latest]
        sev = 10
        if y["mw"]:
            sev += 10
        if y["sd"]:
            sev += 6
        if y["qc"]:
            sev += 4
        if y["mod_op"]:
            sev += 4
        if y["mat_noncomp"]:
            sev += 3
        # stale problems decay
        gap = int(newest) - int(latest)
        sev = max(0, sev - 6 * gap)
    sev = min(30, sev)

    # --- Repeat / persistence (25)
    rep = 0
    persistent = [ltr for ltr, yrs in e["letter_years"].items() if len(yrs) >= 3]
    if persistent:
        rep += 10 + min(6, 3 * (len(persistent) - 1))
    yrs_with_findings = sum(1 for y in e["by_year"].values() if y["n_findings"] > 0)
    if yrs_with_findings >= 3:
        rep += 5
    if latest is not None and e["by_year"][latest]["repeat_flag"]:
        rep += 4
    rep = min(25, rep)

    # --- HHS relevance (15)
    hh = 0
    share = e["hhs_share"]
    hh += round(8 * min(1.0, share * 1.4))          # expenditure concentration
    if e["hhs_finding_ever"]:
        hh += 4
    if e["hhs_major_finding"]:
        hh += 3
    hh = min(15, hh)

    # --- Small-firm fit (15)
    exp = e["latest_expend"]
    if 750_000 <= exp <= 10_000_000:
        fit = 15
    elif exp <= 30_000_000:
        fit = 12
    elif exp <= 75_000_000:
        fit = 8
    elif exp <= 200_000_000:
        fit = 4
    elif exp > 200_000_000:
        fit = 1
    else:
        fit = 6
    if e["n_major_latest"] > 6:
        fit -= 3
    if e["entity_type"] in ("state", "higher-ed"):
        fit -= 4
    fit = max(0, min(15, fit))

    # --- Outsourceable need (10)
    need = 0
    if e["weak_cap"]:
        need += 4
    if len(e["letters_latest"]) >= 3:
        need += 3
    if not e["low_risk"]:
        need += 3
    need = min(10, need)

    # --- Recency (5)
    if latest is None:
        rec = 0
    elif int(latest) >= 2025:
        rec = 5
    elif int(latest) == 2024:
        rec = 3
    else:
        rec = 1

    total = sev + rep + hh + fit + need + rec
    return {"score": total, "s_severity": sev, "s_repeat": rep, "s_hhs": hh,
            "s_fit": fit, "s_need": need, "s_recency": rec,
            "persistent_letters": "".join(sorted(persistent))}


def tier(row) -> str:
    if row["score"] >= 60 and row["s_hhs"] >= 6 and row["s_fit"] >= 8:
        return "Tier 1"
    if row["score"] >= 45 and row["s_hhs"] >= 4:
        return "Tier 2"
    if row["score"] >= 30:
        return "Tier 3"
    return "Not Recommended"


def recommended_service(persistent: str, letters_latest: str, mw: bool) -> str:
    for ltr in persistent:
        if ltr in SERVICE_MAP:
            return SERVICE_MAP[ltr] + " (persistent 3+ yrs)"
    if mw:
        return "Internal control assessment and material weakness remediation"
    for ltr in letters_latest:
        if ltr in SERVICE_MAP:
            return SERVICE_MAP[ltr]
    return "Corrective Action Plan development and implementation"


# ----------------------------------------------------------------------- main
def main():
    ANALYSIS.mkdir(exist_ok=True)
    OUTREACH.mkdir(exist_ok=True)

    gen_cols = {"report_id", "auditee_uei", "auditee_ein", "auditee_name",
                "auditee_state", "auditee_city", "auditee_email",
                "auditee_contact_name", "auditee_contact_title",
                "auditee_certify_name", "auditee_certify_title",
                "auditee_phone", "auditor_firm_name", "entity_type",
                "total_amount_expended", "is_going_concern_included",
                "is_material_noncompliance_disclosed", "is_low_risk_auditee",
                "audit_year"}
    awd_cols = {"report_id", "award_reference", "federal_agency_prefix",
                "federal_program_name", "amount_expended", "is_major",
                "audit_year"}
    fin_cols = {"report_id", "award_reference", "reference_number",
                "is_material_weakness", "is_significant_deficiency",
                "is_questioned_costs", "is_repeat_finding",
                "is_modified_opinion", "type_requirement", "audit_year"}
    cap_cols = {"report_id", "finding_ref_number", "planned_action",
                "audit_year"}

    gen_all, awd_all, fin_all, cap_all = [], [], [], []
    for y in YEARS:
        g = load(y, "general", gen_cols)
        if g.empty:
            print(f"AY{y}: no general file found — skipping year")
            continue
        print(f"AY{y}: {len(g)} filings")
        gen_all.append(g)
        awd_all.append(load(y, "federal_awards", awd_cols))
        fin_all.append(load(y, "findings", fin_cols))
        cap_all.append(load(y, "corrective_action_plans", cap_cols))

    if not gen_all:
        sys.exit("No data found in repo — run the refresh first.")

    gen = pd.concat(gen_all, ignore_index=True)
    awd = pd.concat([d for d in awd_all if not d.empty], ignore_index=True)
    fin = pd.concat([d for d in fin_all if not d.empty], ignore_index=True)
    cap = pd.concat([d for d in cap_all if not d.empty], ignore_index=True)

    # numeric conversions
    gen["total_amount_expended"] = pd.to_numeric(
        gen["total_amount_expended"], errors="coerce").fillna(0)
    awd["amount_expended"] = pd.to_numeric(
        awd["amount_expended"], errors="coerce").fillna(0)

    print("Linking entities (union-find on UEI / EIN / name+state)...")
    gen = link_entities(gen)
    print(f"  {gen['entity_id'].nunique()} distinct organizations "
          f"from {len(gen)} filings")

    # HHS expenditure per report
    awd["prefix"] = awd["federal_agency_prefix"].astype(str).str.strip()
    hhs_by_report = (awd[awd["prefix"].isin(TARGET_AGENCIES)]
                    .groupby("report_id")["amount_expended"].sum())
    major_by_report = (yn(awd["is_major"]).groupby(awd["report_id"]).sum())

    # link findings to agency prefix + major via award_reference
    fin = fin.merge(
        awd[["report_id", "award_reference", "prefix", "is_major",
             "federal_program_name"]],
        on=["report_id", "award_reference"], how="left")
    for col, flag in [("mw", "is_material_weakness"),
                      ("sd", "is_significant_deficiency"),
                      ("qc", "is_questioned_costs"),
                      ("rep", "is_repeat_finding"),
                      ("mod", "is_modified_opinion")]:
        fin[col] = yn(fin[flag])
    fin["hhs"] = fin["prefix"].isin(TARGET_AGENCIES)
    fin["major_flag"] = yn(fin["is_major"])

    # weak-CAP flag per report
    cap["weak"] = cap["planned_action"].map(cap_quality_penalty)
    weak_cap_by_report = cap.groupby("report_id")["weak"].mean() >= 0.5

    print("Building organization-year profiles...")
    fin_by_report = fin.groupby("report_id").agg(
        n_findings=("reference_number", "count"),
        mw=("mw", "any"), sd=("sd", "any"), qc=("qc", "any"),
        repeat_flag=("rep", "any"), mod_op=("mod", "any"),
        hhs_finding=("hhs", "any"),
        hhs_major=("major_flag",
                       lambda s: bool((s & fin.loc[s.index, "hhs"]).any())),
        letters=("type_requirement",
                 lambda s: "".join(sorted(set(
                     ch for v in s.dropna() for ch in str(v).upper()
                     if ch.isalpha())))),
    )

    entities = {}
    for _, r in gen.iterrows():
        eid = r["entity_id"]
        e = entities.setdefault(eid, {
            "by_year": {}, "letter_years": {}, "rows": []})
        rid = r["report_id"]
        f = fin_by_report.loc[rid] if rid in fin_by_report.index else None
        yr = r["audit_year"]
        rec = {
            "report_id": rid,
            "expend": float(r["total_amount_expended"]),
            "hh_expend": float(hhs_by_report.get(rid, 0.0)),
            "n_major": int(major_by_report.get(rid, 0)),
            "n_findings": int(f["n_findings"]) if f is not None else 0,
            "mw": bool(f["mw"]) if f is not None else False,
            "sd": bool(f["sd"]) if f is not None else False,
            "qc": bool(f["qc"]) if f is not None else False,
            "repeat_flag": bool(f["repeat_flag"]) if f is not None else False,
            "mod_op": bool(f["mod_op"]) if f is not None else False,
            "hhs_finding": bool(f["hhs_finding"]) if f is not None else False,
            "hhs_major": bool(f["hhs_major"]) if f is not None else False,
            "letters": f["letters"] if f is not None else "",
            "mat_noncomp": str(r.get("is_material_noncompliance_disclosed", "")).upper() == "Y",
            "going_concern": str(r.get("is_going_concern_included", "")).upper() == "Y",
            "low_risk": str(r.get("is_low_risk_auditee", "")).upper() == "Y",
            "weak_cap": bool(weak_cap_by_report.get(rid, False)),
            "meta": r,
        }
        e["by_year"][yr] = rec
        for ch in rec["letters"]:
            e["letter_years"].setdefault(ch, set()).add(yr)

    print(f"Scoring {len(entities)} organizations...")
    rows = []
    for eid, e in entities.items():
        years = sorted(e["by_year"])
        newest = years[-1]
        finding_years = [y for y in years if e["by_year"][y]["n_findings"] > 0]
        latest_f = finding_years[-1] if finding_years else None
        ny = e["by_year"][newest]
        total_exp = ny["expend"]
        hh_share = min(1.0, ny["hh_expend"] / total_exp) if total_exp > 0 else 0.0

        ctx = {
            "by_year": e["by_year"], "letter_years": e["letter_years"],
            "latest_year": newest, "latest_finding_year": latest_f,
            "hhs_share": hh_share,
            "hhs_finding_ever": any(v["hhs_finding"] for v in e["by_year"].values()),
            "hhs_major_finding": any(v["hhs_major"] for v in e["by_year"].values()),
            "latest_expend": total_exp,
            "n_major_latest": ny["n_major"],
            "entity_type": str(ny["meta"].get("entity_type", "")).lower(),
            "weak_cap": any(v["weak_cap"] for v in e["by_year"].values()),
            "low_risk": ny["low_risk"],
            "letters_latest": (e["by_year"][latest_f]["letters"] if latest_f else ""),
        }
        s = score_entity(ctx)
        m = ny["meta"]
        lf = e["by_year"][latest_f] if latest_f else None
        rows.append({
            "entity_id": eid,
            "org_name": m["auditee_name"],
            "state": m["auditee_state"],
            "ein": m["auditee_ein"], "uei": m["auditee_uei"],
            "entity_type": ctx["entity_type"],
            "latest_report_id": ny["report_id"],
            "latest_audit_year": newest,
            "years_available": ",".join(years),
            "total_federal_expend": round(total_exp),
            "hhs_expend": round(ny["hh_expend"]),
            "hhs_share": round(hh_share, 3),
            "latest_finding_year": latest_f or "",
            "n_findings_latest": lf["n_findings"] if lf else 0,
            "material_weakness": lf["mw"] if lf else False,
            "significant_deficiency": lf["sd"] if lf else False,
            "questioned_costs": lf["qc"] if lf else False,
            "modified_opinion": lf["mod_op"] if lf else False,
            "going_concern": ny["going_concern"],
            "requirement_letters_latest": ctx["letters_latest"],
            "weak_cap_language": ctx["weak_cap"],
            "contact_name": m.get("auditee_contact_name", ""),
            "contact_title": m.get("auditee_contact_title", ""),
            "contact_email": m.get("auditee_email", ""),
            "contact_phone": m.get("auditee_phone", ""),
            "auditor_firm": m.get("auditor_firm_name", ""),
            **s,
        })

    df = pd.DataFrame(rows)
    df["tier"] = df.apply(tier, axis=1)
    df["recommended_service"] = df.apply(
        lambda r: recommended_service(r["persistent_letters"],
                                      r["requirement_letters_latest"],
                                      r["material_weakness"]), axis=1)
    df = df.sort_values("score", ascending=False).reset_index(drop=True)
    df["rank"] = df.index + 1

    def why(r):
        bits = []
        if r["persistent_letters"]:
            bits.append(f"Requirement letter(s) {r['persistent_letters']} "
                        f"flagged 3+ audit years — unresolved systemic issue.")
        if r["material_weakness"]:
            bits.append("Material weakness in the most recent findings year.")
        if r["weak_cap_language"]:
            bits.append("CAP text appears templated/minimal, suggesting "
                        "limited internal remediation capability (inference).")
        if r["hhs_share"] >= 0.5:
            bits.append(f"{int(r['hhs_share']*100)}% of federal spend is "
                        f"HHS programs.")
        if r["going_concern"]:
            bits.append("Going-concern disclosure — structure engagement at "
                        "sponsor/management-agent level.")
        bits.append(f"~${r['total_federal_expend']:,.0f} federal expenditures "
                    f"fits a small specialist engagement.")
        return " ".join(bits)

    top50 = df.head(50).copy()
    top50["why_this_is_a_prospect"] = top50.apply(why, axis=1)

    df.to_csv(ANALYSIS / "prospects_scored.csv.gz", index=False,
              compression="gzip")
    top50.to_csv(ANALYSIS / "top50.csv", index=False)

    t1 = df[df["tier"] == "Tier 1"]
    t1[["org_name", "latest_report_id", "ein", "uei", "state"]].rename(
        columns={"latest_report_id": "report_id"}).to_csv(
        ROOT / "data" / "tier1_targets.csv", index=False)

    # ---- pipeline tracker: append NEW Tier 1 only, never touch existing rows
    if TRACKER.exists():
        tracker = pd.read_csv(TRACKER, dtype=str).fillna("")
    else:
        tracker = pd.DataFrame(columns=TRACKER_COLS)
    known = set(tracker["entity_id"]) | set(tracker["ein"])
    new_rows = []
    for _, r in t1.iterrows():
        if r["entity_id"] in known or (r["ein"] and r["ein"] in known):
            continue
        new_rows.append({
            "entity_id": r["entity_id"], "org_name": r["org_name"],
            "state": r["state"], "ein": r["ein"], "uei": r["uei"],
            "latest_report_id": r["latest_report_id"],
            "latest_audit_year": r["latest_audit_year"],
            "score": r["score"], "tier": r["tier"],
            "top_issue": (f"Letters {r['persistent_letters']} persistent"
                          if r["persistent_letters"] else
                          f"{r['n_findings_latest']} findings "
                          f"AY{r['latest_finding_year']}"),
            "recommended_service": r["recommended_service"],
            "contact_name": r["contact_name"],
            "contact_title": r["contact_title"],
            "contact_email": r["contact_email"],
            "contact_phone": r["contact_phone"],
            "auditor_firm": r["auditor_firm"],
            "status": "new_prospect", "date_added": date.today().isoformat(),
            "last_action": "identified_by_scoring",
            "last_action_date": date.today().isoformat(),
            "draft_file": "", "notes": "",
        })
    if new_rows:
        tracker = pd.concat([tracker, pd.DataFrame(new_rows)],
                            ignore_index=True)
    tracker.to_csv(TRACKER, index=False)

    counts = df["tier"].value_counts()
    summary = [
        f"# Prospect Analysis Summary — {date.today().isoformat()}", "",
        f"Organizations scored: {len(df)}",
        f"Tier 1: {counts.get('Tier 1', 0)} | Tier 2: {counts.get('Tier 2', 0)}"
        f" | Tier 3: {counts.get('Tier 3', 0)} | Not Recommended: "
        f"{counts.get('Not Recommended', 0)}", "",
        f"New prospects added to pipeline tracker this run: {len(new_rows)}",
        "",
        "Facts vs. inferences: expenditures, findings, severity flags, and",
        "requirement letters are FAC facts. CAP-quality, consulting need, and",
        "small-firm fit are inferences and labeled as such in top50.csv.",
    ]
    (ANALYSIS / "summary.md").write_text("\n".join(summary))
    print("\n".join(summary))


if __name__ == "__main__":
    main()
