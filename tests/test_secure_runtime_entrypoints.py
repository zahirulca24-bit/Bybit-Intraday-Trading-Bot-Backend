import asyncio
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_render_uses_secure_canonical_runtime():
    render = (ROOT / "render.yaml").read_text(encoding="utf-8")

    assert "startCommand: python backend/secure_server.py" in render
    assert "startCommand: python backend/position_synced_server.py" not in render
    assert "app.main:app" not in render


def test_alternate_asgi_source_does_not_import_trading_backend():
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")

    assert "import server as bot" not in source
    assert "place_demo_order" not in source
    assert "close_symbol_positions" not in source
    assert "Alternate runtime disabled" in source


def test_alternate_asgi_runtime_returns_gone_without_cors_wildcard():
    module_path = ROOT / "app" / "main.py"
    spec = importlib.util.spec_from_file_location("disabled_alternate_runtime", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(
        module.app(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/bybit/demo-order",
                "headers": [],
            },
            receive,
            send,
        )
    )

    start = messages[0]
    body = messages[1]["body"]
    headers = dict(start["headers"])
    assert start["status"] == 410
    assert b"Alternate runtime disabled" in body
    assert b"access-control-allow-origin" not in headers
