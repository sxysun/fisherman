"""Rewind-style retro labeling UI. Served by the harness daemon on :7893.

Endpoints:
  GET  /label                                 → HTML page (scrubber UI)
  GET  /label/queue                           → next unlabeled decision in a frozen session (JSON)
  GET  /label/timeline/<candidate_id>         → frames in window around decision
  GET  /label/frame/{ts_ms}                   → JPEG proxied from Fisherman
  POST /label/submit                          → append a row to retro_labels.jsonl

The page is a single SwiftUI-feel scrubber: large frame view, time-scrubber with
±2 min window, thumbnail strip, decision-moment highlighted, side panel with
decision details, label buttons + confidence + notes.
"""

from __future__ import annotations

import calendar
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import ClientSession, ClientTimeout, web

from .store import HARNESS_DIR, append_jsonl, iter_jsonl, tail_jsonl


WINDOW_BEFORE_SEC = 120  # 2 minutes before decision
WINDOW_AFTER_SEC = 120   # 2 minutes after decision
MAX_TIMELINE_FRAMES = 60  # 4 min window @ ~one frame every 4s — close to Rewind density


# ────────────────────────────────────────────────────────────────────────────
# HTML
# ────────────────────────────────────────────────────────────────────────────

LABELING_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>harness · retro label</title>
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
    overflow: hidden;
  }

  .app {
    display: grid;
    grid-template-rows: auto 1fr auto;
    height: 100vh;
  }

  /* ── Header ─────────────────────────────────────────────────────────── */
  header {
    padding: 12px 24px;
    display: grid;
    grid-template-columns: minmax(220px, 1fr) auto auto;
    gap: 16px;
    align-items: center;
    border-bottom: 1px solid var(--border);
  }
  header h1 {
    margin: 0; font: 600 12px/1 -apple-system;
    text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-2);
  }
  header .progress { color: var(--text-3); font: 11px ui-monospace; }
  header .progress strong { color: var(--text-2); font-weight: 600; }
  header .keys { color: var(--text-3); font: 11px ui-monospace; }
  header .keys kbd {
    display: inline-block; padding: 1px 6px; margin: 0 2px;
    background: var(--panel-2); border: 1px solid var(--border-2);
    border-radius: 4px; font: 10px ui-monospace; color: var(--text-2);
  }
  .queue-controls {
    display: flex; align-items: center; gap: 8px;
  }
  .queue-controls select, .queue-controls button {
    height: 28px;
    background: var(--panel);
    border: 1px solid var(--border-2);
    color: var(--text-2);
    border-radius: 6px;
    font: 11px ui-monospace;
  }
  .queue-controls select { padding: 0 8px; }
  .queue-controls button { padding: 0 10px; cursor: pointer; }
  .queue-controls button:hover { color: var(--text); border-color: var(--text-3); }

  /* ── Main ───────────────────────────────────────────────────────────── */
  main {
    display: grid;
    grid-template-columns: 1fr 320px;
    gap: 24px;
    padding: 20px 24px;
    overflow: hidden;
  }

  /* Frame viewer */
  .viewer {
    display: flex; flex-direction: column; gap: 14px;
    min-width: 0;
  }
  .stage {
    flex: 1; min-height: 0;
    background: #000;
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    display: flex; align-items: center; justify-content: center;
    position: relative;
  }
  .stage img {
    max-width: 100%; max-height: 100%;
    display: block;
  }
  .stage .stage-empty {
    color: var(--text-3); font: 12px ui-monospace;
  }
  .stage .stage-overlay {
    position: absolute; top: 12px; left: 12px;
    background: rgba(0,0,0,0.6); padding: 4px 10px; border-radius: 6px;
    font: 10.5px ui-monospace; color: var(--text-2);
    backdrop-filter: blur(8px);
  }
  .stage .stage-mark {
    position: absolute; top: 12px; right: 12px;
    background: var(--accent); color: #1a1408;
    padding: 4px 10px; border-radius: 6px;
    font: 10.5px 600 ui-monospace; text-transform: uppercase; letter-spacing: 0.06em;
  }

  /* Scrubber */
  .scrubber-wrap {
    display: flex; flex-direction: column; gap: 8px;
  }
  .scrubber-row { display: flex; align-items: center; gap: 12px; }
  .scrubber {
    flex: 1; position: relative;
    height: 40px; border-radius: 8px;
    background: var(--panel-2);
    border: 1px solid var(--border);
    overflow: hidden;
    cursor: grab;
    user-select: none;
  }
  .scrubber.dragging { cursor: grabbing; }
  /* tick marks every ~20 frames */
  .scrubber-ticks {
    position: absolute; inset: 0;
    background-image: linear-gradient(to right,
      transparent 0, transparent calc(100% / 12 - 1px),
      rgba(255,255,255,0.04) calc(100% / 12 - 1px),
      rgba(255,255,255,0.04) calc(100% / 12));
    pointer-events: none;
  }
  .scrubber-track-decision {
    position: absolute; top: 0; bottom: 0; width: 2px;
    background: var(--accent);
    box-shadow: 0 0 8px rgba(208,192,143,0.5);
    pointer-events: none;
  }
  .scrubber-cursor {
    position: absolute; top: -3px; bottom: -3px;
    width: 3px; margin-left: -1px;
    background: var(--text);
    border-radius: 2px;
    box-shadow: 0 0 0 1px rgba(0,0,0,0.6), 0 0 8px rgba(255,255,255,0.3);
    pointer-events: none;
    will-change: left;
  }
  .scrubber:not(.dragging) .scrubber-cursor { transition: left 80ms ease-out; }
  .scrubber-tooltip {
    position: absolute; bottom: calc(100% + 6px);
    transform: translateX(-50%);
    background: var(--panel); border: 1px solid var(--border-2);
    padding: 3px 8px; border-radius: 4px;
    font: 10.5px ui-monospace; color: var(--text-2);
    pointer-events: none;
    opacity: 0; transition: opacity 100ms;
    white-space: nowrap;
  }
  .scrubber.dragging .scrubber-tooltip { opacity: 1; }
  .scrubber-time {
    font: 11px ui-monospace; color: var(--text-2);
    min-width: 56px; text-align: right;
  }

  .strip {
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(60px, 1fr);
    gap: 4px;
    overflow-x: auto;
    padding-bottom: 2px;
    scrollbar-width: thin; scrollbar-color: var(--border-2) transparent;
  }
  .strip::-webkit-scrollbar { height: 6px; }
  .strip::-webkit-scrollbar-thumb { background: var(--border-2); border-radius: 3px; }
  .thumb {
    aspect-ratio: 16/10;
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 4px;
    cursor: pointer;
    overflow: hidden;
    position: relative;
    transition: border-color 80ms;
  }
  .thumb:hover { border-color: var(--border-2); }
  .thumb.active { border-color: var(--text); }
  .thumb.decision::after {
    content: ""; position: absolute; top: 0; left: 0; right: 0; height: 2px;
    background: var(--accent);
  }
  .thumb img {
    width: 100%; height: 100%; object-fit: cover; display: block;
    opacity: 0.85; transition: opacity 80ms;
  }
  .thumb.active img, .thumb:hover img { opacity: 1; }

  /* Side panel */
  aside {
    display: flex; flex-direction: column; gap: 14px;
    overflow-y: auto;
    padding-right: 4px;
  }
  .meta-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px;
  }
  .meta-card h2 {
    margin: 0 0 10px 0;
    font: 600 10px/1 -apple-system;
    text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--text-3);
  }
  .rubric {
    border-color: rgba(208, 192, 143, 0.35);
    background: linear-gradient(180deg, rgba(208, 192, 143, 0.08), var(--panel) 42%);
  }
  .rubric p {
    margin: 0 0 8px;
    color: var(--text-2);
    font-size: 12px;
  }
  .rubric ol {
    margin: 0;
    padding-left: 18px;
    color: var(--text-2);
    font-size: 12px;
  }
  .rubric li { margin: 4px 0; }
  .meta-row {
    display: flex; justify-content: space-between; gap: 12px;
    padding: 4px 0;
    font-size: 12px;
  }
  .meta-row .k { color: var(--text-3); }
  .meta-row .v { color: var(--text); text-align: right; font: 12px ui-monospace; word-break: break-all; }
  .badge {
    display: inline-block;
    padding: 2px 8px; border-radius: 4px;
    font: 600 10.5px ui-monospace;
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  .badge.no_ping { background: rgba(125, 158, 196, 0.16); color: var(--blue); }
  .badge.notch_ping { background: rgba(196, 168, 125, 0.18); color: var(--amber); }
  .reasons {
    margin-top: 8px;
    color: var(--text-2);
    font: 11.5px ui-monospace;
    word-break: break-word;
  }
  .ocr {
    margin-top: 10px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px;
    font: 11px/1.45 ui-monospace, SFMono-Regular;
    color: var(--text-2);
    max-height: 220px; overflow-y: auto;
    white-space: pre-wrap;
  }

  /* Footer (labels) */
  footer {
    padding: 16px 24px 20px;
    border-top: 1px solid var(--border);
    background: linear-gradient(to bottom, transparent, rgba(0,0,0,0.2));
  }
  .label-row {
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 8px;
  }
  .label-btn {
    background: var(--panel); color: var(--text);
    border: 1px solid var(--border-2);
    border-radius: 8px;
    padding: 14px 16px;
    cursor: pointer;
    text-align: left;
    font: 12.5px/1.35 -apple-system;
    display: grid;
    grid-template-columns: 1fr auto;
    align-items: center;
    gap: 12px;
    transition: background 80ms, border-color 80ms, transform 80ms;
    position: relative;
  }
  .label-btn:hover { background: var(--panel-2); transform: translateY(-1px); }
  .label-btn:active { transform: translateY(0); }
  .label-btn .lbl-body { display: flex; flex-direction: column; gap: 3px; }
  .label-btn .lbl-emoji { font-size: 15px; }
  .label-btn .lbl-title { font-weight: 600; font-size: 13px; }
  .label-btn .lbl-hint { font: 10.5px ui-monospace; color: var(--text-3); }
  .label-btn .lbl-key {
    display: flex; align-items: center; justify-content: center;
    width: 36px; height: 36px;
    background: var(--panel-2);
    border: 1px solid var(--border-2);
    border-radius: 8px;
    font: 600 14px ui-monospace;
    color: var(--text);
    box-shadow: 0 1px 0 rgba(0,0,0,0.4), inset 0 -2px 0 rgba(255,255,255,0.04);
  }
  .label-btn:hover .lbl-key { background: var(--panel); border-color: var(--accent); color: var(--accent); }
  .label-btn.help { border-color: rgba(126, 179, 126, 0.4); }
  .label-btn.help:hover { background: rgba(126, 179, 126, 0.08); }
  .label-btn.annoy { border-color: rgba(196, 132, 140, 0.4); }
  .label-btn.annoy:hover { background: rgba(196, 132, 140, 0.08); }
  .label-btn.good { border-color: rgba(125, 158, 196, 0.4); }
  .label-btn.good:hover { background: rgba(125, 158, 196, 0.08); }
  .label-btn.cant { border-color: var(--border-2); }

  .extras {
    display: flex; gap: 12px; align-items: center; margin-top: 10px;
  }
  .extras .conf {
    display: flex; align-items: center; gap: 8px; flex: 1;
    color: var(--text-3); font: 11px ui-monospace;
  }
  .extras .conf input { flex: 1; }
  .extras input[type="text"] {
    flex: 2;
    background: var(--bg); border: 1px solid var(--border-2); color: var(--text);
    border-radius: 6px; padding: 6px 10px;
    font: 12px ui-monospace;
  }
  .extras input[type="text"]:focus { outline: none; border-color: var(--text-3); }

  .empty {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    padding: 80px; color: var(--text-3); gap: 12px;
  }
  .empty h1 { font: 600 18px/1.2; color: var(--text-2); margin: 0; }
</style>
</head><body>
<div class="app" id="app">
  <div class="empty">loading…</div>
</div>

<script>
let timeline = null;
let activeIdx = 0;
let decisionIdx = 0;
let confidence = 0.7;
let playing = null;
let labeledCount = 0;
let sessionSkipped = new Set();
let sessionCutoff = new Date().toISOString().replace(/[.][0-9]{3}Z$/, 'Z');
let queueFilter = 'all';
let queueOrder = 'newest';
let lastQueue = null;
let loading = false;

async function loadNext(opts = {}) {
  if (loading) return;
  if (opts.skipCurrent && timeline) {
    sessionSkipped.add(queueKey(timeline));
  }
  loading = true;
  setLoading();
  const params = new URLSearchParams({
    before_ts: sessionCutoff,
    order: queueOrder,
    action: queueFilter,
    exclude: Array.from(sessionSkipped).join(','),
  });
  const q = await fetch('/label/queue?' + params.toString()).then(r => r.json());
  if (!q) { loading = false; timeline = null; renderEmpty(); return; }
  lastQueue = q;

  const timelineParams = new URLSearchParams({ decision_id: q.decision_id || '' });
  const t = await fetch(`/label/timeline/${q.candidate_id}?${timelineParams.toString()}`).then(r => r.json());
  if (!t || t.error) { loading = false; sessionSkipped.add(q.decision_id || q.candidate_id); loadNext(); return; }
  timeline = t;
  decisionIdx = (t.frames || []).findIndex(f => f.is_decision);
  if (decisionIdx < 0) decisionIdx = Math.max(0, Math.floor(((t.frames || []).length - 1) / 2));
  activeIdx = decisionIdx;
  loading = false;
  render();
  preloadAll();
}

function setLoading() {
  document.getElementById('app').innerHTML = `<div class="empty">loading…</div>`;
}

function renderEmpty() {
  document.getElementById('app').innerHTML = `
    <div class="empty">
      <h1>queue empty</h1>
      <div>no unlabeled decisions in this frozen review session. labeled this session: ${labeledCount}</div>
      <div>session cutoff: ${sessionCutoff} · skipped this session: ${sessionSkipped.size}</div>
      <button class="label-btn" style="max-width:240px;text-align:center;display:block;" onclick="resetSession()">start fresh session</button>
    </div>`;
}

function render() {
  if (!timeline) return;
  const t = timeline;
  const frames = t.frames || [];
  const cur = frames[activeIdx] || {};
  const dec = t.decision || {};
  const action = dec.action || '?';
  const intent = dec.intent || '—';
  const reasons = (dec.reason_codes || []).join(', ') || '—';
  const qProgress = (lastQueue && lastQueue.progress) || {};
  const remaining = qProgress.remaining ?? ((t.progress && t.progress.remaining) || 0);

  document.getElementById('app').innerHTML = `
    <header>
      <div>
        <h1>retro label · ${shortID(dec.decision_id || t.candidate_id || '')}</h1>
        <div class="progress"><strong>${labeledCount}</strong> labeled · <strong>${sessionSkipped.size}</strong> skipped · <strong>${remaining}</strong> left in session</div>
      </div>
      <div class="queue-controls">
        <select onchange="queueFilter = this.value; sessionSkipped.clear(); loadNext()" title="Decision type">
          <option value="all" ${queueFilter === 'all' ? 'selected' : ''}>all decisions</option>
          <option value="notch_ping" ${queueFilter === 'notch_ping' ? 'selected' : ''}>pings only</option>
          <option value="no_ping" ${queueFilter === 'no_ping' ? 'selected' : ''}>silences only</option>
        </select>
        <select onchange="queueOrder = this.value; sessionSkipped.clear(); loadNext()" title="Queue order">
          <option value="newest" ${queueOrder === 'newest' ? 'selected' : ''}>newest first</option>
          <option value="oldest" ${queueOrder === 'oldest' ? 'selected' : ''}>oldest first</option>
        </select>
        <button onclick="loadNext({skipCurrent:true})" title="Skip this decision in the current session">skip</button>
        <button onclick="resetSession()" title="Use current time as a new frozen cutoff">reset</button>
        <button onclick="location.href='/label/events'" title="Review whole workflow events">events</button>
      </div>
      <div class="keys">
        <kbd>←</kbd><kbd>→</kbd> scrub
        <kbd>space</kbd> play
        <kbd>1</kbd><kbd>2</kbd><kbd>3</kbd> label
        <kbd>s</kbd> skip
      </div>
    </header>
    <main>
      <section class="viewer">
        <div class="stage" id="stage">
          ${cur.ts_ms ? `<img src="/label/frame/${cur.ts_ms}" alt="frame">` : `<div class="stage-empty">no frame at this time</div>`}
          <div class="stage-overlay">${cur.app || '?'} · ${cur.offset_sec >= 0 ? '+' : ''}${cur.offset_sec || 0}s · ${cur.ts_iso || ''}</div>
          ${activeIdx === decisionIdx ? '<div class="stage-mark">decision moment</div>' : ''}
        </div>
        <div class="scrubber-wrap">
          <div class="scrubber-row">
            <span class="scrubber-time">${frames[0]?.offset_sec || 0}s</span>
            <div class="scrubber" id="scrubber" onmousedown="startScrub(event)">
              <div class="scrubber-ticks"></div>
              <div class="scrubber-track-decision" style="left: ${pctForIdx(decisionIdx)}%;"></div>
              <div class="scrubber-cursor" id="cursor" style="left: ${pctForIdx(activeIdx)}%;">
                <div class="scrubber-tooltip" id="cursor-tooltip">${frames[activeIdx]?.offset_sec >= 0 ? '+' : ''}${frames[activeIdx]?.offset_sec || 0}s</div>
              </div>
            </div>
            <span class="scrubber-time">+${frames[frames.length-1]?.offset_sec || 0}s</span>
          </div>
          <div class="strip" id="strip">
            ${frames.map((f, i) => `
              <div class="thumb ${i === activeIdx ? 'active' : ''} ${i === decisionIdx ? 'decision' : ''}"
                   onclick="setIdx(${i})" id="thumb-${i}">
                ${f.ts_ms ? `<img loading="lazy" src="/label/frame/${f.ts_ms}" alt="">` : ''}
              </div>`).join('')}
          </div>
        </div>
      </section>
      <aside>
        <div class="meta-card rubric">
          <h2>labeling rule</h2>
          <p>Judge the yellow decision moment, not what is on screen now. Ask: would an interruption from Hermes have been useful right then?</p>
          <ol>
            <li><strong>Should ping</strong>: Hermes should interrupt here, or this ping was useful.</li>
            <li><strong>Should stay quiet</strong>: silence was correct, or this ping was unwanted.</li>
            <li><strong>Can't tell</strong>: the frame does not contain enough context.</li>
          </ol>
        </div>
        <div class="meta-card">
          <h2>decision</h2>
          <div class="meta-row"><span class="k">action</span><span class="v"><span class="badge ${action}">${action}</span></span></div>
          <div class="meta-row"><span class="k">intent</span><span class="v">${intent}</span></div>
          <div class="meta-row"><span class="k">policy</span><span class="v">${dec.policy_version || '?'}</span></div>
          <div class="meta-row"><span class="k">ts</span><span class="v">${t.decision_ts_iso || '?'}</span></div>
          <div class="reasons">reasons: ${reasons}</div>
        </div>
        <div class="meta-card">
          <h2>at decision moment</h2>
          <div class="meta-row"><span class="k">app</span><span class="v">${(t.screen || {}).frontmost_app || '?'}</span></div>
          <div class="meta-row"><span class="k">scene</span><span class="v">${(t.scene || {}).label || '?'} (${(t.scene || {}).strength || '?'})</span></div>
          <div class="meta-row"><span class="k">frame_age</span><span class="v">${Math.round((t.screen || {}).frame_age_sec || 0)}s</span></div>
          <div class="meta-row"><span class="k">capture_gap</span><span class="v">${Math.round((t.screen || {}).capture_gap_sec || 0)}s</span></div>
          <div class="ocr">${escapeHTML((t.screen || {}).ocr_snippet || '(no ocr)')}</div>
        </div>
        ${t.memory ? `
        <div class="meta-card">
          <h2>memory snapshot</h2>
          <div class="meta-row"><span class="k">app switches 15m</span><span class="v">${t.memory.app_switches_last_15m}</span></div>
          <div class="meta-row"><span class="k">mins on current</span><span class="v">${t.memory.minutes_on_current_app}</span></div>
          <div class="meta-row"><span class="k">session boundary</span><span class="v">${t.memory.session_boundary || 'none'}</span></div>
          <div class="meta-row"><span class="k">recent apps</span><span class="v">${(t.memory.recent_apps || []).slice(-5).join(', ') || '—'}</span></div>
        </div>` : ''}
      </aside>
    </main>
    <footer>
      <div class="label-row">
        ${labelButtons(action)}
      </div>
      <div class="extras">
        <div class="conf">
          confidence
          <input type="range" min="0.1" max="1.0" step="0.1" value="${confidence}"
                 oninput="confidence = parseFloat(this.value)">
          <span id="confval" style="min-width:24px; text-align:right;">${confidence.toFixed(1)}</span>
        </div>
        <input type="text" id="notes" placeholder="optional note…">
      </div>
    </footer>
  `;
  document.querySelector('.thumb.active')?.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
  document.querySelector('.conf input').addEventListener('input', e => {
    document.getElementById('confval').textContent = parseFloat(e.target.value).toFixed(1);
  });
}

function labelButtons(action) {
  const pingTitle = action === 'notch_ping' ? 'Good ping' : 'Should have pinged';
  const pingHint = action === 'notch_ping' ? 'useful interruption' : 'missed useful interruption';
  const quietLabel = action === 'notch_ping' ? 'would_annoy' : 'good_no_ping';
  const quietTitle = action === 'notch_ping' ? 'Should stay quiet' : 'Good silence';
  const quietHint = action === 'notch_ping' ? 'this ping was unwanted' : 'no ping was correct';
  const quietClass = action === 'notch_ping' ? 'annoy' : 'good';
  return `
    <button class="label-btn help" onclick="submit('would_help')">
      <span class="lbl-body">
        <span class="lbl-title">${pingTitle}</span>
        <span class="lbl-hint">${pingHint}</span>
      </span>
      <span class="lbl-key">1</span>
    </button>
    <button class="label-btn ${quietClass}" onclick="submit('${quietLabel}')">
      <span class="lbl-body">
        <span class="lbl-title">${quietTitle}</span>
        <span class="lbl-hint">${quietHint}</span>
      </span>
      <span class="lbl-key">2</span>
    </button>
    <button class="label-btn cant" onclick="submit('cant_tell')">
      <span class="lbl-body">
        <span class="lbl-title">Can't tell</span>
        <span class="lbl-hint">not enough context</span>
      </span>
      <span class="lbl-key">3</span>
    </button>
    <button class="label-btn cant" onclick="loadNext({skipCurrent:true})">
      <span class="lbl-body">
        <span class="lbl-title">Skip</span>
        <span class="lbl-hint">do not label this example</span>
      </span>
      <span class="lbl-key">S</span>
    </button>`;
}

function pctForIdx(idx) {
  if (!timeline || !timeline.frames || timeline.frames.length < 2) return 50;
  return (idx / (timeline.frames.length - 1)) * 100;
}

function setIdx(i) {
  if (!timeline || !timeline.frames || timeline.frames.length === 0) return;
  const n = timeline.frames.length;
  activeIdx = Math.max(0, Math.min(n - 1, i));
  // partial re-render: update stage + cursor + thumb active class
  const f = timeline.frames[activeIdx] || {};
  const stage = document.getElementById('stage');
  if (stage) {
    stage.innerHTML = `
      ${f.ts_ms ? `<img src="/label/frame/${f.ts_ms}" alt="frame">` : `<div class="stage-empty">no frame</div>`}
      <div class="stage-overlay">${f.app || '?'} · ${f.offset_sec >= 0 ? '+' : ''}${f.offset_sec || 0}s · ${f.ts_iso || ''}</div>
      ${activeIdx === decisionIdx ? '<div class="stage-mark">decision moment</div>' : ''}
    `;
  }
  document.getElementById('cursor').style.left = pctForIdx(activeIdx) + '%';
  const tooltip = document.getElementById('cursor-tooltip');
  if (tooltip) {
    const offsetSec = (timeline.frames[activeIdx] || {}).offset_sec || 0;
    tooltip.textContent = (offsetSec >= 0 ? '+' : '') + offsetSec + 's';
  }
  document.querySelectorAll('.thumb').forEach((el, idx) => {
    el.classList.toggle('active', idx === activeIdx);
  });
  // Only scroll the strip when not dragging (otherwise it fights with cursor pos)
  if (!isDragging) {
    document.getElementById(`thumb-${activeIdx}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
  }
}

// ────── Drag scrubbing (Rewind-style) ───────────────────────────────────
let isDragging = false;
let dragRAF = null;
let lastDragEvt = null;

function startScrub(e) {
  if (!timeline) return;
  e.preventDefault();
  isDragging = true;
  document.getElementById('scrubber')?.classList.add('dragging');
  lastDragEvt = e;
  updateFromMouse(e);
  document.addEventListener('mousemove', onScrubMove);
  document.addEventListener('mouseup', endScrub);
}

function onScrubMove(e) {
  if (!isDragging) return;
  lastDragEvt = e;
  if (dragRAF) return;
  dragRAF = requestAnimationFrame(() => {
    dragRAF = null;
    if (lastDragEvt) updateFromMouse(lastDragEvt);
  });
}

function endScrub() {
  isDragging = false;
  document.getElementById('scrubber')?.classList.remove('dragging');
  document.removeEventListener('mousemove', onScrubMove);
  document.removeEventListener('mouseup', endScrub);
  if (dragRAF) { cancelAnimationFrame(dragRAF); dragRAF = null; }
}

function updateFromMouse(e) {
  if (!timeline) return;
  const scrubber = document.getElementById('scrubber');
  if (!scrubber) return;
  const rect = scrubber.getBoundingClientRect();
  const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const n = timeline.frames.length;
  setIdx(Math.round(pct * (n - 1)));
}

function preloadAround(i) {
  if (!timeline) return;
  for (let k = -3; k <= 3; k++) {
    const f = timeline.frames[i + k];
    if (f && f.ts_ms) { const img = new Image(); img.src = `/label/frame/${f.ts_ms}`; }
  }
}

function preloadAll() {
  // Trigger browser cache for every frame in the window — makes drag-scrubbing
  // feel instant. Costs ~5MB for 60 frames.
  if (!timeline) return;
  for (const f of timeline.frames) {
    if (f && f.ts_ms) { const img = new Image(); img.src = `/label/frame/${f.ts_ms}`; }
  }
}

function togglePlay() {
  if (playing) { clearInterval(playing); playing = null; return; }
  playing = setInterval(() => {
    if (!timeline) { clearInterval(playing); playing = null; return; }
    if (activeIdx >= timeline.frames.length - 1) { clearInterval(playing); playing = null; return; }
    setIdx(activeIdx + 1);
  }, 350);
}

async function submit(label) {
  if (!timeline) return;
  if (playing) { clearInterval(playing); playing = null; }
  const dec = timeline.decision || {};
  const notes = document.getElementById('notes')?.value || '';
  const res = await fetch('/label/submit', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      candidate_id: timeline.candidate_id,
      decision_id: dec.decision_id,
      decision_action: dec.action,
      label,
      confidence,
      notes,
      labeled_at_offset_sec: (timeline.frames[activeIdx] || {}).offset_sec,
      queue_session_cutoff: sessionCutoff,
      rubric_version: 'decision_moment_v2',
    }),
  });
  if (!res.ok) return;
  labeledCount += 1;
  sessionSkipped.add(queueKey(timeline));
  loadNext();
}

function queueKey(t) {
  const dec = (t && t.decision) || {};
  return dec.decision_id || (t && t.candidate_id) || '';
}

function resetSession() {
  sessionCutoff = new Date().toISOString().replace(/[.][0-9]{3}Z$/, 'Z');
  sessionSkipped.clear();
  timeline = null;
  lastQueue = null;
  loadNext();
}

function shortID(s) {
  s = String(s || '');
  return s.length > 18 ? s.slice(0, 18) : s;
}

function escapeHTML(s) {
  return String(s).replace(/[<>&"]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
}

document.addEventListener('keydown', e => {
  if (!timeline) return;
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 'ArrowRight') { setIdx(activeIdx + 1); e.preventDefault(); }
  else if (e.key === 'ArrowLeft') { setIdx(activeIdx - 1); e.preventDefault(); }
  else if (e.key === ' ') { togglePlay(); e.preventDefault(); }
  else if (e.key === '1') submit('would_help');
  else if (e.key === '2') {
    const action = ((timeline || {}).decision || {}).action;
    submit(action === 'notch_ping' ? 'would_annoy' : 'good_no_ping');
  }
  else if (e.key === '3') submit('cant_tell');
  else if (e.key === 's' || e.key === 'S') loadNext({skipCurrent:true});
});

loadNext();
</script>
</body></html>
"""


EVENT_LABELING_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>harness · event review</title>
<style>
  :root { color-scheme: dark; --bg:#0a0a0c; --panel:#131318; --panel-2:#1a1a20; --border:#25252c; --text:#ececef; --text-2:#a0a0a8; --text-3:#6c6c74; --accent:#d0c08f; --green:#7eb37e; --red:#c4848c; --blue:#7d9ec4; --amber:#c4a87d; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:13px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif; }
  .app { max-width:1180px; margin:0 auto; padding:20px 24px 40px; }
  header { display:flex; align-items:flex-start; justify-content:space-between; gap:18px; padding-bottom:14px; border-bottom:1px solid var(--border); margin-bottom:18px; }
  h1 { margin:0 0 6px; font:650 17px/1.2 -apple-system; }
  .sub { color:var(--text-3); font:12px ui-monospace; }
  .controls { display:flex; gap:8px; align-items:center; }
  select, button, input[type="text"] { background:var(--panel); border:1px solid var(--border); color:var(--text-2); border-radius:7px; padding:8px 10px; font:12px ui-monospace; }
  button { cursor:pointer; }
  button:hover { border-color:var(--accent); color:var(--text); }
  .layout { display:grid; grid-template-columns: minmax(0,1fr) 330px; gap:18px; }
  .list { display:flex; flex-direction:column; gap:12px; }
  .row { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:14px; cursor:pointer; }
  .row.active { border-color:rgba(208,192,143,.65); box-shadow:0 0 0 1px rgba(208,192,143,.15); }
  .top { display:flex; justify-content:space-between; gap:12px; font:12px ui-monospace; color:var(--text-2); }
  .pill { display:inline-block; padding:2px 7px; border-radius:999px; border:1px solid var(--border); color:var(--accent); font:11px ui-monospace; }
  .title { margin-top:8px; font:600 14px/1.35 -apple-system; color:var(--text); }
  .meta { margin-top:6px; color:var(--text-3); font:11.5px ui-monospace; }
  .preview { margin-top:8px; color:var(--text-2); font:12px/1.45 ui-monospace; max-height:76px; overflow:hidden; }
  aside { position:sticky; top:16px; align-self:start; background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:14px; }
  aside h2 { margin:0 0 8px; font:650 13px/1.2 -apple-system; }
  .rubric { color:var(--text-2); font-size:12px; margin-bottom:12px; }
  .buttons { display:grid; gap:8px; margin-top:12px; }
  .label-btn { text-align:left; background:var(--panel-2); border:1px solid var(--border); color:var(--text); border-radius:8px; padding:11px 12px; font:12.5px/1.35 -apple-system; }
  .label-btn.help { border-color:rgba(126,179,126,.45); }
  .label-btn.quiet { border-color:rgba(125,158,196,.45); }
  .label-btn.annoy { border-color:rgba(196,132,140,.45); }
  .label-btn.cant { color:var(--text-2); }
  .small { color:var(--text-3); font:11px ui-monospace; margin-top:3px; }
  .field { margin-top:10px; }
  .field label { display:block; color:var(--text-3); font:11px ui-monospace; margin-bottom:5px; }
  .field input[type="range"] { width:100%; accent-color:var(--accent); }
  .empty { padding:60px; color:var(--text-3); text-align:center; border:1px solid var(--border); border-radius:10px; background:var(--panel); }
  a { color:var(--accent); text-decoration:none; }
</style>
</head><body>
<div class="app">
  <header>
    <div>
      <h1>Event Review</h1>
      <div class="sub">Label whole workflow runs. Judge whether Hermes should have interrupted at least once during the run, not whether a single frame looks interesting.</div>
    </div>
    <div class="controls">
      <select id="window" onchange="loadQueue(true)">
        <option value="24h">24h</option>
        <option value="7d" selected>7d</option>
        <option value="30d">30d</option>
      </select>
      <select id="kind" onchange="loadQueue(true)">
        <option value="all">all</option>
        <option value="missed">missed help</option>
        <option value="hard_negative">hard negatives</option>
        <option value="negative">negative pings</option>
        <option value="positive">positive pings</option>
      </select>
      <button onclick="loadQueue(true)">refresh</button>
      <a href="/label">decision labeler</a>
    </div>
  </header>
  <div class="layout">
    <section class="list" id="list"><div class="empty">loading…</div></section>
    <aside>
      <h2 id="side-title">No event selected</h2>
      <div class="rubric">
        <strong>Should ping</strong>: at least one timely, brief interruption would likely help progress.<br>
        <strong>Should stay quiet</strong>: a ping would mostly cost attention or duplicate obvious next steps.<br>
        <strong>Not now</strong>: useful topic, wrong moment.<br>
        <strong>Can't tell</strong>: event lacks enough context.
      </div>
      <div id="side-meta" class="small">Pick an event from the queue.</div>
      <div class="field">
        <label>confidence <span id="conf-text">0.7</span></label>
        <input id="confidence" type="range" min="0.1" max="1.0" step="0.1" value="0.7" oninput="document.getElementById('conf-text').textContent=this.value">
      </div>
      <div class="field">
        <label>optional note</label>
        <input id="notes" type="text" placeholder="why this event label?">
      </div>
      <div class="buttons">
        <button class="label-btn help" onclick="submitLabel('would_help')">Should ping<div class="small">missed opportunity or useful ping</div></button>
        <button class="label-btn quiet" onclick="submitLabel('good_no_ping')">Should stay quiet<div class="small">silence was correct</div></button>
        <button class="label-btn annoy" onclick="submitLabel('would_annoy')">Would annoy<div class="small">interruption was actively bad</div></button>
        <button class="label-btn cant" onclick="submitLabel('not_now')">Not now<div class="small">maybe useful later, bad timing</div></button>
        <button class="label-btn cant" onclick="submitLabel('cant_tell')">Can't tell<div class="small">not enough context</div></button>
        <button class="label-btn cant" onclick="curate('exclude')">Exclude event<div class="small">remove from training/eval builders</div></button>
      </div>
    </aside>
  </div>
</div>
<script>
let queue = [];
let selected = null;
let labeled = new Set();

async function loadQueue(reset=false) {
  if (reset) { selected = null; }
  const params = new URLSearchParams({
    window: document.getElementById('window').value,
    kind: document.getElementById('kind').value,
    limit: '120',
  });
  const data = await fetch('/label/events/queue?' + params.toString()).then(r => r.json());
  queue = data.examples || [];
  renderList();
  if (!selected && queue.length) select(queue[0].example_id);
}

function renderList() {
  const list = document.getElementById('list');
  const rows = queue.filter(row => !labeled.has(row.workflow_event_id));
  if (!rows.length) {
    list.innerHTML = '<div class="empty">queue empty for this filter</div>';
    return;
  }
  list.innerHTML = rows.map(row => {
    const ctx = row.context || {};
    const joins = row.joins || {};
    return `<div class="row ${selected && selected.example_id === row.example_id ? 'active' : ''}" onclick="select('${escapeAttr(row.example_id)}')">
      <div class="top"><span><span class="pill">${escapeHTML(row.example_type || '?')}</span> target=${escapeHTML(row.target || '?')}</span><span>${escapeHTML((row.ts || '').slice(0,19))}</span></div>
      <div class="title">${escapeHTML(ctx.app || 'unknown')} · ${escapeHTML(ctx.scene || 'unknown')} · ${escapeHTML(ctx.window_title || '(untitled)')}</div>
      <div class="meta">duration=${escapeHTML(num(ctx.duration_sec))}s candidates=${escapeHTML(ctx.n_candidates ?? 0)} decisions=${escapeHTML(joins.n_decisions ?? 0)} pings=${escapeHTML(joins.n_pings ?? 0)} outcomes=${escapeHTML(joins.n_outcomes ?? 0)}</div>
      ${ctx.ocr_snippet ? `<div class="preview">${escapeHTML(ctx.ocr_snippet)}</div>` : ''}
    </div>`;
  }).join('');
}

function select(exampleID) {
  selected = queue.find(row => row.example_id === exampleID) || null;
  renderList();
  renderSide();
}

function renderSide() {
  if (!selected) return;
  const ctx = selected.context || {};
  const joins = selected.joins || {};
  document.getElementById('side-title').textContent = `${ctx.app || 'unknown'} · ${selected.example_type || 'event'}`;
  document.getElementById('side-meta').innerHTML =
    `event=${escapeHTML(selected.workflow_event_id || '?')}<br>` +
    `target suggestion=${escapeHTML(selected.target || '?')} confidence=${escapeHTML(num(selected.confidence))}<br>` +
    `duration=${escapeHTML(num(ctx.duration_sec))}s pings=${escapeHTML(joins.n_pings ?? 0)} outcomes=${escapeHTML(joins.n_outcomes ?? 0)} actions=${escapeHTML(JSON.stringify(joins.user_actions || {}))}<br>` +
    `flags=${escapeHTML((ctx.quality_flags || []).join(', ') || 'none')}`;
}

async function submitLabel(label) {
  if (!selected) return;
  const resp = await fetch('/label/events/submit', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      workflow_event_id: selected.workflow_event_id,
      candidate_id: selected.candidate_id,
      decision_id: selected.decision_id,
      decision_action: selected.policy_action,
      label,
      confidence: parseFloat(document.getElementById('confidence').value || '0.7'),
      notes: document.getElementById('notes').value || '',
      example_type: selected.example_type,
      source: selected.source,
      target_suggestion: selected.target,
      rubric_version: 'workflow_event_v1',
    }),
  });
  if (!resp.ok) return;
  labeled.add(selected.workflow_event_id);
  selected = null;
  renderList();
  const next = queue.find(row => !labeled.has(row.workflow_event_id));
  if (next) select(next.example_id);
}

async function curate(action) {
  if (!selected) return;
  const reason = document.getElementById('notes').value || 'event_review';
  const resp = await fetch('/label/events/curate', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({workflow_event_id: selected.workflow_event_id, action, reason}),
  });
  if (!resp.ok) return;
  labeled.add(selected.workflow_event_id);
  selected = null;
  renderList();
}

function num(v) { return v === null || v === undefined || v === '' ? 'n/a' : Number(v).toFixed(1); }
function escapeHTML(s) { return String(s).replace(/[<>&"]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c])); }
function escapeAttr(s) { return String(s).replace(/['"\\]/g, '_'); }
loadQueue();
</script>
</body></html>
"""


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _ts_iso_to_unix(ts_iso: str) -> Optional[float]:
    try:
        struct = time.strptime(ts_iso, "%Y-%m-%dT%H:%M:%SZ")
        # ISO strings end in Z (UTC) — calendar.timegm is the unambiguous inverse.
        return float(calendar.timegm(struct))
    except Exception:
        return None


def _unix_to_iso(u: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(u))


def _already_labeled() -> tuple[set[str], set[str]]:
    decision_ids: set[str] = set()
    candidate_ids: set[str] = set()
    for row in iter_jsonl("retro_labels.jsonl"):
        did = row.get("decision_id")
        if did:
            decision_ids.add(did)
        cid = row.get("candidate_id")
        if cid:
            candidate_ids.add(cid)
    return decision_ids, candidate_ids


def _is_labeled(row: dict, labeled: tuple[set[str], set[str]]) -> bool:
    decision_ids, candidate_ids = labeled
    did = row.get("decision_id")
    cid = row.get("candidate_id")
    return bool((did and did in decision_ids) or (cid and cid in candidate_ids))


def _labeled_count(labeled: tuple[set[str], set[str]]) -> int:
    # Modern labels carry both ids; older smoke/local rows may only have a
    # candidate id. max() avoids double-counting modern labels.
    return max(len(labeled[0]), len(labeled[1]))


def _count_unlabeled(
    rows: list[dict],
    labeled: tuple[set[str], set[str]],
    *,
    action: str = "all",
    before_ts: str | None = None,
    exclude: set[str] | None = None,
) -> int:
    return len(_eligible_decisions(
        rows,
        labeled,
        action=action,
        before_ts=before_ts,
        exclude=exclude or set(),
        order="newest",
    ))


def _read_memory_snapshot(snapshot_id: Optional[str]) -> Optional[dict]:
    if not snapshot_id:
        return None
    p = HARNESS_DIR / "memory" / "snapshots" / f"{snapshot_id}.json"
    if not p.exists():
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def _candidates_index() -> dict[str, dict]:
    return {r.get("candidate_id"): r for r in tail_jsonl("candidates.jsonl", n=None) if r.get("candidate_id")}


def _trace_for(candidate_id: str, decision_id: Optional[str] = None) -> Optional[dict]:
    for row in iter_jsonl("traces.jsonl"):
        action = row.get("action") or {}
        if decision_id and action.get("decision_id") != decision_id:
            continue
        state = row.get("state") or {}
        cand = state.get("candidate") or {}
        if cand.get("candidate_id") == candidate_id:
            return row
    return None


def _eligible_decisions(
    decisions: list[dict],
    labeled: tuple[set[str], set[str]],
    *,
    action: str = "all",
    before_ts: str | None = None,
    exclude: set[str] | None = None,
    order: str = "newest",
) -> list[dict]:
    exclude = exclude or set()
    out: list[dict] = []
    for d in decisions:
        did = d.get("decision_id")
        cid = d.get("candidate_id")
        if not cid:
            continue
        if action in ("no_ping", "notch_ping") and d.get("action") != action:
            continue
        if before_ts and d.get("ts", "") > before_ts:
            continue
        if (did and did in exclude) or cid in exclude:
            continue
        if _is_labeled(d, labeled):
            continue
        out.append(d)
    reverse = order != "oldest"
    out.sort(key=lambda r: (r.get("ts", ""), r.get("decision_id", "")), reverse=reverse)
    return out


def _parse_exclude(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


# ────────────────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────────────────

async def get_label_page(_: web.Request) -> web.Response:
    return web.Response(text=LABELING_HTML, content_type="text/html")


async def get_event_label_page(_: web.Request) -> web.Response:
    return web.Response(text=EVENT_LABELING_HTML, content_type="text/html")


async def get_label_queue(request: web.Request) -> web.Response:
    action = request.query.get("action") or "all"
    order = request.query.get("order") or "newest"
    before_ts = request.query.get("before_ts") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    exclude = _parse_exclude(request.query.get("exclude"))
    labeled = _already_labeled()
    decisions = tail_jsonl("decisions.jsonl", n=None)
    eligible = _eligible_decisions(
        decisions,
        labeled,
        action=action,
        before_ts=before_ts,
        exclude=exclude,
        order=order,
    )
    if eligible:
        d = eligible[0]
        return web.json_response({
            "candidate_id": d.get("candidate_id"),
            "decision_id": d.get("decision_id"),
            "session_cutoff": before_ts,
            "order": order,
            "action": action,
            "progress": {
                "remaining": len(eligible),
                "labeled": _labeled_count(labeled),
                "skipped": len(exclude),
            },
        })
    return web.json_response(None)


async def get_label_timeline(request: web.Request) -> web.Response:
    """Return frames around the decision moment, plus decision context."""
    candidate_id = request.match_info.get("candidate_id", "")
    if not candidate_id:
        return web.json_response({"error": "missing candidate_id"}, status=400)

    decision_id = request.query.get("decision_id")
    decisions = tail_jsonl("decisions.jsonl", n=None)
    decision = next(
        (
            d for d in reversed(decisions)
            if d.get("candidate_id") == candidate_id
            and (not decision_id or d.get("decision_id") == decision_id)
        ),
        None,
    )
    if not decision:
        return web.json_response({"error": "decision not found"}, status=404)

    cand_idx = _candidates_index()
    candidate = cand_idx.get(candidate_id, {})

    ts_iso = decision.get("ts") or candidate.get("ts", "")
    decision_ts_unix = _ts_iso_to_unix(ts_iso)
    if decision_ts_unix is None:
        return web.json_response({"error": "bad ts"}, status=400)

    # Anchor the timeline on the screen's ACTUAL capture time, not the moment
    # the decision was logged. screen.frame_age_sec is how stale the screen was
    # when the gate fired; subtract to find the real screen ts.
    screen = candidate.get("screen") or {}
    frame_age = float(screen.get("frame_age_sec") or 0.0)
    anchor_unix = decision_ts_unix - frame_age

    since_unix = anchor_unix - WINDOW_BEFORE_SEC
    until_unix = anchor_unix + WINDOW_AFTER_SEC

    fisherman_url = request.app.get("fisherman_url") or "http://localhost:7892"
    fisherman_frames: list[dict] = []

    async def _query(since_u: float, until_u: float) -> list[dict]:
        params = f"since={int(since_u)}&until={int(until_u)}&limit=400"
        try:
            async with ClientSession(timeout=ClientTimeout(total=4)) as s:
                async with s.get(f"{fisherman_url.rstrip('/')}/query?{params}") as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        if isinstance(data, list):
                            return data
        except Exception:
            pass
        return []

    fisherman_frames = await _query(since_unix, until_unix)

    # If empty, the user may have been idle / Fisherman paused. Expand the
    # window progressively up to ±15 min so the labeler still sees something.
    for widen_min in (5, 15):
        if fisherman_frames:
            break
        fisherman_frames = await _query(
            anchor_unix - widen_min * 60,
            anchor_unix + widen_min * 60,
        )

    # Each frame from Fisherman has ts (seconds, float). Sort, dedupe by ts.
    seen: set[int] = set()
    dedup: list[dict] = []
    for fr in fisherman_frames:
        ts = fr.get("ts")
        try:
            ts_ms = int(float(ts) * 1000)
        except Exception:
            continue
        if ts_ms in seen:
            continue
        seen.add(ts_ms)
        dedup.append({
            "ts_ms": ts_ms,
            "ts_unix": float(ts),
            "ts_iso": _unix_to_iso(float(ts)),
            "app": fr.get("app"),
            "window": fr.get("window"),
            "ocr_snippet": (fr.get("ocr_text") or "")[:200],
        })
    dedup.sort(key=lambda f: f["ts_ms"])

    # Find index of decision moment (the closest frame to anchor_unix).
    anchor_ms = int(anchor_unix * 1000)
    decision_idx = -1
    best_diff = None
    for i, fr in enumerate(dedup):
        diff = abs(fr["ts_ms"] - anchor_ms)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            decision_idx = i

    # Always include decision moment; sample down to MAX_TIMELINE_FRAMES
    timeline_frames = _sample_with_anchor(dedup, anchor_idx=decision_idx, target=MAX_TIMELINE_FRAMES)

    # Tag offsets + is_decision flag relative to the anchor frame
    anchor_ts_ms = dedup[decision_idx]["ts_ms"] if decision_idx >= 0 else -1
    for fr in timeline_frames:
        fr["offset_sec"] = round(fr["ts_unix"] - anchor_unix)
        fr["is_decision"] = fr["ts_ms"] == anchor_ts_ms

    # Pull memory snapshot from local trace, if any
    trace = _trace_for(candidate_id, decision.get("decision_id"))
    mem_snap_id = ((trace or {}).get("state") or {}).get("memory_snapshot_id")
    memory = _read_memory_snapshot(mem_snap_id)

    labeled = _already_labeled()
    remaining = _count_unlabeled(decisions, labeled)

    payload = {
        "candidate_id": candidate_id,
        "decision_ts_iso": ts_iso,
        "decision_ts_ms": int(decision_ts_unix * 1000),
        "anchor_ts_iso": _unix_to_iso(anchor_unix),
        "anchor_ts_ms": int(anchor_unix * 1000),
        "frame_age_sec": frame_age,
        "decision": decision,
        "screen": candidate.get("screen") or {},
        "scene": candidate.get("scene") or {},
        "memory": memory,
        "frames": timeline_frames,
        "progress": {"labeled": _labeled_count(labeled), "remaining": remaining},
    }
    return web.json_response(payload)


def _already_labeled_events() -> set[str]:
    out: set[str] = set()
    for row in iter_jsonl("retro_labels.jsonl"):
        event_id = row.get("workflow_event_id")
        if event_id:
            out.add(str(event_id))
    return out


def _matches_event_kind(row: dict, kind: str) -> bool:
    if kind in ("", "all", None):
        return True
    example_type = str(row.get("example_type") or "")
    if kind == "missed":
        return "missed_help" in example_type
    if kind == "hard_negative":
        return "hard_negative" in example_type
    if kind == "negative":
        return row.get("target") == "no_ping" or "negative" in example_type
    if kind == "positive":
        return row.get("target") == "notch_ping" or "positive" in example_type
    return True


async def get_event_label_queue(request: web.Request) -> web.Response:
    from . import dataset as dataset_mod

    window = request.query.get("window") or "7d"
    kind = request.query.get("kind") or "all"
    try:
        limit = int(request.query.get("limit") or 80)
    except ValueError:
        limit = 80
    include_labeled = request.query.get("include_labeled") in {"1", "true", "yes"}
    report = dataset_mod.event_examples(window=window, limit=max(1, min(limit, 300)))
    labeled = _already_labeled_events()
    examples = [
        row for row in report.get("examples", [])
        if _matches_event_kind(row, kind)
        and (include_labeled or str(row.get("workflow_event_id") or "") not in labeled)
    ]
    return web.json_response({
        "window": window,
        "kind": kind,
        "summary": report.get("summary") or {},
        "labeled_events": len(labeled),
        "examples": examples[:limit],
    })


async def post_event_label_submit(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected object"}, status=400)
    workflow_event_id = str(body.get("workflow_event_id") or "").strip()
    if not workflow_event_id:
        return web.json_response({"error": "missing workflow_event_id"}, status=400)
    label = body.get("label")
    if label not in {"would_help", "would_annoy", "good_no_ping", "not_now", "cant_tell"}:
        return web.json_response({"error": "bad label"}, status=400)
    try:
        confidence = float(body.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    confidence = max(0.0, min(1.0, confidence))
    row = {
        "label_id": f"event_{workflow_event_id}_{int(time.time() * 1000)}",
        "label_scope": "workflow_event",
        "workflow_event_id": workflow_event_id,
        "candidate_id": body.get("candidate_id"),
        "decision_id": body.get("decision_id"),
        "decision_action": body.get("decision_action"),
        "label": label,
        "confidence": confidence,
        "intent_category": body.get("intent_category"),
        "example_type": body.get("example_type"),
        "target_suggestion": body.get("target_suggestion"),
        "source": "event_review_ui",
        "example_source": body.get("source"),
        "rubric_version": body.get("rubric_version") or "workflow_event_v1",
        "notes": body.get("notes") or "",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    append_jsonl("retro_labels.jsonl", row)
    return web.json_response({"ok": True, "label": row})


async def post_event_curate(request: web.Request) -> web.Response:
    from . import curation as curation_mod

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected object"}, status=400)
    workflow_event_id = str(body.get("workflow_event_id") or "").strip()
    if not workflow_event_id:
        return web.json_response({"error": "missing workflow_event_id"}, status=400)
    action = str(body.get("action") or "exclude")
    if action not in {"retain", "exclude", "delete", "blur"}:
        return web.json_response({"error": "bad action"}, status=400)
    row = curation_mod.record(
        target_type="workflow_event",
        target_id=workflow_event_id,
        action=action,
        reason=str(body.get("reason") or ""),
        source="event_review_ui",
    )
    return web.json_response({"ok": True, "curation": row})


def _sample_with_anchor(items: list[dict], *, anchor_idx: int, target: int) -> list[dict]:
    n = len(items)
    if n <= target:
        return items[:]
    # We want roughly target items, evenly spaced, but the anchor must be present.
    step = max(1, n // target)
    out_idxs: set[int] = set(range(0, n, step))
    if anchor_idx >= 0:
        out_idxs.add(anchor_idx)
    # Slightly more than target is fine; trim to target while keeping anchor.
    sorted_idxs = sorted(out_idxs)
    if len(sorted_idxs) > target:
        # Drop indices farthest from anchor until we hit target
        sorted_idxs.sort(key=lambda i: abs(i - anchor_idx))
        sorted_idxs = sorted(sorted_idxs[:target])
    return [items[i] for i in sorted_idxs]


async def get_label_frame(request: web.Request) -> web.Response:
    ts_ms = request.match_info.get("ts_ms", "")
    if not ts_ms.isdigit():
        return web.Response(status=400)
    fisherman_url = request.app.get("fisherman_url") or "http://localhost:7892"
    url = f"{fisherman_url.rstrip('/')}/frames/{ts_ms}/image"
    try:
        async with ClientSession(timeout=ClientTimeout(total=4)) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return web.Response(status=r.status)
                body = await r.read()
                return web.Response(
                    body=body,
                    content_type=r.headers.get("Content-Type", "image/jpeg"),
                    headers={"Cache-Control": "public, max-age=3600"},
                )
    except Exception:
        return web.Response(status=502)


async def post_label_submit(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    cid = body.get("candidate_id")
    if not cid:
        return web.json_response({"error": "missing candidate_id"}, status=400)
    label = body.get("label")
    if label not in {"would_help", "would_annoy", "good_no_ping", "cant_tell"}:
        return web.json_response({"error": "bad label"}, status=400)
    decision_id = body.get("decision_id")
    if _is_labeled({"candidate_id": cid, "decision_id": decision_id}, _already_labeled()):
        return web.json_response({"ok": True, "duplicate": True})
    try:
        confidence = float(body.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    confidence = max(0.0, min(1.0, confidence))
    row = {
        "candidate_id": cid,
        "decision_id": decision_id,
        "decision_action": body.get("decision_action"),
        "label": label,
        "confidence": confidence,
        "labeled_at_offset_sec": body.get("labeled_at_offset_sec"),
        "queue_session_cutoff": body.get("queue_session_cutoff"),
        "rubric_version": body.get("rubric_version") or "decision_moment_v2",
        "notes": body.get("notes") or "",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    append_jsonl("retro_labels.jsonl", row)
    return web.json_response({"ok": True})


def attach_routes(app: web.Application, fisherman_url: str) -> None:
    app["fisherman_url"] = fisherman_url
    app.router.add_get("/label", get_label_page)
    app.router.add_get("/label/events", get_event_label_page)
    app.router.add_get("/label/queue", get_label_queue)
    app.router.add_get("/label/events/queue", get_event_label_queue)
    app.router.add_get("/label/timeline/{candidate_id}", get_label_timeline)
    app.router.add_get("/label/frame/{ts_ms}", get_label_frame)
    app.router.add_post("/label/submit", post_label_submit)
    app.router.add_post("/label/events/submit", post_event_label_submit)
    app.router.add_post("/label/events/curate", post_event_curate)
