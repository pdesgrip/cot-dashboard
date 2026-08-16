#!/usr/bin/env python3
"""
COT Positioning Percentiles dashboard.

Pulls CFTC Commitments of Traders data (Socrata public API), computes the
mapped cohort's net positioning percentile per asset over all-time / 5y / 1y
windows (net/OI normalised + nominal), and writes a self-contained HTML
report that flags extremes.

Usage:
    pip install requests pandas
    python cot_dashboard.py                    # full dashboard -> cot_dashboard.html
    python cot_dashboard.py --extremes-only    # only rows at an extreme
    python cot_dashboard.py --threshold 5      # tighter extreme definition
    python cot_dashboard.py --search ether     # look up contract codes by name
    python cot_dashboard.py --mock             # synthetic data (design/testing)

Data notes:
- Financials (rates, crypto, VIX, indices, FX) come from the Traders in
  Financial Futures (TFF) report -> cohorts: 'lev' (Leveraged Funds) or
  'am' (Asset Manager).
- Physical commodities come from the Disaggregated report -> cohort 'mm'
  (Managed Money).
- Weekly data, Tuesday snapshot, released Friday 3:30pm ET.
"""

import argparse
import datetime as dt
import html
import sys

import pandas as pd

try:
    import requests
except ImportError:
    requests = None

# ---------------------------------------------------------------- config ---

API = {
    # Socrata dataset ids on publicreporting.cftc.gov
    "tff":    "gpe5-46if",   # Traders in Financial Futures, futures only
    "disagg": "72hh-3qpy",   # Disaggregated, futures only
    # combined futures+options variants, enabled with --combined
    "tff_c":    "yw9f-hn96",
    "disagg_c": "kh3c-gbw2",
}

COHORT_LABEL = {"mm": "MM", "lev": "LS", "am": "AM"}

# (display name, group, dataset, cftc contract code, cohort)
# Codes can be verified/found with:  python cot_dashboard.py --search <name>
MARKETS = [
    # --- rates ---
    ("TU (2y)",      "RATES",  "tff", "042601", "lev"),
    ("FV (5y)",      "RATES",  "tff", "044601", "lev"),
    ("TY (10y)",     "RATES",  "tff", "043602", "lev"),
    ("UXY (U10)",    "RATES",  "tff", "043607", "lev"),
    ("US (Bond)",    "RATES",  "tff", "020601", "lev"),
    ("WN (U-Bond)",  "RATES",  "tff", "020604", "lev"),
    ("SOFR 3M",      "RATES",  "tff", "134741", "lev"),
    ("Fed Funds",    "RATES",  "tff", "045601", "lev"),
    # --- volatility ---
    ("VIX",          "VOLATILITY", "tff", "1170E1", "lev"),
    # --- crypto (CME only!) ---
    ("Bitcoin",      "CRYPTO", "tff", "133741", "lev"),
    ("Micro BTC",    "CRYPTO", "tff", "133742", "lev"),
    # Ether's code varies by listing — run `--search ether` once and paste it in:
    # ("Ether",      "CRYPTO", "tff", "XXXXXX", "lev"),

    # --- rest of the board (comment out any you do not want) ---
    ("SPX (ES)",   "EQUITY INDICES", "tff", "13874A", "am"),
    ("Nasdaq (NQ)","EQUITY INDICES", "tff", "209742", "am"),
    ("Dow (YM)",   "EQUITY INDICES", "tff", "124603", "am"),
    ("Russell 2k", "EQUITY INDICES", "tff", "239742", "am"),
    ("Gold",       "METALS", "disagg", "088691", "mm"),
    ("Silver",     "METALS", "disagg", "084691", "mm"),
    ("Copper",     "METALS", "disagg", "085692", "mm"),
    ("WTI",        "ENERGY", "disagg", "067651", "mm"),
    ("EUR",        "FX", "tff", "099741", "lev"),
    ("JPY",        "FX", "tff", "097741", "lev"),
]

START_DATE = "2006-06-13"          # earliest disagg/TFF history
W5Y, W1Y = 261, 52                 # weeks per window

# --------------------------------------------------------------- helpers ---

