from backend import secure_server


def handler(path="/api/bybit/wallet"):
    instance = object.__new__(secure_server.SecurePositionSyncedHandler)
    instance.path = path
    return instance


def test_unauthorized_get_never_reaches_base_handler(monkeypatch):
    reached = []
    monkeypatch.setattr(secure_server, "reject_disallowed_origin", lambda request: False)
    monkeypatch.setattr(
        secure_server,
        "authorize_get",
        lambda request, path: False,
    )
    monkeypatch.setattr(
        secure_server.verified.PositionSyncedHandler,
        "do_GET",
        lambda request: reached.append(True),
    )

    secure_server.SecurePositionSyncedHandler.do_GET(handler())
    assert reached == []


def test_authorized_get_reaches_verified_base_handler(monkeypatch):
    reached = []
    monkeypatch.setattr(secure_server, "reject_disallowed_origin", lambda request: False)
    monkeypatch.setattr(
        secure_server,
        "authorize_get",
        lambda request, path: True,
    )
    monkeypatch.setattr(
        secure_server.verified.PositionSyncedHandler,
        "do_GET",
        lambda request: reached.append(request.path),
    )

    secure_server.SecurePositionSyncedHandler.do_GET(handler("/api/bot/status"))
    assert reached == ["/api/bot/status"]


def test_disallowed_origin_post_never_reaches_trading_handler(monkeypatch):
    reached = []
    monkeypatch.setattr(secure_server, "reject_disallowed_origin", lambda request: True)
    monkeypatch.setattr(
        secure_server.verified.PositionSyncedHandler,
        "do_POST",
        lambda request: reached.append(True),
    )

    secure_server.SecurePositionSyncedHandler.do_POST(
        handler("/api/bybit/demo-order")
    )
    assert reached == []
