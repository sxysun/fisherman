"""Settings + diagnostics dashboard, served by the harness daemon on :7893.

Layout:
  GET  /dashboard                   → HTML, tabs (Activity / Eval / Settings / Diagnostics)
  GET  /dashboard/data?window=24h   → JSON: aggregated state (distributions, counts)
  GET  /dashboard/config            → JSON: current ~/.harness/config.toml
  POST /dashboard/config            → save edited config (writes ~/.harness/config.toml)
  POST /dashboard/policy            → save policy state (writes ~/.harness/policy.json)

Visual aesthetic matches the labeling UI (dark, monospace meta, amber accent).
"""

from __future__ import annotations

import os
import time
from collections import Counter
from typing import Any

import tomllib
from aiohttp import web

from . import config as config_mod
from . import sql_store
from .store import HARNESS_DIR, iter_jsonl


CONFIG_PATH = HARNESS_DIR / "config.toml"


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>harness · dashboard</title>
<style>
  :root {
    color-scheme: dark;
    --bg:        #0a0a0c;
    --panel:     #131318;
    --panel-2:   #1a1a20;
    --border:    #25252c;
    --border-2:  #2d2d35;
    --text:      #ececef;
    --text-2:    #a0a0a8;
    --text-3:    #6c6c74;
    --accent:    #d0c08f;
    --accent-2:  #e8d8a8;
    --green:     #7eb37e;
    --red:       #c4848c;
    --blue:      #7d9ec4;
    --amber:     #c4a87d;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--bg); color: var(--text);
    font: 13px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
  }
  .app { max-width: 1180px; margin: 0 auto; padding: 24px; }

  /* Header */
  header {
    display: flex; align-items: baseline; justify-content: space-between;
    padding-bottom: 16px; border-bottom: 1px solid var(--border); margin-bottom: 20px;
  }
  header h1 {
    margin: 0; font: 600 14px/1 -apple-system;
    color: var(--text-2);
  }
  header .meta { color: var(--text-3); font: 11px ui-monospace; }
  header .status-dot {
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: var(--green); margin-right: 8px;
    box-shadow: 0 0 6px rgba(126,179,126,0.5);
  }

  /* Tabs */
  .tabs { display: flex; gap: 4px; margin-bottom: 20px; border-bottom: 1px solid var(--border); }
  .tab {
    padding: 10px 16px; cursor: pointer;
    color: var(--text-3); border-bottom: 2px solid transparent;
    font: 600 11.5px -apple-system; text-transform: uppercase; letter-spacing: 0.06em;
    transition: color .12s, border-color .12s;
  }
  .tab:hover { color: var(--text-2); }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }

  /* Panels */
  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px;
    margin-bottom: 16px;
  }
  .panel h2 {
    margin: 0 0 12px 0;
    font: 600 10px/1 -apple-system;
    text-transform: uppercase; letter-spacing: 0.1em;
    color: var(--text-3);
  }

  /* Stat tiles */
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
  .stat {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
  }
  .stat .label {
    color: var(--text-3); font: 600 9.5px -apple-system;
    text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 6px;
  }
  .stat .value {
    font: 600 24px ui-monospace; color: var(--text);
    line-height: 1;
  }
  .stat .sub { color: var(--text-3); font: 11px ui-monospace; margin-top: 4px; }

  /* Distribution bars */
  .bars { display: flex; flex-direction: column; gap: 6px; }
  .bar-row { display: flex; align-items: center; gap: 12px; }
  .bar-row .key { width: 200px; color: var(--text-2); font: 12px ui-monospace; }
  .bar-row .val { width: 60px; text-align: right; color: var(--text); font: 12px ui-monospace; }
  .bar-row .track {
    flex: 1; height: 6px; background: var(--panel-2);
    border-radius: 3px; overflow: hidden;
  }
  .bar-row .fill { height: 100%; background: var(--accent); border-radius: 3px; }
  .bar-row.action_no_ping .fill { background: var(--blue); }
  .bar-row.action_notch_ping .fill { background: var(--amber); }

  /* Settings form */
  .form-row {
    display: grid;
    grid-template-columns: 200px 1fr auto;
    align-items: center;
    gap: 14px;
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
  }
  .form-row:last-child { border-bottom: none; }
  .form-row .label { color: var(--text-2); font: 12px -apple-system; }
  .form-row .hint { color: var(--text-3); font: 10.5px ui-monospace; }
  input[type="text"], input[type="number"] {
    background: var(--bg); border: 1px solid var(--border-2); color: var(--text);
    border-radius: 6px; padding: 6px 10px; width: 100%;
    font: 12px ui-monospace;
  }
  input[type="text"]:focus, input[type="number"]:focus {
    outline: none; border-color: var(--accent);
  }
  input[type="checkbox"] { transform: scale(1.2); accent-color: var(--accent); }
  .save-row {
    display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px;
    padding-top: 16px; border-top: 1px solid var(--border);
  }
  button.save {
    background: var(--accent); color: #1a1408; border: none;
    padding: 8px 16px; border-radius: 6px; cursor: pointer;
    font: 600 12px -apple-system;
  }
  button.save:hover { background: var(--accent-2); }
  button.revert {
    background: transparent; color: var(--text-2);
    border: 1px solid var(--border-2); padding: 8px 16px; border-radius: 6px;
    cursor: pointer; font: 12px -apple-system;
  }

  /* Intent grid */
  .intents-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
  .intent-chip {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 12px;
    background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 8px;
    font: 12px ui-monospace; color: var(--text-2);
  }
  .intent-chip input { margin: 0; }
  .intent-chip.muted { opacity: 0.55; }

  /* Diagnostics list */
  .log {
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 6px; padding: 10px; max-height: 460px; overflow-y: auto;
    font: 11px/1.5 ui-monospace; color: var(--text-2);
  }
  .log .row { padding: 4px 0; border-bottom: 1px dashed rgba(255,255,255,0.04); }
  .log .ts { color: var(--text-3); }
  .log .action.no_ping { color: var(--blue); }
  .log .action.notch_ping { color: var(--amber); }
  .log .reasons { color: var(--text-3); }

  /* Eval report */
  .table {
    width: 100%;
    border-collapse: collapse;
    font: 11.5px ui-monospace;
  }
  .table th {
    text-align: left;
    color: var(--text-3);
    font-weight: 600;
    padding: 7px 8px;
    border-bottom: 1px solid var(--border);
  }
  .table td {
    padding: 8px;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    color: var(--text-2);
    vertical-align: top;
  }
  .pill {
    display: inline-block;
    padding: 2px 7px;
    border-radius: 999px;
    border: 1px solid var(--border-2);
    color: var(--text-2);
    font: 10.5px ui-monospace;
  }
  .pill.high { color: var(--red); border-color: rgba(196,132,140,0.45); }
  .pill.medium { color: var(--amber); border-color: rgba(196,168,125,0.45); }
  .pill.low { color: var(--blue); border-color: rgba(125,158,196,0.45); }
  .pill.info { color: var(--green); border-color: rgba(126,179,126,0.45); }
  .gap-list, .example-list { display: flex; flex-direction: column; gap: 8px; }
  .gap-row, .example-row {
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
  }
  .example-row .topline {
    display: flex; justify-content: space-between; gap: 12px;
    color: var(--text-2); font: 11.5px ui-monospace;
  }
  .example-row .msg {
    margin-top: 6px; color: var(--text); font-size: 12px;
  }
  .example-row .meta {
    margin-top: 4px; color: var(--text-3); font: 10.5px ui-monospace;
  }

  /* Tab content */
  .tab-content { display: none; }
  .tab-content.active { display: block; }
