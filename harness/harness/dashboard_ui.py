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

import asyncio
import os
import time
from collections import Counter
from typing import Any

import tomllib
from aiohttp import web

from . import config as config_mod
from . import sql_store
from .store import HARNESS_DIR, iter_jsonl, read_policy_state, write_policy_state


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
    <div class="tab" data-tab="diet">Diet</div>
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
      <h2>Recent workflow events</h2>
      <div class="example-list" id="workflow-events"></div>
    </div>
    <div class="panel">
      <h2>Recent policy context packets</h2>
      <div class="example-list" id="context-packets"></div>
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
    <div class="panel">
      <h2>Event-level review</h2>
      <div class="example-list">
        <div class="example-row">
          <div class="topline"><span>Workflow labels measure recall at the task-run level.</span><span><a href="/label/events" style="color:var(--accent);text-decoration:none;">open event review</a></span></div>
          <div class="meta">Use this for hard negatives and missed-help candidates; use the decision labeler for exact tick labels.</div>
        </div>
      </div>
    </div>
  </div>

  <div class="tab-content" id="tab-diet">
    <div class="stats" id="diet-stats"></div>
    <div class="panel">
      <h2>Workflow patterns</h2>
      <div class="bars" id="diet-patterns"></div>
    </div>
    <div class="panel">
      <h2>Top source domains</h2>
      <div class="bars" id="diet-domains"></div>
    </div>
    <div class="panel">
      <h2>Skill hypotheses</h2>
      <div class="example-list" id="diet-skills"></div>
    </div>
    <div class="panel">
      <h2>Recent research episodes</h2>
      <div class="example-list" id="diet-episodes"></div>
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
      <h2>Gate</h2>
      <div class="form-row">
        <span class="label">Active policy</span>
        <input type="text" id="cfg-policy">
        <span class="hint">rule_v0 or llm_icl_v0</span>
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
      <h2>LLM ICL policy learner</h2>
      <div class="form-row">
        <span class="label">Enabled</span>
        <input type="checkbox" id="cfg-learner-enabled">
        <span class="hint">used only when active_policy=llm_icl_v0</span>
      </div>
      <div class="form-row">
        <span class="label">Base URL</span>
        <input type="text" id="cfg-learner-base-url">
        <span class="hint">OpenAI-compatible</span>
      </div>
      <div class="form-row">
        <span class="label">Model</span>
        <input type="text" id="cfg-learner-model">
        <span class="hint">fast text model is enough</span>
      </div>
      <div class="form-row">
        <span class="label">API key</span>
        <input type="password" id="cfg-learner-api-key">
        <span class="hint">optional for local endpoints</span>
      </div>
      <div class="form-row">
        <span class="label">Few-shot examples</span>
        <input type="number" id="cfg-learner-examples" min="0" max="64" step="1">
        <span class="hint">balanced explicit + implicit labels</span>
      </div>
      <div class="form-row">
        <span class="label">Min confidence</span>
        <input type="number" id="cfg-learner-conf" min="0" max="1" step="0.05">
        <span class="hint">threshold to allow a ping</span>
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
let dietReport = null;
let dietLoadedAt = 0;

// Tab switching
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('tab-' + t.dataset.tab).classList.add('active');
    if (t.dataset.tab === 'eval') loadEval();
    if (t.dataset.tab === 'diet') loadDiet();
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

