"""Runtime-only confirmed UI fixes for the canonical Render entrypoint.

Python imports ``sitecustomize`` automatically when this directory is on
``sys.path``. The patch is limited to the served frontend document and
approved in-memory defaults.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_ORIGINAL_READ_TEXT = Path.read_text
_PATCH_MARKER = "serial-9-ui-confirmed-fixes"

_CONFIRMED_STYLE = """
<style id="serial-9-ui-confirmed-fixes">
  body.control-view .control-col .panel-body {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px 12px;
    align-items: start;
  }
  body.control-view .control-col .panel-body > .mode-switch,
  body.control-view .control-col .panel-body > .split,
  body.control-view .control-col .panel-body > .auto-state,
  body.control-view .control-col .panel-body > .btn-row,
  body.control-view .control-col .panel-body > #sizingPreviewPanel,
  body.control-view .control-col .panel-body > .danger,
  body.control-view .control-col .panel-body > .notice,
  body.control-view .control-col .panel-body > .field:has(#routerMode) {
    grid-column: 1 / -1;
  }
  body.control-view .control-col .field { margin-bottom: 0; }
  body.control-view .control-col .panel-body,
  body.control-view .right-col .panel-body { padding: 12px; }
  body.control-view .control-col .auto-state,
  body.control-view #sizingPreviewPanel { margin-top: 0 !important; }
  .runtime-truth-strip {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
    margin-top: 12px;
  }
  .runtime-truth-card {
    border: 1px solid var(--line);
    border-radius: 8px;
    background: rgba(12, 20, 32, .92);
    padding: 10px 12px;
    min-width: 0;
  }
  .runtime-truth-card span {
    display: block;
    color: var(--muted);
    font-size: .76rem;
    margin-bottom: 4px;
  }
  .runtime-truth-card strong {
    display: block;
    color: var(--text);
    font-size: .88rem;
    overflow-wrap: anywhere;
  }
  .runtime-truth-card[data-state="ok"] strong { color: var(--green); }
  .runtime-truth-card[data-state="warning"] strong { color: var(--yellow); }
  .runtime-truth-card[data-state="error"] strong { color: var(--red); }
  @media (max-width: 900px) {
    body.control-view .control-col .panel-body { grid-template-columns: 1fr; }
    body.control-view .control-col .panel-body > * { grid-column: 1 !important; }
    .runtime-truth-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (max-width: 520px) {
    .runtime-truth-strip { grid-template-columns: 1fr; }
  }
</style>
"""

_CONFIRMED_SCRIPT = """
<script id="serial-9-ui-confirmed-fixes-script">
(() => {
  const text = (id, value) => {
    const node = document.getElementById(id);
    if (node) node.textContent = value;
  };
  const card = (id, value, state) => {
    const node = document.getElementById(id);
    if (!node) return;
    node.dataset.state = state;
    const target = node.querySelector('strong');
    if (target) target.textContent = value;
  };
  const token = () => {
    const field = document.getElementById('adminToken');
    return String((field && field.value) || localStorage.getItem('adminToken') || '').trim();
  };
  const headers = () => token() ? { Authorization: `Bearer ${token()}` } : {};

  const installTruthStrip = () => {
    if (document.getElementById('runtimeTruthStrip')) return;
    const tabs = document.querySelector('.page-tabs');
    if (!tabs) return;
    const strip = document.createElement('section');
    strip.id = 'runtimeTruthStrip';
    strip.className = 'runtime-truth-strip';
    strip.setAttribute('aria-label', 'Confirmed runtime truth');
    strip.innerHTML = `
      <div class="runtime-truth-card" id="truthScanner" data-state="ok"><span>Scanner universe</span><strong>Liquid Intraday Top Movers</strong></div>
      <div class="runtime-truth-card" id="truthLimits" data-state="ok"><span>Bounded scan</span><strong>20 shortlist → 10 deep scan</strong></div>
      <div class="runtime-truth-card" id="truthReplay" data-state="ok"><span>Replay method</span><strong>Closed candles · next-open entry</strong></div>
      <div class="runtime-truth-card" id="truthDurable" data-state="warning"><span>Durable state</span><strong>Checking authenticated status…</strong></div>`;
    tabs.insertAdjacentElement('afterend', strip);
  };

  const applyConfirmedLabels = () => {
    text('symbolSourceText', 'Liquid Intraday Top Movers');
    text('symbolUniverseText', '20-symbol shortlist · 10-symbol deep scan · 10 min refresh');
    const takeProfit = document.getElementById('take');
    if (takeProfit && Number(takeProfit.value) <= 1.6) takeProfit.value = '2.0';
    const maxOpen = document.getElementById('maxOpenPositions');
    if (maxOpen && String(maxOpen.value) === '1') maxOpen.value = '3';
  };

  const refreshDurableTruth = async () => {
    installTruthStrip();
    if (!token()) {
      card('truthDurable', 'Admin token required to verify', 'warning');
      return;
    }
    try {
      const response = await fetch('/api/durable-state/status', { headers: headers(), cache: 'no-store' });
      if (response.status === 401 || response.status === 403) {
        card('truthDurable', 'Authentication failed', 'error');
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const degraded = Boolean(data.degraded) || data.persistentConfigured === false;
      card(
        'truthDurable',
        degraded ? 'Degraded · persistent disk not configured' : 'Persistent SQLite ready',
        degraded ? 'warning' : 'ok'
      );
    } catch (error) {
      card('truthDurable', `Status unavailable · ${error.message}`, 'error');
    }
  };

  document.addEventListener('DOMContentLoaded', () => {
    installTruthStrip();
    applyConfirmedLabels();
    refreshDurableTruth();
    const field = document.getElementById('adminToken');
    if (field) field.addEventListener('change', refreshDurableTruth);
  });
  window.setInterval(applyConfirmedLabels, 1500);
  window.setInterval(refreshDurableTruth, 30000);
})();
</script>
"""


def patch_frontend_html(text: str) -> str:
    """Return the confirmed UI document exactly once."""
    if _PATCH_MARKER in text:
        return text
    replacements = {
        'id="maxOpenPositions" type="number" value="1"': 'id="maxOpenPositions" type="number" value="3"',
        'id="take" type="number" value="1.6"': 'id="take" type="number" value="2.0"',
        '<strong id="symbolSourceText">Top gainers auto</strong>': '<strong id="symbolSourceText">Liquid Intraday Top Movers</strong>',
        '<span id="symbolUniverseText">Refreshes every 10 minutes</span>': '<span id="symbolUniverseText">20-symbol shortlist · 10-symbol deep scan · 10 min refresh</span>',
        'top-gainer scan': 'liquid intraday top-movers scan',
        'Top Gainer': 'Liquid Top Mover',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    if "</head>" in text:
        text = text.replace("</head>", f"{_CONFIRMED_STYLE}</head>", 1)
    if "</body>" in text:
        text = text.replace("</body>", f"{_CONFIRMED_SCRIPT}</body>", 1)
    return text


def _patched_read_text(path: Path, *args, **kwargs) -> str:
    text = _ORIGINAL_READ_TEXT(path, *args, **kwargs)
    if path.name == "index.html" and path.parent.name == "frontend":
        return patch_frontend_html(text)
    return text


def _set_runtime_default() -> None:
    for _ in range(400):
        for module_name in ("server", "backend.server"):
            module = sys.modules.get(module_name)
            state = getattr(module, "BOT_STATE", None) if module else None
            if isinstance(state, dict):
                lock = getattr(module, "BOT_LOCK", None)

                def apply_defaults() -> None:
                    try:
                        current_open = int(state.get("maxOpenPositions") or 1)
                    except (TypeError, ValueError):
                        current_open = 1
                    if current_open == 1:
                        state["maxOpenPositions"] = 3
                    try:
                        current_take = float(state.get("takeProfitPct") or 0)
                    except (TypeError, ValueError):
                        current_take = 0
                    if current_take <= 1.6:
                        state["takeProfitPct"] = 2.0

                if lock is None:
                    apply_defaults()
                else:
                    with lock:
                        apply_defaults()
                return
        time.sleep(0.025)


Path.read_text = _patched_read_text
threading.Thread(target=_set_runtime_default, daemon=True).start()
