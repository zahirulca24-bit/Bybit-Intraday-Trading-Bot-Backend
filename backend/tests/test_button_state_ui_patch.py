from pathlib import Path
import runpy


def test_runtime_button_state_patch(tmp_path, monkeypatch):
    root = tmp_path
    scripts = root / "scripts"
    frontend = root / "frontend"
    scripts.mkdir()
    frontend.mkdir()

    source_script = Path(__file__).resolve().parents[2] / "scripts" / "apply_button_state_ui.py"
    target_script = scripts / "apply_button_state_ui.py"
    target_script.write_text(source_script.read_text(encoding="utf-8"), encoding="utf-8")

    html = '''<script>
      const autoStartBtn = document.getElementById("autoStartBtn");
      const startBtn = document.getElementById("startBtn");
      if (capReached) {
        if (autoStartBtn) autoStartBtn.disabled = true;
        if (startBtn) startBtn.disabled = true;
      } else {
        if (autoStartBtn) autoStartBtn.disabled = false;
        if (startBtn) startBtn.disabled = false;
      }
    </script>'''
    index = frontend / "index.html"
    index.write_text(html, encoding="utf-8")

    runpy.run_path(str(target_script), run_name="__main__")
    patched = index.read_text(encoding="utf-8")

    assert "button-state-ui-v1" in patched
    assert 'const autoStopBtn = document.getElementById("autoStopBtn");' in patched
    assert "autoStartBtn.disabled = Boolean(bot.enabled)" in patched
    assert "autoStopBtn.disabled = !Boolean(bot.enabled)" in patched
    assert "autoStopBtn.disabled = false" in patched

    runpy.run_path(str(target_script), run_name="__main__")
    assert index.read_text(encoding="utf-8") == patched
