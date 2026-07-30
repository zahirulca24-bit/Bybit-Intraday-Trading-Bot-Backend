import importlib.util
from pathlib import Path


def load_sitecustomize():
    path = Path(__file__).resolve().parents[1] / "backend" / "sitecustomize.py"
    spec = importlib.util.spec_from_file_location("serial9_sitecustomize", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_patch_replaces_stale_scanner_wording_and_defaults():
    module = load_sitecustomize()
    html = """
    <html><head></head><body>
      <strong id="symbolSourceText">Top gainers auto</strong>
      <span id="symbolUniverseText">Refreshes every 10 minutes</span>
      <input id="maxOpenPositions" type="number" value="1">
      <input id="take" type="number" value="1.6">
    </body></html>
    """
    patched = module.patch_frontend_html(html)
    assert "Liquid Intraday Top Movers" in patched
    assert "20-symbol shortlist · 10-symbol deep scan" in patched
    assert 'id="maxOpenPositions" type="number" value="3"' in patched
    assert 'id="take" type="number" value="2.0"' in patched


def test_patch_adds_runtime_truth_once():
    module = load_sitecustomize()
    html = "<html><head></head><body><nav class=\"page-tabs\"></nav></body></html>"
    first = module.patch_frontend_html(html)
    second = module.patch_frontend_html(first)
    assert first.count("serial-9-ui-confirmed-fixes") >= 1
    assert second == first
    assert "/api/durable-state/status" in first
    assert "Closed candles · next-open entry" in first
    assert "20 shortlist → 10 deep scan" in first


def test_patch_surfaces_auth_and_degraded_states():
    module = load_sitecustomize()
    patched = module.patch_frontend_html("<html><head></head><body></body></html>")
    assert "Admin token required to verify" in patched
    assert "Authentication failed" in patched
    assert "persistent disk not configured" in patched