def col(df, *tokens):
    """Resolve a column whose name contains all tokens (Socrata field names
    drift slightly between datasets)."""
    for c in df.columns:
        if all(t in c for t in tokens):
            return c
    raise KeyError(f"no column matching {tokens}; got {list(df.columns)}")


def pctile(series: pd.Series) -> float:
    """Percentile rank of the last value within the series (inclusive)."""
    s = series.dropna()
    if len(s) < 8:
        return float("nan")
    return round(float(s.rank(pct=True).iloc[-1] * 100))


def fetch(dataset_id: str, codes: list[str]) -> pd.DataFrame:
    quoted = ",".join(f"'{c}'" for c in codes)
    url = f"https://publicreporting.cftc.gov/resource/{dataset_id}.json"
    params = {
        "$where": (f"cftc_contract_market_code in({quoted}) "
                   f"AND report_date_as_yyyy_mm_dd >= '{START_DATE}'"),
        "$order": "report_date_as_yyyy_mm_dd",
        "$limit": "500000",
    }
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df[col(df, "report_date")])
    return df


def search_contracts(term: str):
    for name, ds in (("TFF", API["tff"]), ("Disaggregated", API["disagg"])):
        url = f"https://publicreporting.cftc.gov/resource/{ds}.json"
        params = {
            "$select": "distinct contract_market_name, cftc_contract_market_code",
            "$where": f"upper(contract_market_name) like '%{term.upper()}%'",
            "$limit": "50",
        }
        r = requests.get(url, params=params, timeout=60)
        r.raise_for_status()
        print(f"\n[{name}]")
        for row in r.json():
            print(f"  {row.get('cftc_contract_market_code'):>8}  "
                  f"{row.get('contract_market_name')}")


def mock_history(seed: int) -> pd.DataFrame:
    import numpy as np
    rng = np.random.default_rng(seed)
    dates = pd.date_range(end=dt.date.today(), periods=700, freq="W-TUE")
    n = len(dates)
    net = pd.Series(rng.normal(0, 8000, n)).cumsum() + rng.normal(0, 40000)
    oi = pd.Series(abs(rng.normal(0, 5000, n)).cumsum() + 200000)
    return pd.DataFrame({"date": dates, "net": net, "oi": oi})

# ----------------------------------------------------------- computation ---

def cohort_net(df: pd.DataFrame, cohort: str) -> pd.DataFrame:
    prefix = {"mm": ("m_money",), "lev": ("lev_money",), "am": ("asset_mgr",)}[cohort]
    long_c = col(df, *prefix, "long")
    short_c = col(df, *prefix, "short")
    oi_c = col(df, "open_interest")
    out = pd.DataFrame({
        "date": df["date"],
        "net": pd.to_numeric(df[long_c]) - pd.to_numeric(df[short_c]),
        "oi": pd.to_numeric(df[oi_c]),
    })
    return out.sort_values("date").reset_index(drop=True)


HIST_WEEKS = 52     # sparkline length + how far back to track streaks


def pct_series(hist: pd.DataFrame, weeks: int) -> dict:
    """Percentiles for the last `weeks` observations, each computed as of
    that week (expanding for all-time, trailing windows for 5y/1y)."""
    net_oi, net = hist["net_oi"], hist["net"]
    n = len(hist)
    out = {k: [] for k in ("p_all", "p_5y", "p_1y", "n_5y", "n_1y")}
    for i in range(max(1, n - weeks + 1), n + 1):
        out["p_all"].append(pctile(net_oi.iloc[:i]))
        out["p_5y"].append(pctile(net_oi.iloc[max(0, i - W5Y):i]))
        out["p_1y"].append(pctile(net_oi.iloc[max(0, i - W1Y):i]))
        out["n_5y"].append(pctile(net.iloc[max(0, i - W5Y):i]))
        out["n_1y"].append(pctile(net.iloc[max(0, i - W1Y):i]))
    return out


def extreme_at(pcts: dict, i: int, thr: int) -> bool:
    for k in pcts:
        v = pcts[k][i]
        if pd.notna(v) and (v <= thr or v >= 100 - thr):
            return True
    return False