</style>
</head><body>
<div class="app" id="app">
  <header>
    <h1><span class="status-dot"></span>HARNESS · DASHBOARD</h1>
    <div class="meta" id="header-meta">loading…</div>
  </header>

  <div class="tabs">
    <div class="tab active" data-tab="activity">Activity</div>
    <div class="tab" data-tab="eval">Eval</div>
    <div class="tab" data-tab="settings">Settings</div>
    <div class="tab" data-tab="diagnostics">Diagnostics</div>
  </div>

  <div class="tab-content active" id="tab-activity">
    <div class="stats" id="stats"></div>
    <div class="panel">
      <h2>Decision actions (last 24h)</h2>
      <div class="bars" id="action-bars"></div>
    </div>
    <div class="panel">
      <h2>Scene tags</h2>
      <div class="bars" id="scene-bars"></div>
    </div>
    <div class="panel">
      <h2>Frontmost apps</h2>
      <div class="bars" id="app-bars"></div>
    </div>
    <div class="panel">
      <h2>Reason codes (why decisions went the way they did)</h2>
      <div class="bars" id="reason-bars"></div>
    </div>
    <div class="panel">
      <h2>Recent outcomes (intent signal distribution)</h2>
      <div class="bars" id="intent-bars"></div>
    </div>
  </div>

  <div class="tab-content" id="tab-eval">
    <div class="stats" id="eval-stats"></div>
    <div class="panel">
      <h2>Next-step prediction loop</h2>
      <div class="stats" id="next-step-stats"></div>
      <div class="bars" id="next-step-residuals" style="margin-top:12px;"></div>
    </div>
    <div class="panel">
      <h2>OpenAdapt-style gaps</h2>
      <div class="gap-list" id="eval-gaps"></div>
    </div>
    <div class="panel">
      <h2>Failure taxonomy</h2>
      <div class="bars" id="eval-taxonomy"></div>
    </div>
    <div class="panel">
      <h2>Policy variants</h2>
      <table class="table" id="eval-variants"></table>
    </div>
    <div class="panel">
      <h2>Recent non-green examples</h2>
      <div class="example-list" id="eval-examples"></div>
    </div>
  </div>

  <div class="tab-content" id="tab-settings">
    <div class="panel">
      <h2>Daemon</h2>
      <div class="form-row">
        <span class="label">Poll interval (sec)</span>
        <input type="number" id="cfg-poll" min="1" max="60" step="1">
        <span class="hint">5 = matches Fisherman capture</span>
      </div>
      <div class="form-row">
        <span class="label">Fisherman URL</span>
        <input type="text" id="cfg-fisherman-url">
        <span class="hint">localhost:7892</span>
      </div>
    </div>
    <div class="panel">
      <h2>Gate (rule_v0 thresholds)</h2>
      <div class="form-row">
        <span class="label">Active policy</span>
        <input type="text" id="cfg-policy">
        <span class="hint">module under policies/</span>
      </div>
      <div class="form-row">
        <span class="label">Cooldown (min)</span>
        <input type="number" id="cfg-cooldown" min="0" step="0.5">
        <span class="hint">min minutes between pings</span>
      </div>
      <div class="form-row">
        <span class="label">Quiet hours</span>
        <input type="text" id="cfg-quiet" placeholder="22-8">
        <span class="hint">start-end (24h, wraps midnight)</span>
      </div>
    </div>
    <div class="panel">
      <h2>Realizer (LLM endpoint)</h2>
      <div class="form-row">
        <span class="label">Base URL</span>
        <input type="text" id="cfg-base-url">
        <span class="hint">OpenAI-compatible</span>
      </div>
      <div class="form-row">
        <span class="label">Model</span>
        <input type="text" id="cfg-model">
        <span class="hint">e.g. hermes-agent</span>
      </div>
      <div class="form-row">
        <span class="label">Vision (multimodal)</span>
        <input type="checkbox" id="cfg-vision">
        <span class="hint">attach JPEG to each ping call</span>
      </div>
      <div class="form-row">
        <span class="label">Max tokens out</span>
        <input type="number" id="cfg-max-tokens" min="20" max="500">
        <span class="hint">tight ceiling = brevity</span>
      </div>
      <div class="form-row">
        <span class="label">Timeout (sec)</span>
        <input type="number" id="cfg-timeout" min="5" max="120">
        <span class="hint">45 recommended w/ vision</span>
      </div>
    </div>
    <div class="panel">
      <h2>Intents enabled</h2>
      <div class="intents-grid" id="intents-grid"></div>
    </div>
    <div class="panel">
      <h2>Reward weights (used by score.py)</h2>
      <div class="form-row">
        <span class="label">welcomed (clicked)</span>
        <input type="number" id="cfg-r-welcomed" step="0.1">
        <span class="hint">+ for good pings</span>
      </div>
      <div class="form-row">
        <span class="label">annoying (dismissed)</span>
        <input type="number" id="cfg-r-annoying" step="0.1">
        <span class="hint">− for bad pings</span>
      </div>
      <div class="form-row">
        <span class="label">privacy violation</span>
        <input type="number" id="cfg-r-privacy" step="0.1">
        <span class="hint">large negative</span>
      </div>
      <div class="form-row">
        <span class="label">duplicate</span>
        <input type="number" id="cfg-r-duplicate" step="0.1">
        <span class="hint">− for repetition</span>
      </div>
    </div>
    <div class="save-row">
      <button class="revert" onclick="loadConfig()">Revert</button>
      <button class="save" onclick="saveConfig()">Save (restart daemon to apply)</button>
    </div>
  </div>

  <div class="tab-content" id="tab-diagnostics">
    <div class="panel">
      <h2>Recent decisions (last 30)</h2>
      <div class="log" id="diag-decisions"></div>
    </div>
    <div class="panel">
      <h2>Recent outcomes (last 15)</h2>
      <div class="log" id="diag-outcomes"></div>
    </div>
    <div class="panel">
      <h2>Recent realizer messages (last 10)</h2>
      <div class="log" id="diag-realizations"></div>
    </div>
    <div class="panel">
      <h2>Recent model calls (last 30)</h2>
      <div class="log" id="diag-model-calls"></div>
    </div>
  </div>
