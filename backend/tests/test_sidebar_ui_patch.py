from scripts.apply_sidebar_ui import MARKER, patch_html


def sample_html() -> str:
    return '''<!doctype html>
<html>
<head><style>.shell { width: 100%; }</style></head>
<body class="dashboard-view page-dashboard">
  <main class="shell">
    <nav class="page-tabs" aria-label="Pages">
      <button class="page-tab" data-page="dashboard" id="dashboardTab">Dashboard</button>
      <button class="page-tab" data-page="backtest" id="backtestTab">Backtest Replay</button>
    </nav>
    <section id="backtestPage" aria-label="Backtest and paper replay page">
      <h2>Backtest / Paper Replay</h2>
    </section>
  </main>

  <script>
    const backendStatus = document.createElement("span");
    const engineStatusFooter = document.createElement("span");
  </script>
</body>
</html>'''


def test_patch_html_adds_responsive_sidebar_and_preserves_page_ids():
    patched = patch_html(sample_html())

    assert MARKER in patched
    assert patched.count('id="dashboardTab"') == 1
    assert patched.count('id="backtestTab"') == 1
    assert 'id="sidebarMobileToggle"' in patched
    assert 'id="sidebarKillSwitch"' in patched
    assert "Historical Candle Replay" in patched
    assert "Backtest / Paper Replay" not in patched
    assert "sidebar-ui-v1" in patched


def test_patch_html_is_idempotent():
    once = patch_html(sample_html())
    twice = patch_html(once)

    assert twice == once
