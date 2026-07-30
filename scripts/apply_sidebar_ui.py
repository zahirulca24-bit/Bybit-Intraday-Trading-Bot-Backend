from __future__ import annotations

import re
from pathlib import Path

MARKER = 'data-sidebar-shell="v1"'

SIDEBAR_CSS = r'''

    /* sidebar-ui-v1 */
    .app-layout {
      min-height: 100vh;
      display: grid;
      grid-template-columns: 252px minmax(0, 1fr);
      transition: grid-template-columns .2s ease;
    }

    .app-sidebar {
      position: sticky;
      top: 0;
      z-index: 40;
      height: 100vh;
      display: flex;
      flex-direction: column;
      gap: 18px;
      padding: 18px 14px;
      overflow-y: auto;
      border-right: 1px solid var(--line);
      background:
        radial-gradient(circle at 20% 0%, rgba(84, 163, 255, .14), transparent 18rem),
        linear-gradient(180deg, rgba(14, 25, 40, .98), rgba(7, 12, 20, .98));
      box-shadow: 14px 0 38px rgba(0, 0, 0, .24);
    }

    .sidebar-brand {
      display: flex;
      align-items: center;
      gap: 11px;
      min-height: 48px;
      padding: 0 4px;
    }

    .sidebar-brand .mark {
      width: 40px;
      height: 40px;
      border-radius: 10px;
    }

    .sidebar-brand-copy {
      min-width: 0;
    }

    .sidebar-brand-copy strong,
    .sidebar-brand-copy span {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .sidebar-brand-copy strong {
      font-size: .98rem;
    }

    .sidebar-brand-copy span {
      margin-top: 3px;
      color: var(--muted);
      font-size: .72rem;
    }

    .sidebar-section-label {
      margin: 0 10px -8px;
      color: #718197;
      font-size: .68rem;
      font-weight: 800;
      letter-spacing: .12em;
      text-transform: uppercase;
    }

    .page-tabs.sidebar-nav {
      display: grid;
      gap: 6px;
      margin: 0;
    }

    .sidebar-nav .page-tab {
      width: 100%;
      min-height: 44px;
      display: flex;
      align-items: center;
      gap: 11px;
      padding: 9px 11px;
      border-radius: 9px;
      text-align: left;
      white-space: nowrap;
    }

    .sidebar-nav .page-tab:hover {
      color: var(--text);
      border-color: #3a4a60;
      background: rgba(84, 163, 255, .08);
    }

    .sidebar-nav .page-tab[aria-pressed="true"] {
      background: linear-gradient(135deg, rgba(39, 209, 127, .18), rgba(30, 142, 126, .11));
      box-shadow: inset 3px 0 0 var(--green);
    }

    .sidebar-icon {
      width: 28px;
      height: 28px;
      flex: 0 0 auto;
      display: grid;
      place-items: center;
      border: 1px solid rgba(84, 163, 255, .22);
      border-radius: 8px;
      background: rgba(84, 163, 255, .09);
      color: var(--cyan);
      font-size: .67rem;
      font-weight: 900;
      letter-spacing: .02em;
    }

    .sidebar-nav .page-tab[aria-pressed="true"] .sidebar-icon {
      border-color: rgba(39, 209, 127, .35);
      background: rgba(39, 209, 127, .12);
      color: var(--green);
    }

    .sidebar-footer {
      display: grid;
      gap: 10px;
      margin-top: auto;
      padding-top: 14px;
      border-top: 1px solid var(--line);
    }

    .sidebar-runtime {
      display: grid;
      gap: 8px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: rgba(8, 14, 23, .72);
    }

    .sidebar-runtime-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      color: var(--muted);
      font-size: .75rem;
    }

    .sidebar-runtime-row strong {
      max-width: 128px;
      overflow: hidden;
      color: var(--text);
      font-size: .75rem;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .sidebar-runtime-row strong[data-state="running"],
    .sidebar-runtime-row strong[data-state="connected"] {
      color: var(--green);
    }

    .sidebar-runtime-row strong[data-state="stopped"],
    .sidebar-runtime-row strong[data-state="error"] {
      color: var(--red);
    }

    .sidebar-action {
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(21, 27, 36, .86);
      color: var(--muted);
      font-weight: 700;
    }

    .sidebar-action:hover {
      color: var(--text);
      border-color: #3a4a60;
    }

    .sidebar-action.danger {
      border-color: rgba(255, 91, 110, .35);
      background: rgba(255, 91, 110, .1);
      color: var(--red);
    }

    .sidebar-collapse {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
    }

    .sidebar-mobile-toggle {
      display: none;
      position: fixed;
      top: 16px;
      left: 16px;
      z-index: 70;
      width: 42px;
      height: 42px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(12, 20, 32, .96);
      box-shadow: var(--shadow);
      color: var(--text);
      font-size: 1.2rem;
    }

    .sidebar-scrim {
      display: none;
    }

    body.sidebar-collapsed .app-layout {
      grid-template-columns: 82px minmax(0, 1fr);
    }

    body.sidebar-collapsed .app-sidebar {
      padding-inline: 12px;
    }

    body.sidebar-collapsed .sidebar-brand {
      justify-content: center;
    }

    body.sidebar-collapsed .sidebar-brand-copy,
    body.sidebar-collapsed .sidebar-section-label,
    body.sidebar-collapsed .sidebar-label,
    body.sidebar-collapsed .sidebar-runtime,
    body.sidebar-collapsed .sidebar-collapse-label {
      display: none;
    }

    body.sidebar-collapsed .sidebar-nav .page-tab {
      justify-content: center;
      padding-inline: 8px;
    }

    body.sidebar-collapsed .sidebar-action {
      font-size: 0;
    }

    body.sidebar-collapsed .sidebar-action::after {
      content: attr(data-short-label);
      font-size: .72rem;
    }

    @media (max-width: 980px) {
      .app-layout,
      body.sidebar-collapsed .app-layout {
        grid-template-columns: minmax(0, 1fr);
      }

      .app-sidebar {
        position: fixed;
        left: 0;
        top: 0;
        width: min(286px, 86vw);
        transform: translateX(-105%);
        transition: transform .2s ease;
      }

      body.sidebar-open .app-sidebar {
        transform: translateX(0);
      }

      .sidebar-mobile-toggle {
        display: grid;
        place-items: center;
      }

      .sidebar-scrim {
        position: fixed;
        inset: 0;
        z-index: 35;
        background: rgba(0, 0, 0, .58);
        backdrop-filter: blur(2px);
      }

      body.sidebar-open .sidebar-scrim {
        display: block;
      }

      .shell {
        padding-top: 70px;
      }

      .sidebar-collapse {
        display: none;
      }
    }
'''