</div>

<script>
let cfg = null;
let policyState = null;
let evalReport = null;
let evalLoadedAt = 0;

// Tab switching
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('tab-' + t.dataset.tab).classList.add('active');
    if (t.dataset.tab === 'eval') loadEval();
  });
});

async function refresh() {
  const [data, c, p] = await Promise.all([
    fetch('/dashboard/data').then(r => r.json()),
    fetch('/dashboard/config').then(r => r.json()),
    fetch('/status').then(r => r.json()),
  ]);
  renderActivity(data);
  renderDiagnostics(data);
  renderHeader(data, p);
  cfg = c;
  policyState = p;
  populateSettings(c, p);
}

async function loadEval(force=false) {
  const now = Date.now();
  if (!force && evalReport && now - evalLoadedAt < 30000) {
    renderEval(evalReport);
    return;
  }
  document.getElementById('eval-stats').innerHTML = `<div class="stat"><div class="label">Eval</div><div class="value">…</div><div class="sub">loading report</div></div>`;
  evalReport = await fetch('/eval/report?window=7d&max_examples=20').then(r => r.json());
  evalLoadedAt = now;
  renderEval(evalReport);
}

function renderHeader(data, p) {
  const muted = (p.muted_intents || []).length;
  const snoozed = p.snoozed_until ? `snoozed until ${p.snoozed_until}` : 'active';
  document.getElementById('header-meta').textContent =
    `${snoozed} · policy=${p.active_policy || '?'} · ${data.n_candidates} candidates / ${data.n_decisions} decisions / ${data.n_outcomes} outcomes · muted=${muted}`;
}

