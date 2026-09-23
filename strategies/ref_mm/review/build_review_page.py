#!/usr/bin/env python3
"""Build the local HTML review page (spec section 2).

Per pair: both titles, rules excerpts, outcome labels, the side alignment and its reason, the
deterministic check summary, and the LLM reason. For KAT-3 flagged pairs only, an overlay of
Kalshi mid and FV. The page restates that a rejection must cite a rules-based reason. Decisions
are kept in the browser (localStorage) and exported as data/approved_pairs.csv.

Usage: .venv/bin/python -m strategies.ref_mm.review.build_review_page
  reads data/review_sample.csv, data/candidate_pairs.csv, data/kat3.csv (optional)
  writes data/review/index.html
"""

from __future__ import annotations

import argparse
import csv
import html
import json
from datetime import UTC
from pathlib import Path
from typing import Any

from pmcore.data.holdout import repo_root

CSS = """
:root { color-scheme: light; --surface: #fcfcfb; --surface-1: #ffffff; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781; --line: #e6e5e0;
  --series-1: #2a78d6; --series-2: #eb6834; --good: #008300; --bad: #e34948; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark; --surface: #1a1a19; --surface-1: #232322; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781; --line: #3a3a38; --series-1: #3987e5; --series-2: #d95926; --good: #4caf50; --bad: #e66767; } }
:root[data-theme="dark"] { color-scheme: dark; --surface: #1a1a19; --surface-1: #232322; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781; --line: #3a3a38; --series-1: #3987e5; --series-2: #d95926; --good: #4caf50; --bad: #e66767; }
body { margin: 0; padding: 16px; background: var(--surface); color: var(--ink); font: 14px/1.45 system-ui, sans-serif; }
header { position: sticky; top: 0; background: var(--surface); padding: 8px 0; border-bottom: 1px solid var(--line); z-index: 2; }
.rule { background: var(--surface-1); border: 1px solid var(--line); border-left: 4px solid var(--series-2); padding: 8px 12px; margin: 8px 0; }
.card { background: var(--surface-1); border: 1px solid var(--line); border-radius: 6px; padding: 12px; margin: 12px 0; }
.card.flagged { border-left: 4px solid var(--bad); }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; } @media (max-width: 800px) { .grid { grid-template-columns: 1fr; } }
h3 { margin: 0 0 4px; font-size: 15px; } .k { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
pre { white-space: pre-wrap; font: 12px/1.4 ui-monospace, monospace; color: var(--ink-2); background: transparent; max-height: 160px; overflow: auto; margin: 4px 0; }
.dec { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-top: 8px; } .dec input[type=text] { flex: 1; min-width: 240px; padding: 6px; }
.badge { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 12px; border: 1px solid var(--line); color: var(--ink-2); }
button { padding: 6px 12px; } .count { color: var(--ink-2); }
svg text { fill: var(--muted); font-size: 11px; } .legend { display: flex; gap: 16px; font-size: 12px; color: var(--ink-2); margin: 4px 0; }
.sw { display: inline-block; width: 12px; height: 3px; vertical-align: middle; margin-right: 4px; border-radius: 2px; }
.tip { position: absolute; pointer-events: none; background: var(--surface-1); border: 1px solid var(--line); padding: 4px 8px; font-size: 12px; color: var(--ink); display: none; }
table.tv { border-collapse: collapse; font-size: 12px; } table.tv td, table.tv th { border-bottom: 1px solid var(--line); padding: 2px 8px; text-align: right; }
"""

