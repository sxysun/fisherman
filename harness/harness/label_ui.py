"""Rewind-style retro labeling UI. Served by the harness daemon on :7893.

Endpoints:
  GET  /label                                 → HTML page (scrubber UI)
  GET  /label/queue                           → next unlabeled decision (JSON)
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
    padding: 14px 24px;
    display: flex; align-items: baseline; justify-content: space-between;
    border-bottom: 1px solid var(--border);
  }
  header h1 {
    margin: 0; font: 600 12px/1 -apple-system;
    text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-2);
  }
  header .progress { color: var(--text-3); font: 11px ui-monospace; }
  header .keys { color: var(--text-3); font: 11px ui-monospace; }
  header .keys kbd {
    display: inline-block; padding: 1px 6px; margin: 0 2px;
    background: var(--panel-2); border: 1px solid var(--border-2);
    border-radius: 4px; font: 10px ui-monospace; color: var(--text-2);
  }

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

async function loadNext() {
  setLoading();
  const q = await fetch('/label/queue').then(r => r.json());
  if (!q) { renderEmpty(); return; }

  const t = await fetch(`/label/timeline/${q.candidate_id}`).then(r => r.json());
  if (!t) { renderEmpty(); return; }
  timeline = t;
  decisionIdx = (t.frames || []).findIndex(f => f.is_decision);
  if (decisionIdx < 0) decisionIdx = Math.floor((t.frames.length - 1) / 2);
  activeIdx = decisionIdx;
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
      <div>no unlabeled decisions left. labeled this session: ${labeledCount}</div>
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
  const remaining = (t.progress && t.progress.remaining) || 0;

  document.getElementById('app').innerHTML = `
    <header>
      <h1>retro label · ${t.candidate_id || ''}</h1>
      <div class="progress">${labeledCount} labeled · ${remaining} remaining</div>
      <div class="keys">
        <kbd>←</kbd><kbd>→</kbd> scrub
        <kbd>space</kbd> play
        <kbd>1</kbd><kbd>2</kbd><kbd>3</kbd><kbd>4</kbd> label
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
          <div class="ocr">${escapeHTML((t.screen || {}).ocr_snippet || '(no ocr)')}</div>
        </div>
        ${t.memory ? `
        <div class="meta-card">
          <h2>memory snapshot</h2>
          <div class="meta-row"><span class="k">app switches 15m</span><span class="v">${t.memory.app_switches_last_15m}</span></div>
          <div class="meta-row"><span class="k">mins on current</span><span class="v">${t.memory.minutes_on_current_app}</span></div>
          <div class="meta-row"><span class="k">recent apps</span><span class="v">${(t.memory.recent_apps || []).slice(-5).join(', ') || '—'}</span></div>
        </div>` : ''}
      </aside>
    </main>
    <footer>
      <div class="label-row">
        <button class="label-btn help"  onclick="submit('would_help')">
          <span class="lbl-body">
            <span class="lbl-title">✓ Would have helped</span>
            <span class="lbl-hint">a ping here would be welcome</span>
          </span>
          <span class="lbl-key">1</span>
        </button>
        <button class="label-btn annoy" onclick="submit('would_annoy')">
          <span class="lbl-body">
            <span class="lbl-title">✕ Would have annoyed</span>
            <span class="lbl-hint">unwelcome interruption</span>
          </span>
          <span class="lbl-key">2</span>
        </button>
        <button class="label-btn good"  onclick="submit('good_no_ping')">
          <span class="lbl-body">
            <span class="lbl-title">· Good silence</span>
            <span class="lbl-hint">no ping was right</span>
          </span>
          <span class="lbl-key">3</span>
        </button>
        <button class="label-btn cant"  onclick="submit('cant_tell')">
          <span class="lbl-body">
            <span class="lbl-title">? Can't tell</span>
            <span class="lbl-hint">not enough context</span>
          </span>
          <span class="lbl-key">4</span>
        </button>
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

function pctForIdx(idx) {
  if (!timeline || !timeline.frames || timeline.frames.length < 2) return 50;
  return (idx / (timeline.frames.length - 1)) * 100;
}

function setIdx(i) {
  if (!timeline) return;
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
  await fetch('/label/submit', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      candidate_id: timeline.candidate_id,
      decision_id: dec.decision_id,
      label,
      confidence,
      notes,
      labeled_at_offset_sec: (timeline.frames[activeIdx] || {}).offset_sec,
    }),
  });
  labeledCount += 1;
  loadNext();
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
  else if (e.key === '2') submit('would_annoy');
  else if (e.key === '3') submit('good_no_ping');
  else if (e.key === '4') submit('cant_tell');
  else if (e.key === 's' || e.key === 'S') loadNext();
});

loadNext();
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


def _already_labeled() -> set[str]:
    out: set[str] = set()
    for row in iter_jsonl("retro_labels.jsonl"):
        cid = row.get("candidate_id")
        if cid:
            out.add(cid)
    return out


def _count_unlabeled(rows: list[dict], labeled: set[str]) -> int:
    return sum(1 for r in rows if r.get("candidate_id") and r["candidate_id"] not in labeled)


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


def _trace_for(candidate_id: str) -> Optional[dict]:
    for row in iter_jsonl("traces.jsonl"):
        state = row.get("state") or {}
        cand = state.get("candidate") or {}
        if cand.get("candidate_id") == candidate_id:
            return row
    return None


# ────────────────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────────────────

async def get_label_page(_: web.Request) -> web.Response:
    return web.Response(text=LABELING_HTML, content_type="text/html")


async def get_label_queue(request: web.Request) -> web.Response:
    only_action = request.query.get("action")
    labeled = _already_labeled()
    decisions = tail_jsonl("decisions.jsonl", n=None)
    for action_pref in ([only_action] if only_action else ["no_ping", "notch_ping"]):
        for d in reversed(decisions):
            if d.get("action") != action_pref:
                continue
            cid = d.get("candidate_id")
            if not cid or cid in labeled:
                continue
            return web.json_response({"candidate_id": cid, "decision_id": d.get("decision_id")})
    return web.json_response(None)


async def get_label_timeline(request: web.Request) -> web.Response:
    """Return frames around the decision moment, plus decision context."""
    candidate_id = request.match_info.get("candidate_id", "")
    if not candidate_id:
        return web.json_response({"error": "missing candidate_id"}, status=400)

    decisions = tail_jsonl("decisions.jsonl", n=None)
    decision = next((d for d in reversed(decisions) if d.get("candidate_id") == candidate_id), None)
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
    trace = _trace_for(candidate_id)
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
        "progress": {"labeled": len(labeled), "remaining": remaining},
    }
    return web.json_response(payload)


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
    row = {
        "candidate_id": cid,
        "decision_id": body.get("decision_id"),
        "label": body.get("label"),
        "confidence": float(body.get("confidence", 1.0)),
        "labeled_at_offset_sec": body.get("labeled_at_offset_sec"),
        "notes": body.get("notes") or "",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    append_jsonl("retro_labels.jsonl", row)
    return web.json_response({"ok": True})


def attach_routes(app: web.Application, fisherman_url: str) -> None:
    app["fisherman_url"] = fisherman_url
    app.router.add_get("/label", get_label_page)
    app.router.add_get("/label/queue", get_label_queue)
    app.router.add_get("/label/timeline/{candidate_id}", get_label_timeline)
    app.router.add_get("/label/frame/{ts_ms}", get_label_frame)
    app.router.add_post("/label/submit", post_label_submit)