function renderActivity(d) {
  const stats = [
    {label: 'Pings (24h)',          val: d.n_pings,    sub: `of ${d.n_decisions} decisions`},
    {label: 'Ping rate',             val: pct(d.n_pings / Math.max(d.n_decisions, 1))},
    {label: 'Outcomes captured',     val: d.n_outcomes, sub: `${d.n_clicked} clicked · ${d.n_dismissed} dismissed`},
    {label: 'Considered + timed_out',val: d.n_considered_no_click, sub: 'intent signal but no commit'},
  ];
  document.getElementById('stats').innerHTML = stats.map(s => `
    <div class="stat">
      <div class="label">${s.label}</div>
      <div class="value">${s.val ?? '—'}</div>
      ${s.sub ? `<div class="sub">${s.sub}</div>` : ''}
    </div>`).join('');

  document.getElementById('action-bars').innerHTML = barsHTML(d.dist_actions, 'action_');
  document.getElementById('scene-bars').innerHTML = barsHTML(d.dist_scenes);
  document.getElementById('app-bars').innerHTML = barsHTML(d.dist_apps);
  document.getElementById('reason-bars').innerHTML = barsHTML(d.dist_reasons);
  document.getElementById('intent-bars').innerHTML = barsHTML(d.dist_intent_signals);
}

