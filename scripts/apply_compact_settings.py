from __future__ import annotations

from pathlib import Path

MARKER = 'data-compact-settings="v1"'

COMPACT_CSS = r'''

    /* compact-settings-v1 */
    body.control-view .main-grid {
      grid-template-columns: minmax(0, 1.55fr) minmax(340px, .75fr);
      align-items: start;
    }

    body.control-view .control-col {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      align-items: start;
    }

    body.control-view .control-col > .panel:first-child {
      grid-column: 1 / -1;
    }

    body.control-view .control-col > .panel:not(:first-child) {
      min-width: 0;
    }

    .compact-settings-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0 12px;
      align-items: start;
    }

    .compact-settings-grid > .settings-full-span,
    .compact-settings-grid > .split,
    .compact-settings-grid > .mode-switch,
    .compact-settings-grid > #sizingPreviewPanel {
      grid-column: 1 / -1;
    }

    .compact-settings-grid .field {
      margin-bottom: 10px;
    }

    .compact-settings-grid input,
    .compact-settings-grid select,
    .compact-settings-grid .static-field {
      min-height: 40px;
    }

    .compact-settings-grid .auto-state {
      margin: 8px 0 10px;
    }

    .compact-settings-grid .btn-row {
      margin-top: 0;
    }

    .compact-settings-grid .notice {
      margin-top: 10px;
    }

    .compact-settings-grid .engine-grid {
      grid-template-columns: repeat(5, minmax(0, 1fr));
    }

    .compact-settings-grid button:disabled {
      cursor: not-allowed;
      opacity: .48;
      transform: none;
    }

    body.control-view .right-col {
      position: sticky;
      top: 14px;
      align-self: start;
    }

    body.control-view .right-col .execution-panel {
      min-height: 0;
    }

    body.control-view .right-col .timeline {
      max-height: 300px;
    }

    .settings-section-label {
      grid-column: 1 / -1;
      margin: 4px 0 10px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: rgba(84, 163, 255, .06);
      color: var(--muted);
      font-size: .74rem;
      font-weight: 800;
      letter-spacing: .08em;
      text-transform: uppercase;
    }

    @media (max-width: 1280px) {
      body.control-view .main-grid {
        grid-template-columns: minmax(0, 1fr);
      }

      body.control-view .right-col {
        position: static;
        grid-column: 1 / -1;
      }
    }

    @media (max-width: 760px) {
      body.control-view .control-col,
      .compact-settings-grid {
        grid-template-columns: minmax(0, 1fr);
      }

      body.control-view .control-col > .panel:first-child,
      .compact-settings-grid > .settings-full-span,
      .compact-settings-grid > .split,
      .compact-settings-grid > .mode-switch,
      .compact-settings-grid > #sizingPreviewPanel,
      .settings-section-label {
        grid-column: 1;
      }

      .compact-settings-grid .engine-grid {
        grid-template-columns: minmax(0, 1fr);
      }
    }
'''

COMPACT_JS = r'''

    // compact-settings-v1
    (() => {
      const controlsPanel = document.getElementById("adminToken")?.closest(".panel");
      const controlsBody = controlsPanel?.querySelector(".panel-body");
      if (!controlsBody || controlsBody.dataset.compactSettings === "v1") return;

      controlsBody.dataset.compactSettings = "v1";
      controlsBody.classList.add("compact-settings-grid");

      const makeFullSpan = (element) => {
        if (!element) return;
        const target = element.closest(".field, .auto-state, .btn-row, .notice, .engine-grid, .risk-meter, .static-field") || element;
        target.classList.add("settings-full-span");
      };

      [
        "adminToken",
        "routerMode",
        "autoStatus",
        "autoStartBtn",
        "autoStopBtn",
        "startBtn",
        "manageStopsBtn",
        "sizingPreviewPanel",
        "killBtn",
        "controlNotice",
        "engineGrid"
      ].forEach((id) => makeFullSpan(document.getElementById(id)));

      const sectionBefore = (anchorId, text) => {
        const anchor = document.getElementById(anchorId)?.closest(".field, .auto-state, .btn-row, .risk-meter, .split");
        if (!anchor || anchor.previousElementSibling?.classList.contains("settings-section-label")) return;
        const label = document.createElement("div");
        label.className = "settings-section-label";
        label.textContent = text;
        anchor.parentElement?.insertBefore(label, anchor);
      };

      sectionBefore("adminToken", "Connection & test controls");
      sectionBefore("allocation", "Trading rules");
      sectionBefore("breakevenTrigger", "Position management");
      sectionBefore("routerMode", "Runtime & engine status");

      const syncRuntimeButtons = () => {
        const statusElement = document.getElementById("autoStatus");
        const running = String(statusElement?.textContent || "").toLowerCase().includes("running");
        const startButton = document.getElementById("autoStartBtn");
        const stopButton = document.getElementById("autoStopBtn");
        if (startButton) {
          startButton.disabled = running;
          startButton.setAttribute("aria-disabled", running ? "true" : "false");
        }
        if (stopButton) {
          stopButton.disabled = !running;
          stopButton.setAttribute("aria-disabled", !running ? "true" : "false");
        }
      };

      syncRuntimeButtons();
      setInterval(syncRuntimeButtons, 1000);
    })();
'''


def patch_html(html: str) -> str:
    if MARKER in html:
        return html
    if 'data-sidebar-shell="v1"' not in html:
        raise ValueError("Compact settings requires sidebar UI to be applied first")
    if "</style>" not in html or "  </script>" not in html:
        raise ValueError("Cannot apply compact settings; required HTML anchors are missing")

    patched = html.replace("</style>", f"{COMPACT_CSS}\n  </style>", 1)
    script_close = patched.rfind("  </script>")
    patched = patched[:script_close] + COMPACT_JS + "\n" + patched[script_close:]
    patched = patched.replace(
        '<main class="shell" id="appContent">',
        '<main class="shell" id="appContent" data-compact-settings="v1">',
        1,
    )
    if patched.count(MARKER) != 1:
        raise ValueError("Compact settings marker validation failed")
    return patched


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    index_path = project_root / "frontend" / "index.html"
    original = index_path.read_text(encoding="utf-8")
    patched = patch_html(original)
    if patched == original:
        print("Compact settings UI already applied.")
        return
    index_path.write_text(patched, encoding="utf-8")
    print(f"Applied compact settings UI to {index_path}.")


if __name__ == "__main__":
    main()