def compute_row(hist: pd.DataFrame, thr: int) -> dict:
    hist = hist.copy()
    hist["net_oi"] = hist["net"] / hist["oi"]
    pcts = pct_series(hist, HIST_WEEKS)
    last = -1
    r = {
        "date": hist["date"].iloc[-1],
        "prev_date": hist["date"].iloc[-2] if len(hist) > 1 else None,
        "net": hist["net"].iloc[-1],
        "wow": hist["net"].iloc[-1] - hist["net"].iloc[-2] if len(hist) > 1 else 0,
        "spark": hist["net_oi"].tail(HIST_WEEKS).tolist(),
        "spark_pct": pcts["p_all"],
    }
    for k in pcts:
        r[k] = pcts[k][last]
        r[k + "_prev"] = pcts[k][-2] if len(pcts[k]) > 1 else float("nan")

    # consecutive weeks (ending now) at an extreme in any window
    streak = 0
    for i in range(len(pcts["p_all"]) - 1, -1, -1):
        if extreme_at(pcts, i, thr):
            streak += 1
        else:
            break
    r["streak"] = streak
    r["extreme"] = streak > 0
    r["new"] = streak == 1
    return r

# --------------------------------------------------------------- render ----

CSS = """
:root{--ink:#16181d;--mut:#8b909a;--line:#e7e8ea;--grp:#a3a7ae;
 --red:#f0a9b0;--grn:#a8dcb5;--bg:#fff}
*{box-sizing:border-box}
body{margin:0;padding:40px 48px;background:var(--bg);color:var(--ink);
 font:14px/1.45 -apple-system,'Segoe UI',Inter,Roboto,sans-serif}
h1{font-size:22px;font-weight:800;letter-spacing:-.01em;margin:0}
.sub{color:var(--mut);font-size:12.5px;margin:6px 0 26px}
.sub a{color:var(--ink);text-decoration:underline dotted}
table{border-collapse:collapse;width:100%;max-width:1240px;
 font-variant-numeric:tabular-nums}
th{font-size:11px;font-weight:600;color:var(--mut);text-align:center;
 padding:6px 10px;border-bottom:1px solid var(--ink)}
th.l,td.l{text-align:left}
th.grp-head{border-bottom:none;font-weight:400}
td{padding:5px 10px;text-align:center;border-bottom:1px solid var(--line);
 font-size:13px}
td.name{font-weight:700}
td.cohort,td.wow{color:var(--mut);font-size:11.5px}
tr.group td{color:var(--grp);font-size:10.5px;font-weight:700;
 letter-spacing:.08em;text-transform:uppercase;padding-top:16px;
 border-bottom:none}
.pill{display:block;border-radius:3px;padding:1px 0;font-weight:600}
.hi{background:var(--red)}
.lo{background:var(--grn)}
.legend{display:flex;gap:28px;margin-top:18px;font-size:11.5px;color:var(--mut)}
.sw{display:inline-block;width:34px;height:11px;border-radius:3px;
 vertical-align:-1px;margin-right:7px}
.foot{color:var(--mut);font-size:11px;margin-top:8px;max-width:1080px}
.arr{font-size:9px;font-weight:600;margin-left:3px;opacity:.75}
td.sp{padding:2px 6px}
td.sp svg{display:block;margin:0 auto;overflow:visible}
.line{fill:none;stroke:#8b909a;stroke-width:1.1}
.zero{stroke:#dcdde0;stroke-width:1;stroke-dasharray:2 2}
.dhi{fill:#d9535f}.dlo{fill:#3ea862}.dcur{fill:#16181d}
td.st{padding:2px 6px}
.tag{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.04em;
 color:var(--mut);border:1px solid var(--line);border-radius:3px;padding:1px 6px}
.tag.new{color:#fff;background:var(--ink);border-color:var(--ink)}
@media print{body{padding:12px}}
"""

def cell(v, thr, prev=float("nan")):
    """Percentile cell; extremes get a pill, and a small arrow shows the
    week-on-week percentile change when it moved 5+ points."""
    if pd.isna(v):
        return "<td>–</td>"
    v = int(v)
    arrow = ""
    if pd.notna(prev):
        d = v - int(prev)
        if abs(d) >= 5:
            arrow = f'<span class="arr">{"▲" if d > 0 else "▼"}{abs(d)}</span>'
    if v >= 100 - thr:
        return f'<td><span class="pill hi">{v}{arrow}</span></td>'
    if v <= thr:
        return f'<td><span class="pill lo">{v}{arrow}</span></td>'
    return f"<td>{v}{arrow}</td>"