function renderEval(r) {
  const data = r.data || {};
  const nextStep = (r.next_step || {}).predictions || {};
  const best = (((r.variants || {}).calibration || {}).best_variant || {});
  const stats = [
    {label: 'Decisions', val: data.n_decisions ?? 0, sub: `${data.n_pings ?? 0} pings · ${data.n_claimed_pings ?? 0} claimed`},
    {label: 'Claimed capture', val: pctMaybe(data.outcome_capture_rate_for_claimed_pings), sub: `${pctMaybe(data.outcome_capture_rate_for_pings)} all pings`},
    {label: 'Explicit labels', val: data.n_explicit_labels ?? 0, sub: pctMaybe(data.explicit_label_coverage)},
    {label: 'Best variant', val: best.variant || 'n/a', sub: `score ${numMaybe(best.score)}`},
  ];
  document.getElementById('eval-stats').innerHTML = stats.map(s => `
    <div class="stat">
      <div class="label">${escapeHTML(s.label)}</div>
      <div class="value" style="font-size:${String(s.val).length > 8 ? '15px' : '24px'}">${escapeHTML(s.val)}</div>
      ${s.sub ? `<div class="sub">${escapeHTML(s.sub)}</div>` : ''}
    </div>`).join('');

  const nsStats = [
    {label: 'Predictions', val: nextStep.n ?? 0, sub: `${nextStep.scored ?? 0} scored · ${nextStep.pending ?? 0} pending`},
    {label: 'Top-1 accuracy', val: pctMaybe(nextStep.accuracy_top1), sub: `top-3 ${pctMaybe(nextStep.accuracy_top3)}`},
    {label: 'Unknown rate', val: pctMaybe(nextStep.unknown_rate), sub: 'no future observation'},
    {label: 'Avg score', val: numMaybe(nextStep.avg_score), sub: `${nextStep.matched ?? 0} matched · ${nextStep.missed ?? 0} missed`},
  ];
  document.getElementById('next-step-stats').innerHTML = nsStats.map(s => `
    <div class="stat">
      <div class="label">${escapeHTML(s.label)}</div>
      <div class="value" style="font-size:${String(s.val).length > 8 ? '15px' : '24px'}">${escapeHTML(s.val)}</div>
      ${s.sub ? `<div class="sub">${escapeHTML(s.sub)}</div>` : ''}
    </div>`).join('');
  document.getElementById('next-step-residuals').innerHTML = barsHTML(nextStep.residual_types || {});

  document.getElementById('eval-gaps').innerHTML = (r.openadapt_style_gaps || []).map(g => `
    <div class="gap-row">
      <span class="pill ${g.status === 'pass' ? 'info' : g.status === 'missing' ? 'high' : 'medium'}">${escapeHTML(g.status)}</span>
      <span style="margin-left:8px;color:var(--text);font:12px ui-monospace;">${escapeHTML(g.name)}</span>
      <div style="margin-top:4px;color:var(--text-3);font-size:11.5px;">${escapeHTML(g.detail)} value=${escapeHTML(g.value ?? 'n/a')}</div>
    </div>`).join('') || '<div style="color:#7c7c86">no gap data</div>';

  const taxDist = {};
  ((r.taxonomy || {}).by_type || []).forEach(row => taxDist[row.type] = row.n);
  document.getElementById('eval-taxonomy').innerHTML = barsHTML(taxDist);

  const variants = ((((r.variants || {}).calibration || {}).variants) || []);
  document.getElementById('eval-variants').innerHTML = `
    <thead><tr><th>variant</th><th>score</th><th>implicit utility</th><th>explicit</th><th>overrides</th></tr></thead>
    <tbody>
      ${variants.map(v => `
        <tr>
          <td>${escapeHTML(v.variant || 'n/a')}</td>
          <td>${numMaybe(v.score)}</td>
          <td>${numMaybe(v.implicit_avg_utility)} · n=${numMaybe(v.implicit_weighted_n)}</td>
          <td>agree=${pctMaybe(v.explicit_agreement_rate)} false=${pctMaybe(v.explicit_false_interruption_rate)} missed=${pctMaybe(v.explicit_missed_help_rate)}</td>
          <td>${escapeHTML(JSON.stringify(v.overrides || {}))}</td>
        </tr>`).join('') || '<tr><td colspan="5">no variant comparison yet</td></tr>'}
    </tbody>`;

  document.getElementById('eval-examples').innerHTML = (r.examples || []).map(ex => {
    const cls = ex.classification || {};
    const dec = ex.decision || {};
    const out = ex.outcome || {};
    const ctx = ex.context || {};
    return `<div class="example-row">
      <div class="topline">
        <span><span class="pill ${cls.severity || 'low'}">${escapeHTML(cls.type || '?')}</span> ${escapeHTML(dec.action || '?')} · ${escapeHTML(ctx.app || '?')} · ${escapeHTML(ctx.scene || '?')}</span>
        <span>${escapeHTML((ex.ts || '').slice(0,19))}</span>
      </div>
      ${ctx.message ? `<div class="msg">"${escapeHTML(ctx.message)}"</div>` : ''}
      <div class="meta">outcome=${escapeHTML(out.user_action || 'none')} signal=${escapeHTML(out.intent_signal || 'none')} label=${escapeHTML((ex.label || {}).label || 'none')} reasons=[${escapeHTML((dec.reason_codes || []).join(', '))}]</div>
    </div>`;
  }).join('') || '<div style="color:#7c7c86">no non-green examples in this window</div>';
}

function barsHTML(dist, prefix='') {
  const entries = Object.entries(dist || {}).sort((a,b) => b[1] - a[1]).slice(0, 12);
  const max = entries.length ? entries[0][1] : 1;
  return entries.map(([k, v]) => `
    <div class="bar-row ${prefix}${(k || 'unknown').replace(/[^a-z0-9_]/gi,'_')}">
      <div class="key">${escapeHTML(k || '(empty)')}</div>
      <div class="track"><div class="fill" style="width:${(v/max*100).toFixed(1)}%"></div></div>
      <div class="val">${v}</div>
    </div>`).join('') || '<div style="color:#7c7c86;font-size:11.5px;">(no data)</div>';
}

