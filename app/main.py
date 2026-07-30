"""Disabled alternate ASGI entrypoint.

The only supported runtime is ``python backend/secure_server.py``. This module
remains as an explicit fail-closed compatibility endpoint so an accidental
``uvicorn app.main:app`` deployment cannot expose legacy unauthenticated bot
controls.
"""

from __future__ import annotations

import json


MESSAGE = {
    "ok": False,
    "error": "Alternate runtime disabled",
    "canonicalRuntime": "python backend/secure_server.py",
}


async def app(scope, receive, send):
    if scope.get("type") != "http":
        return
    body = json.dumps(MESSAGE, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 410,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
