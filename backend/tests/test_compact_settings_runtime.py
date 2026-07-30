import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "sitecustomize.py"
SPEC = importlib.util.spec_from_file_location("compact_settings_sitecustomize", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_frontend_patch_sets_three_and_compact_layout_once():
    original = """<html><head></head><body><input id=\"maxOpenPositions\" type=\"number\" value=\"1\"></body></html>"""
    patched = MODULE.patch_frontend_html(original)

    assert 'id="maxOpenPositions" type="number" value="3"' in patched
    assert 'id="serial-9-ui-confirmed-fixes"' in patched
    assert 'grid-template-columns: repeat(2, minmax(0, 1fr))' in patched
    assert patched.count('id="serial-9-ui-confirmed-fixes"') == 1

    patched_twice = MODULE.patch_frontend_html(patched)
    assert patched_twice == patched


def test_unrelated_text_is_preserved():
    text = "plain configuration text"
    assert MODULE.patch_frontend_html(text) == text