function renderDiagnostics(d) {
  document.getElementById('diag-decisions').innerHTML = (d.recent_decisions || []).map(r =>
    `<div class="row">
      <span class="ts">${(r.ts || '').slice(0,19)}</span>
      <span class="action ${r.action}">${(r.action || '?').padEnd(11)}</span>
      intent=${r.intent || '—'}
      <span class="reasons">reasons=[${(r.reason_codes||[]).join(', ')}]</span>
    </div>`
  ).join('') || '<div style="color:#7c7c86">no decisions yet</div>';

  document.getElementById('diag-outcomes').innerHTML = (d.recent_outcomes || []).map(r =>
    `<div class="row">
      <span class="ts">${(r.ts || '').slice(0,19)}</span>
      ${r.user_action} ${r.decision_id}
      <span class="reasons">signal=${(r.interaction_summary||{}).intent_signal || '—'}
        considered=${JSON.stringify((r.interaction_summary||{}).considered_targets || [])}</span>
    </div>`
  ).join('') || '<div style="color:#7c7c86">no outcomes yet</div>';

  document.getElementById('diag-realizations').innerHTML = (d.recent_realizations || []).map(r =>
    `<div class="row">
      <span class="ts">${(r.ts || '').slice(0,19)}</span>
      ${r.intent || '?'} <span class="reasons">[${r.latency_ms}ms, vision=${r.vision_used ? '✓' : '✗'}]</span>
      <div style="margin-top:3px;color:var(--text);">"${escapeHTML((r.message || '').slice(0,180))}"</div>
    </div>`
  ).join('') || '<div style="color:#7c7c86">no realizer calls yet</div>';

  const modelCallsEl = document.getElementById('diag-model-calls');
  if (modelCallsEl) {
    modelCallsEl.innerHTML = (d.recent_model_calls || []).map(r =>
      `<div class="row">
        <span class="ts">${(r.ts || '').slice(0,19)}</span>
        ${r.purpose || '?'} ${r.model || '?'}
        <span class="reasons">status=${r.status || '?'} http=${r.http_status || '—'}
          ${r.latency_ms || 0}ms vision=${r.vision_used ? '✓' : '✗'} image=${r.image_bytes || 0}B</span>
      </div>`
    ).join('') || '<div style="color:#7c7c86">no model calls yet</div>';
  }
}

function populateSettings(c, p) {
  const g = c.gate || {};
  const r = c.realizer || {};
  const rw = (c.reward && c.reward.weights) || {};
  const d = c.daemon || {};
  const intents = (c.intents && c.intents.enabled) || [];

  document.getElementById('cfg-poll').value         = d.poll_interval_sec ?? '';
  document.getElementById('cfg-fisherman-url').value= d.fisherman_url ?? '';
  document.getElementById('cfg-policy').value       = g.active_policy ?? '';
  document.getElementById('cfg-cooldown').value     = g.cooldown_min ?? '';
  document.getElementById('cfg-quiet').value        = `${g.quiet_hours_start ?? 22}-${g.quiet_hours_end ?? 8}`;
  document.getElementById('cfg-base-url').value     = r.base_url ?? '';
  document.getElementById('cfg-model').value        = r.model ?? '';
  document.getElementById('cfg-vision').checked     = !!r.include_vision;
  document.getElementById('cfg-max-tokens').value   = r.max_tokens ?? 80;
  document.getElementById('cfg-timeout').value      = r.timeout_sec ?? 45;
  document.getElementById('cfg-r-welcomed').value   = rw.welcomed ?? 3;
  document.getElementById('cfg-r-annoying').value   = rw.annoying ?? -5;
  document.getElementById('cfg-r-privacy').value    = rw.privacy ?? -8;
  document.getElementById('cfg-r-duplicate').value  = rw.duplicate ?? -1;

  const muted = new Set((p && p.muted_intents) || []);
  const allIntents = ["focus_nudge","offer_research","surface_open_thread","summarize_session"];
  document.getElementById('intents-grid').innerHTML = allIntents.map(name => {
    const enabled = intents.includes(name);
    const isMuted = muted.has(name);
    return `<label class="intent-chip ${isMuted ? 'muted' : ''}">
      <input type="checkbox" data-intent="${name}" ${enabled ? 'checked' : ''}>
      ${name.replace(/_/g, ' ')} ${isMuted ? '· muted' : ''}
    </label>`;
  }).join('');
}

async function loadConfig() {
  cfg = await fetch('/dashboard/config').then(r => r.json());
  populateSettings(cfg, policyState);
}