async function loadDiet(force=false) {
  const now = Date.now();
  if (!force && dietReport && now - dietLoadedAt < 30000) {
    renderDiet(dietReport);
    return;
  }
  document.getElementById('diet-stats').innerHTML = `<div class="stat"><div class="label">Diet</div><div class="value">…</div><div class="sub">loading report</div></div>`;
  dietReport = await fetch('/information-diet/report?window=7d&max_episodes=20').then(r => r.json());
  dietLoadedAt = now;
  renderDiet(dietReport);
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
    {label: 'Workflow runs',         val: d.n_workflow_events || 0, sub: `avg ${numMaybe(d.workflow_avg_duration_sec)}s closed`},
    {label: 'Context packets',       val: d.n_context_packets || 0, sub: 'frozen policy inputs'},
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
  document.getElementById('workflow-events').innerHTML = (d.recent_workflow_events || []).map(row => `
    <div class="example-row">
      <div class="topline">
        <span><span class="pill info">${escapeHTML(row.status || 'event')}</span> ${escapeHTML(row.app || 'unknown')} · ${escapeHTML(row.scene_label || 'unknown')}</span>
        <span>${escapeHTML((row.last_ts || row.ts || '').slice(0,19))}</span>
      </div>
      <div class="msg">${escapeHTML(row.window_title || '(untitled window)')}</div>
      <div class="meta">duration=${escapeHTML(numMaybe(row.duration_sec))}s candidates=${escapeHTML(row.n_candidates ?? 0)} close=${escapeHTML(row.close_reason || 'open')}</div>
      ${row.ocr_preview ? `<div class="meta">preview=${escapeHTML(row.ocr_preview)}</div>` : ''}
    </div>`).join('') || '<div style="color:#7c7c86">no closed workflow events yet</div>';
  document.getElementById('context-packets').innerHTML = (d.recent_context_packets || []).map(row => {
    const obs = row.current_observation || {};
    const identity = obs.app_identity || {};
    const appName = obs.effective_app || obs.frontmost_app || '?';
    const rawApp = identity.raw_frontmost_app || obs.frontmost_app || '';
    const appMeta = rawApp && rawApp !== appName ? ` raw=${escapeHTML(rawApp)}` : '';
    const appFlags = (identity.flags || []).length ? ` flags=${escapeHTML((identity.flags || []).join(','))}` : '';
    const wf = row.current_workflow_event || {};
    const base = row.rule_baseline || {};
    const priors = row.kg_priors || {};
    return `<div class="example-row">
      <div class="topline">
        <span><span class="pill info">${escapeHTML(row.policy_name || 'policy')}</span> ${escapeHTML(appName)} · ${escapeHTML((obs.scene || {}).label || '?')}</span>
        <span>${escapeHTML((row.ts || '').slice(0,19))}</span>
      </div>
      <div class="msg">${escapeHTML(wf.window_title || obs.window_title || '(untitled window)')}</div>
      <div class="meta">packet=${escapeHTML(row.packet_id || '?')} workflow=${escapeHTML(row.workflow_event_id || '?')} baseline=${escapeHTML(base.action || '?')} examples=${escapeHTML((row.few_shot_examples || []).length)} priors=${escapeHTML(priors.n_examples ?? 0)}${appMeta}${appFlags}</div>
      ${obs.ocr_snippet ? `<div class="meta">screen=${escapeHTML(obs.ocr_snippet)}</div>` : ''}
    </div>`;
  }).join('') || '<div style="color:#7c7c86">no context packets yet</div>';
  document.getElementById('reason-bars').innerHTML = barsHTML(d.dist_reasons);
  document.getElementById('intent-bars').innerHTML = barsHTML(d.dist_intent_signals);
}