JS = """
const KEY='ref_mm_review_v1'; let state={}; try{state=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){state={}}
function save(){ try{localStorage.setItem(KEY, JSON.stringify(state))}catch(e){} refresh(); }
function setDec(id,v){ state[id]=Object.assign(state[id]||{},{decision:v}); save(); }
function setReason(id,v){ state[id]=Object.assign(state[id]||{},{reason:v}); save(); }
function setReasonCode(id,v){ state[id]=Object.assign(state[id]||{},{reason_code:v}); save(); }
function refresh(){ let a=0,r=0,m=0; for(const id of PAIR_IDS){ const s=state[id]||{}; if(s.decision==='approve')a++; else if(s.decision==='reject')r++; else m++;
  const el=document.getElementById('st-'+id); if(el){ el.textContent = s.decision? s.decision.toUpperCase() : 'undecided'; }
  const rin=document.querySelector('input[name="d-'+id+'"][value="'+(s.decision||'')+'"]'); if(rin) rin.checked=true;
  const tx=document.getElementById('r-'+id); if(tx && s.reason!==undefined && tx.value!==s.reason) tx.value=s.reason;
  const sel=document.getElementById('c-reason-'+id); if(sel && s.reason_code!==undefined && sel.value!==s.reason_code) sel.value=s.reason_code; }
  document.getElementById('counts').textContent=`approved ${a} · rejected ${r} · undecided ${m} of ${PAIR_IDS.length}`; }
function exportCsv(){ const rows=[['pair_id','kalshi_ticker','poly_market_id','condition_id','reference_token_id','reference_outcome','decision','reason_code','reason_quote','reviewed_at']];
  const bad=[]; for(const p of PAIRS){ const s=state[p.pair_id]||{}; if(!s.decision){bad.push(p.pair_id+' (undecided)');continue;}
    if(s.decision==='reject' && !(s.reason_code||'')){bad.push(p.pair_id+' (reject without a rules-based reason code)');continue;}
    rows.push([p.pair_id,p.kalshi_ticker,p.poly_market_id,p.condition_id,p.reference_token_id,p.reference_outcome,s.decision,s.reason_code||'',s.reason||'',new Date().toISOString()]); }
  if(bad.length){ alert('Cannot export. Fix:\\n'+bad.slice(0,20).join('\\n')+(bad.length>20?'\\n...':'')); return; }
  const csv=rows.map(r=>r.map(v=>'"'+String(v).replace(/"/g,'""')+'"').join(',')).join('\\n');
  const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'})); a.download='approved_pairs.csv'; a.click(); }
function drawChart(id, series){ const host=document.getElementById('c-'+id); if(!host||!series||!series.t.length) return;
  const W=host.clientWidth||600,H=220,L=44,R=12,T=10,B=26; const n=series.t.length;
  const ys=series.mid.concat(series.fv); let lo=Math.min(...ys), hi=Math.max(...ys); if(hi-lo<2){hi=lo+2;} const pad=(hi-lo)*0.05; lo-=pad; hi+=pad;
  const x=i=>L+(W-L-R)*i/Math.max(1,n-1), y=v=>T+(H-T-B)*(1-(v-lo)/(hi-lo));
  const path=arr=>arr.map((v,i)=>(i?'L':'M')+x(i).toFixed(1)+','+y(v).toFixed(1)).join(' ');
  const ticks=[lo+pad, (lo+hi)/2, hi-pad];
  let svg=`<svg width="${W}" height="${H}" role="img" aria-label="Kalshi mid and fair value over time">`;
  for(const tv of ticks){ svg+=`<line x1="${L}" x2="${W-R}" y1="${y(tv).toFixed(1)}" y2="${y(tv).toFixed(1)}" stroke="var(--line)"/><text x="${L-6}" y="${(y(tv)+4).toFixed(1)}" text-anchor="end">${tv.toFixed(1)}c</text>`; }
  svg+=`<text x="${L}" y="${H-8}">${series.t0}</text><text x="${W-R}" y="${H-8}" text-anchor="end">${series.t1}</text>`;
  svg+=`<path d="${path(series.mid)}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round"/>`;
  svg+=`<path d="${path(series.fv)}" fill="none" stroke="var(--series-2)" stroke-width="2" stroke-linejoin="round"/>`;
  svg+=`<line id="x-${id}" x1="0" x2="0" y1="${T}" y2="${H-B}" stroke="var(--muted)" stroke-dasharray="3 3" style="display:none"/></svg>`;
  host.innerHTML=svg; const tip=host.parentElement.querySelector('.tip'); const xl=host.querySelector('line[id^="x-"]');
  host.addEventListener('mousemove',ev=>{ const rect=host.getBoundingClientRect(); const px=ev.clientX-rect.left; let i=Math.round((px-L)/((W-L-R)/Math.max(1,n-1))); i=Math.max(0,Math.min(n-1,i));
    xl.setAttribute('x1',x(i)); xl.setAttribute('x2',x(i)); xl.style.display='block'; tip.style.display='block'; tip.style.left=(px+12)+'px'; tip.style.top=(ev.clientY-rect.top+8)+'px';
    tip.textContent=`${series.labels[i]}  mid ${series.mid[i].toFixed(1)}c  fv ${series.fv[i].toFixed(1)}c  gap ${(series.mid[i]-series.fv[i]).toFixed(1)}c`; });
  host.addEventListener('mouseleave',()=>{ xl.style.display='none'; tip.style.display='none'; }); }
function toggleTable(id){ const t=document.getElementById('t-'+id); t.style.display = t.style.display==='none'?'block':'none'; }
window.addEventListener('load',()=>{ for(const p of PAIRS){ if(SERIES[p.pair_id]) drawChart(p.pair_id, SERIES[p.pair_id]); } refresh(); });
"""