async function saveConfig() {
  const next = JSON.parse(JSON.stringify(cfg));
  next.daemon ||= {};
  next.gate ||= {};
  next.realizer ||= {};
  next.reward ||= {weights: {}};
  next.reward.weights ||= {};
  next.intents ||= {};

  next.daemon.poll_interval_sec = parseInt(document.getElementById('cfg-poll').value || '5');
  next.daemon.fisherman_url = document.getElementById('cfg-fisherman-url').value;
  next.gate.active_policy = document.getElementById('cfg-policy').value;
  next.gate.cooldown_min = parseFloat(document.getElementById('cfg-cooldown').value || '5');
  const q = (document.getElementById('cfg-quiet').value || '22-8').split('-');
  next.gate.quiet_hours_start = parseInt(q[0]);
  next.gate.quiet_hours_end = parseInt(q[1]);
  next.realizer.base_url = document.getElementById('cfg-base-url').value;
  next.realizer.model = document.getElementById('cfg-model').value;
  next.realizer.include_vision = document.getElementById('cfg-vision').checked;
  next.realizer.max_tokens = parseInt(document.getElementById('cfg-max-tokens').value || '80');
  next.realizer.timeout_sec = parseInt(document.getElementById('cfg-timeout').value || '45');
  next.reward.weights.welcomed  = parseFloat(document.getElementById('cfg-r-welcomed').value);
  next.reward.weights.annoying  = parseFloat(document.getElementById('cfg-r-annoying').value);
  next.reward.weights.privacy   = parseFloat(document.getElementById('cfg-r-privacy').value);
  next.reward.weights.duplicate = parseFloat(document.getElementById('cfg-r-duplicate').value);
  next.intents.enabled = Array.from(document.querySelectorAll('#intents-grid input[type=checkbox]'))
    .filter(c => c.checked).map(c => c.dataset.intent);

  const resp = await fetch('/dashboard/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(next),
  });
  if (resp.ok) {
    cfg = next;
    flash('Saved. Restart daemon to apply.');
  } else {
    flash('Save failed.', 'red');
  }
}

function flash(text, color) {
  const el = document.createElement('div');
  el.textContent = text;
  el.style.cssText = `position:fixed;bottom:24px;right:24px;background:${color === 'red' ? '#3a1f1f' : '#1f3a1f'};
                      color:${color === 'red' ? '#c4848c' : '#7eb37e'};padding:10px 14px;border-radius:8px;
                      font:12px ui-monospace;box-shadow:0 4px 12px rgba(0,0,0,0.4);z-index:1000;`;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2400);
}