function renderEval(r) {
  const data = r.data || {};
  const labels = ((r.quality || {}).labels || {});
  const eventLabels = labels.event || {};
  const best = (((r.variants || {}).calibration || {}).best_variant || {});
  const stats = [
    {label: 'Decisions', val: data.n_decisions ?? 0, sub: `${data.n_pings ?? 0} pings · ${data.n_claimed_pings ?? 0} claimed`},
    {label: 'Trace complete', val: pctMaybe(data.trace_completeness_for_pings), sub: `${data.n_traces ?? 0} traces`},
    {label: 'Claimed capture', val: pctMaybe(data.outcome_capture_rate_for_claimed_pings), sub: `${pctMaybe(data.outcome_capture_rate_for_pings)} all pings`},
    {label: 'P/R/F1 labels', val: `${pctMaybe(labels.precision_labeled)} / ${pctMaybe(labels.recall_labeled)} / ${pctMaybe(labels.f1_labeled)}`, sub: `${data.n_explicit_labels ?? 0} labels`},
    {label: 'Event F1', val: pctMaybe(eventLabels.f1_labeled), sub: `${data.n_event_labels ?? 0} event labels`},
    {label: 'Best variant', val: best.variant || 'n/a', sub: `score ${numMaybe(best.score)}`},
  ];
  document.getElementById('eval-stats').innerHTML = stats.map(s => `
    <div class="stat">
      <div class="label">${escapeHTML(s.label)}</div>
      <div class="value" style="font-size:${String(s.val).length > 8 ? '15px' : '24px'}">${escapeHTML(s.val)}</div>
      ${s.sub ? `<div class="sub">${escapeHTML(s.sub)}</div>` : ''}
    </div>`).join('');

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

function renderDiet(r) {
  const s = r.summary || {};
  const stats = [
    {label: 'Research events', val: s.n_research_events ?? 0, sub: `${s.n_episodes ?? 0} episodes`},
    {label: 'Observed minutes', val: numMaybe(s.observed_research_min), sub: r.window || '7d'},
    {label: 'Top domain', val: Object.keys(s.top_domains || {})[0] || 'n/a', sub: 'OCR inferred'},
    {label: 'Hypotheses', val: (r.skill_hypotheses || []).length, sub: 'confidence-weighted'},
  ];
  document.getElementById('diet-stats').innerHTML = stats.map(st => `
    <div class="stat">
      <div class="label">${escapeHTML(st.label)}</div>
      <div class="value" style="font-size:${String(st.val).length > 12 ? '14px' : '24px'}">${escapeHTML(st.val)}</div>
      ${st.sub ? `<div class="sub">${escapeHTML(st.sub)}</div>` : ''}
    </div>`).join('');
  document.getElementById('diet-patterns').innerHTML = barsHTML(s.workflow_patterns || {});
  document.getElementById('diet-domains').innerHTML = barsHTML(s.top_domains || {});
  document.getElementById('diet-skills').innerHTML = (r.skill_hypotheses || []).map(row => `
    <div class="example-row">
      <div class="topline">
        <span><span class="pill info">${numMaybe(row.confidence)}</span> ${escapeHTML(row.topic || 'topic')}</span>
        <span>${escapeHTML(numMaybe(row.observed_duration_min))}m</span>
      </div>
      <div class="msg">${escapeHTML(row.hypothesis || '')}</div>
      <div class="meta">patterns=${escapeHTML(Object.keys(row.patterns || {}).join(', ') || 'none')} domains=${escapeHTML((row.domains || []).join(', ') || 'none')}</div>
    </div>`).join('') || '<div style="color:#7c7c86">no skill hypotheses yet</div>';
  document.getElementById('diet-episodes').innerHTML = (r.episodes || []).map(ep => `
    <div class="example-row">
      <div class="topline">
        <span>${escapeHTML(ep.task_hypothesis || 'research episode')}</span>
        <span>${escapeHTML((ep.ts_start || '').slice(0,19))}</span>
      </div>
      <div class="meta">duration=${escapeHTML(numMaybe(ep.observed_duration_min))}m events=${escapeHTML(ep.n_events)} patterns=${escapeHTML((ep.workflow_patterns || []).join(', '))}</div>
      <div class="meta">domains=${escapeHTML((ep.source_domains || []).join(', ') || 'none')} queries=${escapeHTML((ep.query_candidates || []).join(' | ') || 'none')}</div>
    </div>`).join('') || '<div style="color:#7c7c86">no research episodes in this window</div>';
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
  const learner = c.policy_learner || {};
  const rw = (c.reward && c.reward.weights) || {};
  const d = c.daemon || {};
  const intents = (c.intents && c.intents.enabled) || [];

  document.getElementById('cfg-poll').value         = d.poll_interval_sec ?? '';
  document.getElementById('cfg-fisherman-url').value= d.fisherman_url ?? '';
  document.getElementById('cfg-policy').value       = g.active_policy ?? '';
  document.getElementById('cfg-cooldown').value     = g.cooldown_min ?? '';
  document.getElementById('cfg-quiet').value        = `${g.quiet_hours_start ?? 22}-${g.quiet_hours_end ?? 8}`;
  document.getElementById('cfg-learner-enabled').checked = !!learner.enabled;
  document.getElementById('cfg-learner-base-url').value  = learner.base_url ?? '';
  document.getElementById('cfg-learner-model').value     = learner.model ?? '';
  document.getElementById('cfg-learner-api-key').value   = learner.api_key ?? '';
  document.getElementById('cfg-learner-examples').value  = learner.max_examples ?? 16;
  document.getElementById('cfg-learner-conf').value      = learner.min_confidence_to_ping ?? 0.55;
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
  next.policy_learner ||= {};
  next.policy_learner.enabled = document.getElementById('cfg-learner-enabled').checked;
  next.policy_learner.base_url = document.getElementById('cfg-learner-base-url').value;
  next.policy_learner.model = document.getElementById('cfg-learner-model').value;
  next.policy_learner.api_key = document.getElementById('cfg-learner-api-key').value;
  next.policy_learner.max_examples = parseInt(document.getElementById('cfg-learner-examples').value || '16');
  next.policy_learner.min_confidence_to_ping = parseFloat(document.getElementById('cfg-learner-conf').value || '0.55');
  next.policy_learner.api_key_env ||= 'HARNESS_REALIZER_KEY';
  next.policy_learner.timeout_sec ||= 8;
  next.policy_learner.max_tokens ||= 220;
  next.policy_learner.temperature ||= 0.0;
  next.policy_learner.min_interval_sec ||= 15;
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
    try:
        if sql_store.db_path().exists() and sql_store.count_rows("decisions") > 0:
            return _aggregate_sql(window_sec)
    except Exception:
        pass
    return _aggregate_jsonl(window_sec)


def _aggregate_sql(window_sec: int = 86400) -> dict:
    """Compute the live dashboard from typed SQLite columns.

    The floating notch refreshes this path while the notification client is
    polling. Keep it bounded: aggregate from typed columns and only decode small
    recent samples.
    """
    now = time.time()
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - window_sec))

    dist_actions = Counter(sql_store.value_counts("decisions", "action", since_iso=since_iso, limit=20))
    dist_scenes = Counter(sql_store.value_counts("candidates", "scene_label", since_iso=since_iso, limit=20))
    dist_apps = Counter(sql_store.value_counts("candidates", "frontmost_app", since_iso=since_iso, limit=20))
    dist_reasons = Counter(sql_store.decision_reason_counts(since_iso=since_iso, limit=20))
    dist_intent_signals = Counter(sql_store.value_counts("outcomes", "intent_signal", since_iso=since_iso, limit=20))
    outcome_actions = Counter(sql_store.value_counts("outcomes", "user_action", since_iso=since_iso, limit=20))

    n_candidates = sql_store.count_payload_rows("candidates", since_iso=since_iso)
    n_decisions = sql_store.count_payload_rows("decisions", since_iso=since_iso)
    n_outcomes = sql_store.count_payload_rows("outcomes", since_iso=since_iso)
    n_workflow_events = sql_store.count_payload_rows("workflow_events", since_iso=since_iso)
    n_context_packets = sql_store.count_payload_rows("context_packets", since_iso=since_iso)

    recent_decisions = sql_store.payload_rows("decisions", since_iso=since_iso, limit=30, newest_first=True)
    recent_outcomes = sql_store.payload_rows("outcomes", since_iso=since_iso, limit=15, newest_first=True)
    recent_workflow_events = sql_store.payload_rows("workflow_events", since_iso=since_iso, limit=12, newest_first=True)
    recent_context_packets = sql_store.payload_rows("context_packets", since_iso=since_iso, limit=12, newest_first=True)
    traces = sql_store.payload_rows("traces", limit=80, newest_first=True)
    model_calls = sql_store.payload_rows("model_calls", limit=30, newest_first=True)

    n_considered_no_click = 0
    for outcome in recent_outcomes:
        ua = outcome.get("user_action")
        sig = (outcome.get("interaction_summary") or {}).get("intent_signal")
        if sig == "considered" and ua == "timed_out":
            n_considered_no_click += 1

    recent_realizations = []
    for trace in traces:
        r = trace.get("realization") or {}
        if not r.get("message"):
            continue
        tool_calls = r.get("tool_calls") or []
        provider_reasoning = next(
            (tc.get("result_summary") for tc in tool_calls
             if tc.get("name", "").startswith("_provider_")),
            None,
        )
        recent_realizations.append({
            "ts": trace.get("ts"),
            "intent": (trace.get("action") or {}).get("intent"),
            "why_now": (trace.get("action") or {}).get("why_now"),
            "message": r.get("message"),
            "latency_ms": r.get("latency_ms"),
            "vision_used": r.get("vision_used"),
            "privacy_flags": r.get("privacy_flags") or [],
            "privacy_provenance": r.get("privacy_provenance") or {},
            "provider_reasoning": provider_reasoning,
        })
        if len(recent_realizations) >= 10:
            break

    ping_ids = sql_store.decision_ids_by_action("notch_ping", since_iso=since_iso)
    displayed_ids = sql_store.displayed_ping_decision_ids(since_iso=since_iso) & ping_ids
    outcome_ids = {
        str(row.get("decision_id"))
        for row in sql_store.payload_rows("outcomes", since_iso=since_iso, limit=5000, newest_first=False)
        if row.get("decision_id")
    }

    return {
        "n_candidates": n_candidates,
        "n_decisions": n_decisions,
        "n_outcomes": n_outcomes,
        "n_pings": dist_actions.get("notch_ping", 0),
        "n_claimed_pings": len(displayed_ids),
        "outcome_capture_rate_for_pings": _ratio(len(ping_ids & outcome_ids), len(ping_ids)),
        "outcome_capture_rate_for_claimed_pings": _ratio(len(displayed_ids & outcome_ids), len(displayed_ids)),
        "n_clicked": outcome_actions.get("clicked", 0),
        "n_dismissed": outcome_actions.get("dismissed", 0),
        "n_considered_no_click": n_considered_no_click,
        "n_workflow_events": n_workflow_events,
        "n_context_packets": n_context_packets,
        "workflow_avg_duration_sec": sql_store.workflow_avg_duration(since_iso=since_iso),
        "dist_actions": dict(dist_actions),
        "dist_scenes": dict(dist_scenes),
        "dist_apps": dict(dist_apps),
        "dist_reasons": dict(dist_reasons),
        "dist_intent_signals": dict(dist_intent_signals),
        "recent_decisions": recent_decisions,
        "recent_outcomes": recent_outcomes,
        "recent_workflow_events": recent_workflow_events,
        "recent_context_packets": recent_context_packets,
        "recent_realizations": recent_realizations,
        "recent_model_calls": model_calls,
    }