SIDEBAR_HTML = r'''
  <button class="sidebar-mobile-toggle" id="sidebarMobileToggle" type="button" aria-controls="appSidebar" aria-expanded="false" aria-label="Open navigation">☰</button>
  <div class="app-layout" data-sidebar-shell="v1">
    <aside class="app-sidebar" id="appSidebar" aria-label="Primary navigation">
      <div class="sidebar-brand">
        <div class="mark" aria-hidden="true">B</div>
        <div class="sidebar-brand-copy">
          <strong>Bybit Demo Bot</strong>
          <span>Intraday control center</span>
        </div>
      </div>

      <div class="sidebar-section-label">Workspace</div>
      <nav class="page-tabs sidebar-nav" aria-label="Pages">
        <button class="page-tab" type="button" data-page="dashboard" id="dashboardTab" aria-pressed="true"><span class="sidebar-icon">DB</span><span class="sidebar-label">Dashboard</span></button>
        <button class="page-tab" type="button" data-page="scanner" id="scannerTab" aria-pressed="false"><span class="sidebar-icon">SC</span><span class="sidebar-label">Scanner &amp; Signals</span></button>
        <button class="page-tab" type="button" data-page="trades" id="tradesTab" aria-pressed="false"><span class="sidebar-icon">TR</span><span class="sidebar-label">Trades &amp; Journal</span></button>
        <button class="page-tab" type="button" data-page="analytics" id="analyticsTab" aria-pressed="false"><span class="sidebar-icon">AN</span><span class="sidebar-label">Strategy Analytics</span></button>
        <button class="page-tab" type="button" data-page="backtest" id="backtestTab" aria-pressed="false"><span class="sidebar-icon">HR</span><span class="sidebar-label">Historical Replay</span></button>
        <button class="page-tab" type="button" data-page="settings" id="controlTab" aria-pressed="false"><span class="sidebar-icon">ST</span><span class="sidebar-label">Settings &amp; Health</span></button>
      </nav>

      <div class="sidebar-footer">
        <div class="sidebar-runtime" aria-label="Runtime status">
          <div class="sidebar-runtime-row"><span>Exchange</span><strong data-state="connected">Bybit Demo</strong></div>
          <div class="sidebar-runtime-row"><span>Backend</span><strong id="sidebarBackendStatus">Checking</strong></div>
          <div class="sidebar-runtime-row"><span>Engine</span><strong id="sidebarEngineStatus">Checking</strong></div>
        </div>
        <button class="sidebar-action danger" id="sidebarKillSwitch" type="button" data-short-label="KILL">Kill Switch</button>
        <button class="sidebar-action sidebar-collapse" id="sidebarToggle" type="button" data-short-label="»" aria-label="Collapse sidebar"><span aria-hidden="true">«</span><span class="sidebar-collapse-label">Collapse sidebar</span></button>
      </div>
    </aside>
    <div class="sidebar-scrim" id="sidebarScrim" aria-hidden="true"></div>
    <main class="shell" id="appContent">
'''