def sparkline(vals: list, pcts: list, thr: int, w=96, h=22) -> str:
    """Inline SVG of net/OI over the last year. Dots mark weeks that were at
    an all-time extreme; the last point is emphasised."""
    vals = [v for v in vals if pd.notna(v)]
    if len(vals) < 3:
        return "<td></td>"
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    n = len(vals)
    pts = [(2 + i * (w - 4) / (n - 1), 2 + (h - 4) * (1 - (v - lo) / rng))
           for i, v in enumerate(vals)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
    # zero line if the range crosses zero
    zero = ""
    if lo < 0 < hi:
        zy = 2 + (h - 4) * (1 - (0 - lo) / rng)
        zero = f'<line x1="0" x2="{w}" y1="{zy:.1f}" y2="{zy:.1f}" class="zero"/>'
    dots = ""
    off = len(pcts) - n
    for i, (x, y) in enumerate(pts[:-1]):
        p = pcts[i + off] if 0 <= i + off < len(pcts) else float("nan")
        if pd.notna(p) and p >= 100 - thr:
            dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.6" class="dhi"/>'
        elif pd.notna(p) and p <= thr:
            dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.6" class="dlo"/>'
    lx, ly = pts[-1]
    lp = pcts[-1] if pcts else float("nan")
    lcls = "dhi" if pd.notna(lp) and lp >= 100 - thr else \
           "dlo" if pd.notna(lp) and lp <= thr else "dcur"
    return (f'<td class="sp"><svg viewBox="0 0 {w} {h}" width="{w}" height="{h}">'
            f'{zero}<path d="{path}" class="line"/>{dots}'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.4" class="{lcls}"/></svg></td>')


def status_cell(r: dict) -> str:
    if not r["extreme"]:
        return '<td class="st"></td>'
    if r["new"]:
        return '<td class="st"><span class="tag new">NEW</span></td>'
    if r["streak"] >= HIST_WEEKS:
        return f'<td class="st"><span class="tag">{HIST_WEEKS}+ wk</span></td>'
    return f'<td class="st"><span class="tag">{r["streak"]} wk</span></td>'


def render(rows: list[dict], thr: int, extremes_only: bool, out: str, nav: bool = False):
    latest = max(r["date"] for _, _, _, r in rows)
    prev = max((r["prev_date"] for _, _, _, r in rows if r["prev_date"] is not None),
               default=None)

    def is_extreme(r):
        return r["extreme"]

    body, last_group = [], None
    shown = 0
    for name, group, cohort, r in rows:
        if extremes_only and not is_extreme(r):
            continue
        if group != last_group:
            body.append(f'<tr class="group"><td colspan="11" class="l">{group}</td></tr>')
            last_group = group
        wow = f"{r['wow']:+,.0f}"
        body.append(
            '<tr>'
            f'<td class="name l">{html.escape(name)}</td>'
            f'<td class="cohort">{COHORT_LABEL[cohort]}</td>'
            f'<td class="wow">{wow}</td>'
            + status_cell(r)
            + sparkline(r["spark"], r["spark_pct"], thr)
            + cell(r["p_all"], thr, r["p_all_prev"]) + cell(r["p_5y"], thr, r["p_5y_prev"])
            + cell(r["p_1y"], thr, r["p_1y_prev"])
            + cell(r["n_5y"], thr, r["n_5y_prev"]) + cell(r["n_1y"], thr, r["n_1y_prev"])
            + "</tr>")
        shown += 1

    if extremes_only and shown == 0:
        body.append('<tr><td colspan="11" class="l" style="color:var(--mut)">'
                    'No assets at an extreme this week.</td></tr>')

    title = "COT Positioning Extremes" if extremes_only else "COT Positioning Percentiles"
    sub = f"week ending {latest:%d %b %Y}"
    if prev is not None:
        sub += f" &nbsp;·&nbsp; Δ vs {prev:%d %b %Y}"
    sub += " &nbsp;·&nbsp; one cohort per asset"
    if nav:
        a, b = ("index.html", "extremes.html")
        sub += (' &nbsp;·&nbsp; '
                + (f'<a href="{a}">full board</a>' if extremes_only else '<b>full board</b>')
                + ' / '
                + ('<b>extremes only</b>' if extremes_only else f'<a href="{b}">extremes only</a>'))

    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{CSS}</style></head><body>
<h1>{title}</h1><div class="sub">{sub}</div>
<table>
<tr><th class="l" style="border-bottom:none"></th><th class="grp-head"></th>
<th class="grp-head"></th><th class="grp-head"></th><th class="grp-head"></th>
<th colspan="3" class="grp-head">net / OI &nbsp;%ile</th>
<th colspan="2" class="grp-head">nominal &nbsp;%ile</th></tr>
<tr><th class="l">Asset</th><th>Cohort</th><th>Δ wk</th><th>Status</th>
<th>1y net/OI</th><th>All-time</th><th>5y</th><th>1y</th><th>5y</th><th>1y</th></tr>
{''.join(body)}
</table>
<div class="legend">
<span><span class="sw" style="background:var(--grn)"></span>bottom {thr}% · net-short extreme</span>
<span><span class="sw" style="background:var(--red)"></span>top {thr}% · net-long extreme</span>
</div>
<div class="foot">Percentile of the mapped cohort's net positioning within each
window · net/OI normalises for OI growth · nominal ranks raw net contracts
(5y/1y only) · Δ wk = week-on-week change in net contracts · Status = consecutive
weeks at an extreme (NEW = entered this week) · sparkline = net/OI over the last
year, dots mark weeks at an all-time extreme · ▲▼ = percentile change vs last
week (shown when ≥5) · cohorts:
MM = Managed Money (disaggregated), LS = Leveraged Funds, AM = Asset Manager
(TFF) · source: CFTC</div>
</body></html>"""
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"wrote {out}  ({shown or len(rows)} rows, week ending {latest:%Y-%m-%d})")

# ------------------------------------------------------------------ main ---

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="cot_dashboard.html")
    ap.add_argument("--threshold", type=int, default=10,
                    help="extreme = <= N or >= 100-N percentile (default 10)")
    ap.add_argument("--extremes-only", action="store_true")
    ap.add_argument("--combined", action="store_true",
                    help="use futures+options combined reports")
    ap.add_argument("--search", metavar="TERM",
                    help="look up contract names/codes containing TERM, then exit")
    ap.add_argument("--mock", action="store_true",
                    help="synthetic data, no network (design/testing)")
    ap.add_argument("--nav", action="store_true",
                    help="add full-board / extremes-only links (for the published site)")
    args = ap.parse_args()

    if args.search:
        search_contracts(args.search)
        return

    rows = []
    if args.mock:
        for i, (name, group, _ds, _code, cohort) in enumerate(MARKETS):
            rows.append((name, group, cohort, compute_row(mock_history(i), args.threshold)))
    else:
        if requests is None:
            sys.exit("pip install requests pandas")
        for ds_key in ("tff", "disagg"):
            wanted = [m for m in MARKETS if m[2] == ds_key]
            if not wanted:
                continue
            ds_id = API[ds_key + "_c"] if args.combined else API[ds_key]
            df = fetch(ds_id, [m[3] for m in wanted])
            for name, group, _ds, code, cohort in wanted:
                sub = df[df[col(df, "contract_market_code")] == code]
                if sub.empty:
                    print(f"  ! no data for {name} (code {code}) — "
                          f"check with --search", file=sys.stderr)
                    continue
                rows.append((name, group, cohort, compute_row(cohort_net(sub, cohort), args.threshold)))

    if not rows:
        sys.exit("no data fetched")
    order = {g: i for i, g in enumerate(dict.fromkeys(m[1] for m in MARKETS))}
    rows.sort(key=lambda r: order.get(r[1], 99))
    render(rows, args.threshold, args.extremes_only, args.out, args.nav)


if __name__ == "__main__":
    main()