def _aggregate_jsonl(window_sec: int = 86400) -> dict:
    """Read typed event payloads and compute distributions over the last window."""
    now = time.time()
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - window_sec))

    candidates = _read_payloads("candidates", "candidates.jsonl", since_iso=since_iso)
    decisions = _read_payloads("decisions", "decisions.jsonl", since_iso=since_iso)
    outcomes = _read_payloads("outcomes", "outcomes.jsonl", since_iso=since_iso)
    traces = list(reversed(_read_payloads("traces", "traces.jsonl", limit=50, newest_first=True)))
    workflow_events = _read_payloads("workflow_events", "workflow_events.jsonl", since_iso=since_iso)
    context_packets = _read_payloads("context_packets", "context_packets.jsonl", since_iso=since_iso)

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

    workflow_durations = [
        float(row.get("duration_sec") or 0.0)
        for row in workflow_events
        if row.get("status") == "closed"
    ]

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
        "n_workflow_events": len(workflow_events),
        "n_context_packets": len(context_packets),
        "workflow_avg_duration_sec": (
            round(sum(workflow_durations) / len(workflow_durations), 2)
            if workflow_durations else None
        ),
        "dist_actions": dict(dist_actions),
        "dist_scenes": dict(dist_scenes.most_common(20)),
        "dist_apps": dict(dist_apps.most_common(20)),
        "dist_reasons": dict(dist_reasons.most_common(20)),
        "dist_intent_signals": dict(dist_intent_signals),
        "recent_decisions": decisions[-30:][::-1],
        "recent_outcomes": outcomes[-15:][::-1],
        "recent_workflow_events": workflow_events[-12:][::-1],
        "recent_context_packets": context_packets[-12:][::-1],
        "recent_realizations": recent_realizations,
        "recent_model_calls": model_calls,
    }


def _ratio(num: float, den: float) -> float | None:
    if not den:
        return None
    return num / den


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
    section_order = ["daemon", "gate", "experiment", "trainer", "policy_learner", "scene", "scene_tagger", "memory",
                     "long_term_memory", "context_packets",
                     "workflow_events", "realizer", "critic", "privacy", "push", "reward", "intents", "debug"]
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
    return web.json_response(await asyncio.to_thread(_aggregate, secs))


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
        gate_cfg = body.get("gate") if isinstance(body.get("gate"), dict) else {}
        active_policy = gate_cfg.get("active_policy")
        if active_policy in {"rule_v0", "llm_icl_v0"}:
            state = read_policy_state()
            state["active_policy"] = active_policy
            write_policy_state(state)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


def attach_routes(app: web.Application) -> None:
    app.router.add_get("/dashboard", get_dashboard_page)
    app.router.add_get("/dashboard/data", get_dashboard_data)
    app.router.add_get("/dashboard/config", get_dashboard_config)
    app.router.add_post("/dashboard/config", post_dashboard_config)
