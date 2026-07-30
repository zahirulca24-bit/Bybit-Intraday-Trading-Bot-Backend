from scripts.apply_compact_settings import MARKER, patch_html


def sample_sidebar_html() -> str:
    return '''<!doctype html>
<html>
<head><style>.panel { display: block; }</style></head>
<body class="control-view page-settings">
  <div class="app-layout" data-sidebar-shell="v1">
    <main class="shell" id="appContent">
      <section class="main-grid">
        <section class="control-col">
          <section class="panel">
            <div class="panel-body">
              <div class="field"><input id="adminToken"></div>
              <div class="field"><input id="maxAllocation"></div>
              <div class="field"><input id="breakevenTrigger"></div>
              <div class="field"><select id="routerMode"></select></div>
              <div class="auto-state" id="autoStatus">Stopped</div>
              <div class="btn-row"><button id="autoStartBtn">Start auto</button><button id="autoStopBtn">Stop auto</button></div>
              <div class="btn-row"><button id="startBtn">Manual buy test</button><button id="manageStopsBtn">Manage stops</button></div>
              <button id="killBtn">Kill switch</button>
              <div id="controlNotice"></div>
              <div id="engineGrid"></div>
            </div>
          </section>
          <section class="panel">Risk guard</section>
          <section class="panel">Engine health</section>
        </section>
        <aside class="right-col"><section class="execution-panel"><div class="timeline"></div></section></aside>
      </section>
    </main>
  </div>
  <script>
    const autoStatus = document.getElementById("autoStatus");
  </script>
</body>
</html>'''


def test_patch_adds_compact_grid_and_runtime_controls():
    patched = patch_html(sample_sidebar_html())

    assert MARKER in patched
    assert "compact-settings-grid" in patched
    assert "settings-section-label" in patched
    assert "syncRuntimeButtons" in patched
    assert "body.control-view .control-col" in patched
    assert "body.control-view .right-col" in patched


def test_patch_is_idempotent():
    once = patch_html(sample_sidebar_html())
    twice = patch_html(once)

    assert twice == once