def _e(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def _card(
    p: dict[str, Any], flagged: bool, kat: dict[str, Any] | None, series: dict[str, Any] | None
) -> str:
    pid = p["pair_id"]
    outcomes = p.get("poly_outcomes", "")
    overlay = ""
    if flagged:
        stats = (
            f"KAT-3: corr {kat.get('corr')}, median |gap| {kat.get('median_abs_gap_cents')} cents over {kat.get('n_minutes')} minutes"
            if kat
            else "KAT-3 flagged"
        )
        overlay = f"""<div class="k">Kalshi mid vs FV (flagged for side-inversion review)</div><div class="count">{_e(stats)}</div>
<div class="legend"><span><i class="sw" style="background:var(--series-1)"></i>Kalshi mid</span><span><i class="sw" style="background:var(--series-2)"></i>FV (Polymarket prints)</span></div>
<div style="position:relative"><div id="c-{_e(pid)}"></div><div class="tip"></div></div>
<button type="button" onclick="toggleTable('{_e(pid)}')">Table view</button><div id="t-{_e(pid)}" style="display:none">"""
        if series:
            rows = "".join(
                f"<tr><td>{_e(lbl)}</td><td>{m:.1f}</td><td>{f:.1f}</td></tr>"
                for lbl, m, f in zip(
                    series["labels"][:: max(1, len(series["labels"]) // 50)],
                    series["mid"][:: max(1, len(series["mid"]) // 50)],
                    series["fv"][:: max(1, len(series["fv"]) // 50)],
                    strict=False,
                )
            )
            overlay += f"<table class='tv'><tr><th>time</th><th>mid (c)</th><th>fv (c)</th></tr>{rows}</table>"
        else:
            overlay += "<div class='count'>no overlapping minute data</div>"
        overlay += "</div>"
    return f"""<div class="card{" flagged" if flagged else ""}" id="p-{_e(pid)}">
<div class="grid"><div><div class="k">Polymarket</div><h3>{_e(p.get("poly_question"))}</h3>
<div class="count">market {_e(p.get("poly_market_id"))} · ends {_e(p.get("poly_end_date"))}</div>
<div class="k">outcome labels</div><div>{_e(outcomes)} · reference outcome <b>{_e(p.get("reference_outcome"))}</b> (token {_e(p.get("reference_token_id"))})</div>
<div class="k">description / rules</div><pre>{_e(p.get("poly_description"))}</pre></div>
<div><div class="k">Kalshi</div><h3>{_e(p.get("kalshi_title"))}</h3>
<div class="count">{_e(p.get("kalshi_ticker"))} · {_e(p.get("kalshi_category"))} · scheduled end {_e(p.get("kalshi_scheduled_end"))}</div>
<div class="k">yes_sub_title</div><div><b>{_e(p.get("kalshi_yes_sub_title"))}</b></div>
<div class="k">rules_primary</div><pre>{_e(p.get("kalshi_rules_primary"))}</pre><div class="k">rules_secondary</div><pre>{_e(p.get("kalshi_rules_secondary"))}</pre></div></div>
<div class="k">side alignment</div><div><span class="badge">{_e(p.get("side_status"))}</span> {_e(p.get("side_method"))}: {_e(p.get("side_reason"))}</div>
<div class="k">deterministic checks</div><div class="count">{_e(p.get("checks_summary"))}</div>
<div class="k">LLM</div><div class="count">{_e(p.get("llm_final"))} · {_e(p.get("llm_note"))}</div>
{overlay}
<div class="dec"><span class="badge" id="st-{_e(pid)}">undecided</span>
<label><input type="radio" name="d-{_e(pid)}" value="approve" onchange="setDec('{_e(pid)}','approve')"> approve (identical resolution, side confirmed)</label>
<label><input type="radio" name="d-{_e(pid)}" value="reject" onchange="setDec('{_e(pid)}','reject')"> reject</label>
<select id="c-reason-{_e(pid)}" onchange="setReasonCode('{_e(pid)}',this.value)"><option value="">rules-based reason (required for reject)</option><option value="underlying">different underlying / entity</option><option value="threshold">different threshold or unit</option><option value="date_window">different date window</option><option value="source">different resolution source</option><option value="tie_handling">different tie / cancellation handling</option><option value="side_unconfirmed">side alignment unconfirmed</option></select>
<input type="text" id="r-{_e(pid)}" placeholder="optional: quote the decisive rules text" oninput="setReason('{_e(pid)}',this.value)"></div></div>"""


def build(
    pairs: list[dict[str, Any]], flagged: dict[str, dict[str, Any]], series: dict[str, Any]
) -> str:
    cards = "\n".join(
        _card(p, p["pair_id"] in flagged, flagged.get(p["pair_id"]), series.get(p["pair_id"]))
        for p in pairs
    )
    ids = json.dumps([p["pair_id"] for p in pairs])
    slim = json.dumps(
        [
            {
                k: p.get(k, "")
                for k in (
                    "pair_id",
                    "kalshi_ticker",
                    "poly_market_id",
                    "condition_id",
                    "reference_token_id",
                    "reference_outcome",
                )
            }
            for p in pairs
        ]
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Pair Review</title><style>{CSS}</style></head>
<body><header><b>ref_mm pair review</b> · <span id="counts" class="count"></span> · <button type="button" onclick="exportCsv()">Export approved_pairs.csv</button>
<div class="rule">A pair may be rejected <b>only for a documented rules-based reason</b> (different underlying, threshold, date window, resolution source, or tie/cancellation handling, or an unconfirmed side alignment). Never reject because of its price path or because the venues resolved differently: divergent resolution is a real cost and stays in the sample. Settlement results are deliberately not shown on this page (the universe must be decided without knowledge of outcomes). Save the exported file as <code>data/approved_pairs.csv</code>.</div></header>
{cards}
<script>const PAIR_IDS={ids}; const PAIRS={slim}; const SERIES={json.dumps(series)};{JS}</script></body></html>"""


def load_csv(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def overlay_series(pair: dict[str, Any], max_points: int = 600) -> dict[str, Any] | None:
    from datetime import datetime

    from strategies.ref_mm.research.kat.kat3 import pair_series

    t, mid, fv = pair_series(
        pair["kalshi_ticker"], pair["condition_id"], pair["reference_token_id"]
    )
    if t.size == 0:
        return None
    step = max(1, t.size // max_points)
    t, mid, fv = t[::step], mid[::step], fv[::step]
    labels = [datetime.fromtimestamp(int(x) / 1e6, tz=UTC).strftime("%m-%d %H:%M") for x in t]
    return {
        "t": [int(x) for x in t],
        "mid": [float(m) / 200 for m in mid],
        "fv": [float(f) / 200 for f in fv],
        "labels": labels,
        "t0": labels[0],
        "t1": labels[-1],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/review/index.html")
    args = ap.parse_args()
    root = repo_root()
    sample = load_csv(root / "data" / "review_sample.csv")
    all_pairs = {p["pair_id"]: p for p in load_csv(root / "data" / "candidate_pairs.csv")}
    kat3 = {
        r["pair_id"]: r
        for r in load_csv(root / "data" / "kat3.csv")
        if r.get("flagged", "").lower() == "true"
    }
    pairs = list(sample)
    have = {p["pair_id"] for p in pairs}
    for pid in kat3:
        if pid not in have and pid in all_pairs:
            pairs.append(all_pairs[pid])
    series: dict[str, Any] = {}
    for pid in kat3:
        p = all_pairs.get(pid)
        if p:
            try:
                s = overlay_series(p)
            except Exception:  # noqa: BLE001 - overlay is optional
                s = None
            if s:
                series[pid] = s
    out = root / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(pairs, kat3, series), encoding="utf-8")
    print(
        json.dumps(
            {
                "pairs": len(pairs),
                "flagged": len(kat3),
                "with_overlay": len(series),
                "out": str(out),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