function pct(v) { return (v * 100).toFixed(1) + '%'; }
function pctMaybe(v) { return v === null || v === undefined ? 'n/a' : pct(Number(v)); }
function numMaybe(v) { return v === null || v === undefined ? 'n/a' : Number(v).toFixed(2); }
function escapeHTML(s) {
  return String(s).replace(/[<>&"]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
}

refresh();
setInterval(refresh, 5000);
</script>
</body></html>
"""


def _aggregate(window_sec: int = 86400) -> dict:
    """Read typed event payloads and compute distributions over the last window."""
    now = time.time()
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - window_sec))

    candidates = _read_payloads("candidates", "candidates.jsonl", since_iso=since_iso)
    decisions = _read_payloads("decisions", "decisions.jsonl", since_iso=since_iso)
    outcomes = _read_payloads("outcomes", "outcomes.jsonl", since_iso=since_iso)
    traces = list(reversed(_read_payloads("traces", "traces.jsonl", limit=50, newest_first=True)))

    dist_actions: Counter = Counter()
    dist_intents: Counter = Counter()
    dist_reasons: Counter = Counter()
    for d in decisions:
        dist_actions[d.get("action", "?")] += 1
        if d.get("intent"):
            dist_intents[d["intent"]] += 1
        for r in d.get("reason_codes", []):
            dist_reasons[r] += 1

    dist_scenes: Counter = Counter()
    dist_apps: Counter = Counter()
    for c in candidates:
        scene = (c.get("scene") or {}).get("label", "?")
        dist_scenes[scene] += 1
        app = (c.get("screen") or {}).get("frontmost_app") or "?"
        dist_apps[app] += 1

    dist_intent_signals: Counter = Counter()
    n_clicked = 0
    n_dismissed = 0
    n_considered_no_click = 0
    for o in outcomes:
        ua = o.get("user_action", "?")
        if ua == "clicked":
            n_clicked += 1
        elif ua == "dismissed":
            n_dismissed += 1
        sig = (o.get("interaction_summary") or {}).get("intent_signal")
        if sig:
            dist_intent_signals[sig] += 1
        if sig == "considered" and ua == "timed_out":
            n_considered_no_click += 1

    model_calls = _read_payloads("model_calls", "model_calls.jsonl", limit=30, newest_first=True)

    # Recent realizer calls from traces — only those that produced a message
    recent_realizations = []
    for t in reversed(traces):
        r = t.get("realization") or {}
        if not r.get("message"):
            continue
        # Surface provider reasoning / tool calls if present
        tool_calls = r.get("tool_calls") or []
        provider_reasoning = next(
            (tc.get("result_summary") for tc in tool_calls
             if tc.get("name", "").startswith("_provider_")),
            None,
        )
        recent_realizations.append({
            "ts": t.get("ts"),
            "intent": (t.get("action") or {}).get("intent"),
            "why_now": (t.get("action") or {}).get("why_now"),
            "message": r.get("message"),
            "latency_ms": r.get("latency_ms"),
            "vision_used": r.get("vision_used"),
            "privacy_flags": r.get("privacy_flags") or [],
            "privacy_provenance": r.get("privacy_provenance") or {},
            "provider_reasoning": provider_reasoning,
        })
        if len(recent_realizations) >= 10:
            break

    return {
        "n_candidates": len(candidates),
        "n_decisions": len(decisions),
        "n_outcomes": len(outcomes),
        "n_pings": dist_actions.get("notch_ping", 0),
        "n_clicked": n_clicked,
        "n_dismissed": n_dismissed,
        "n_considered_no_click": n_considered_no_click,
        "dist_actions": dict(dist_actions),
        "dist_scenes": dict(dist_scenes.most_common(20)),
        "dist_apps": dict(dist_apps.most_common(20)),
        "dist_reasons": dict(dist_reasons.most_common(20)),
        "dist_intent_signals": dict(dist_intent_signals),
        "recent_decisions": decisions[-30:][::-1],
        "recent_outcomes": outcomes[-15:][::-1],
        "recent_realizations": recent_realizations,
        "recent_model_calls": model_calls,
    }


def _read_payloads(
    table: str,
    filename: str,
    *,
    since_iso: str | None = None,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[dict]:
    try:
        db_exists = sql_store.db_path().exists()
        table_has_rows = db_exists and sql_store.count_rows(table) > 0
        if table_has_rows:
            return sql_store.payload_rows(
                table,
                since_iso=since_iso,
                limit=limit,
                newest_first=newest_first,
            )
    except Exception:
        pass

    rows = [r for r in iter_jsonl(filename) if since_iso is None or r.get("ts", "") >= since_iso]
    if limit is not None:
        rows = rows[-limit:]
    if newest_first:
        rows = list(reversed(rows))
    return rows


# ────────────────────────────────────────────────────────────────────────────
# Config R/W
# ────────────────────────────────────────────────────────────────────────────

def _load_config_dict() -> dict:
    if not CONFIG_PATH.exists():
        return tomllib.loads(config_mod.DEFAULT_CONFIG_TOML)
    return config_mod.load()


def _toml_quote(s: str) -> str:
    """Escape a string for use as a TOML value."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dump_toml(cfg: dict) -> str:
    """Minimal TOML dump for the harness config shape. Preserves nested tables."""
    lines: list[str] = []
    # Top-level tables in a stable order
    section_order = ["daemon", "gate", "experiment", "trainer", "scene", "scene_tagger", "memory",
                     "realizer", "critic", "privacy", "push", "reward", "intents", "debug"]
    written: set[str] = set()

    def _val(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, str):
            return _toml_quote(v)
        if isinstance(v, list):
            return "[" + ", ".join(_val(x) for x in v) + "]"
        return _toml_quote(str(v))

    def _emit_table(name: str, body: dict, prefix: list[str] | None = None) -> None:
        prefix = prefix or []
        full_name = ".".join(prefix + [name])
        # Separate scalars (incl. lists) from nested dicts
        scalars: list[tuple[str, Any]] = []
        nested: list[tuple[str, dict]] = []
        for k, v in body.items():
            if isinstance(v, dict):
                nested.append((k, v))
            else:
                scalars.append((k, v))
        lines.append(f"[{full_name}]")
        for k, v in scalars:
            lines.append(f"{k} = {_val(v)}")
        lines.append("")
        for k, sub in nested:
            _emit_table(k, sub, prefix=prefix + [name])

    for section in section_order:
        if section in cfg and isinstance(cfg[section], dict):
            _emit_table(section, cfg[section])
            written.add(section)
    for section, body in cfg.items():
        if section in written:
            continue
        if isinstance(body, dict):
            _emit_table(section, body)
    return "\n".join(lines).rstrip() + "\n"


# ────────────────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────────────────

async def get_dashboard_page(_: web.Request) -> web.Response:
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


async def get_dashboard_data(request: web.Request) -> web.Response:
    window = request.query.get("window", "24h")
    secs = {"1h": 3600, "24h": 86400, "7d": 604800}.get(window, 86400)
    return web.json_response(_aggregate(secs))


async def get_dashboard_config(_: web.Request) -> web.Response:
    return web.json_response(_load_config_dict())


async def post_dashboard_config(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected object"}, status=400)
    try:
        toml_text = _dump_toml(body)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp then move to avoid partial writes.
        tmp = CONFIG_PATH.with_suffix(".toml.tmp")
        tmp.write_text(toml_text)
        os.replace(tmp, CONFIG_PATH)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


def attach_routes(app: web.Application) -> None:
    app.router.add_get("/dashboard", get_dashboard_page)
    app.router.add_get("/dashboard/data", get_dashboard_data)
    app.router.add_get("/dashboard/config", get_dashboard_config)
    app.router.add_post("/dashboard/config", post_dashboard_config)