SIDEBAR_JS = r'''

    // sidebar-ui-v1
    (() => {
      const sidebarToggle = document.getElementById("sidebarToggle");
      const mobileToggle = document.getElementById("sidebarMobileToggle");
      const sidebarScrim = document.getElementById("sidebarScrim");
      const sidebarKillSwitch = document.getElementById("sidebarKillSwitch");
      const sidebarBackendStatus = document.getElementById("sidebarBackendStatus");
      const sidebarEngineStatus = document.getElementById("sidebarEngineStatus");

      const setMobileOpen = (open) => {
        document.body.classList.toggle("sidebar-open", open);
        if (mobileToggle) mobileToggle.setAttribute("aria-expanded", open ? "true" : "false");
      };

      if (localStorage.getItem("bybitSidebarCollapsed") === "1" && window.innerWidth > 980) {
        document.body.classList.add("sidebar-collapsed");
      }

      sidebarToggle?.addEventListener("click", () => {
        const collapsed = document.body.classList.toggle("sidebar-collapsed");
        localStorage.setItem("bybitSidebarCollapsed", collapsed ? "1" : "0");
        sidebarToggle.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
      });

      mobileToggle?.addEventListener("click", () => setMobileOpen(!document.body.classList.contains("sidebar-open")));
      sidebarScrim?.addEventListener("click", () => setMobileOpen(false));
      document.querySelectorAll(".sidebar-nav .page-tab").forEach((item) => {
        item.addEventListener("click", () => setMobileOpen(false));
      });

      sidebarKillSwitch?.addEventListener("click", () => {
        const existingKillButton = document.getElementById("killBtn");
        if (existingKillButton) existingKillButton.click();
      });

      const normalizeState = (value) => {
        const text = String(value || "").toLowerCase();
        if (text.includes("connected") || text.includes("running") || text.includes("alive")) return "connected";
        if (text.includes("stopped") || text.includes("failed") || text.includes("error") || text.includes("unauthorized")) return "error";
        return "idle";
      };

      const syncSidebarRuntime = () => {
        if (sidebarBackendStatus && backendStatus) {
          sidebarBackendStatus.textContent = backendStatus.textContent || "Unknown";
          sidebarBackendStatus.dataset.state = normalizeState(sidebarBackendStatus.textContent);
        }
        if (sidebarEngineStatus && engineStatusFooter) {
          sidebarEngineStatus.textContent = engineStatusFooter.textContent || "Unknown";
          sidebarEngineStatus.dataset.state = normalizeState(sidebarEngineStatus.textContent);
        }
      };

      syncSidebarRuntime();
      setInterval(syncSidebarRuntime, 1000);
      window.addEventListener("resize", () => {
        if (window.innerWidth > 980) setMobileOpen(false);
      });
    })();
'''

NAV_PATTERN = re.compile(
    r'\n\s*<nav class="page-tabs" aria-label="Pages">.*?</nav>\n',
    flags=re.DOTALL,
)


def patch_html(html: str) -> str:
    """Inject the responsive sidebar while preserving existing page IDs and JS."""
    if MARKER in html:
        return html

    required = ("</style>", '<main class="shell">', "</main>", "<script>", "</script>")
    missing = [marker for marker in required if marker not in html]
    if missing:
        raise ValueError(f"Cannot apply sidebar UI; missing anchors: {', '.join(missing)}")

    if not NAV_PATTERN.search(html):
        raise ValueError("Cannot apply sidebar UI; page navigation block was not found")

    patched = html.replace("</style>", f"{SIDEBAR_CSS}\n  </style>", 1)
    patched = NAV_PATTERN.sub("\n", patched, count=1)
    patched = patched.replace('  <main class="shell">', SIDEBAR_HTML.rstrip(), 1)
    patched = patched.replace(
        'aria-label="Backtest and paper replay page"',
        'aria-label="Historical candle replay page"',
        1,
    )
    patched = patched.replace("<h2>Backtest / Paper Replay</h2>", "<h2>Historical Candle Replay</h2>", 1)
    patched = patched.replace("  </main>\n\n  <script>", "    </main>\n  </div>\n\n  <script>", 1)

    script_close = patched.rfind("  </script>")
    if script_close < 0:
        raise ValueError("Cannot apply sidebar UI; closing script tag was not found")
    patched = patched[:script_close] + SIDEBAR_JS + "\n" + patched[script_close:]

    if patched.count(MARKER) != 1:
        raise ValueError("Sidebar UI marker validation failed")
    return patched


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    index_path = project_root / "frontend" / "index.html"
    original = index_path.read_text(encoding="utf-8")
    patched = patch_html(original)
    if patched == original:
        print("Sidebar UI already applied.")
        return
    index_path.write_text(patched, encoding="utf-8")
    print(f"Applied sidebar UI to {index_path}.")


if __name__ == "__main__":
    main()
