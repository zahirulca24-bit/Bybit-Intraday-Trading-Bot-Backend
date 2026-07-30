from backend.position_sync import collect_open_positions


def test_collects_all_non_zero_positions_across_pages():
    calls = []

    def requester(method, path, params):
        calls.append(dict(params))
        if not params.get("cursor"):
            return {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "list": [
                        {"symbol": "BTCUSDT", "side": "Buy", "size": "0.002", "positionIdx": 0, "unrealisedPnl": "0.10"},
                        {"symbol": "ETHUSDT", "side": "", "size": "0", "positionIdx": 0},
                    ],
                    "nextPageCursor": "page-2",
                },
            }
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {"symbol": "GWEIUSDT", "side": "Buy", "size": "9834", "positionIdx": 0, "unrealisedPnl": "-0.49"},
                    {"symbol": "CHILLGUYUSDT", "side": "Buy", "size": "21123", "positionIdx": 0, "unrealisedPnl": "1.37"},
                ],
                "nextPageCursor": "",
            },
        }

    payload = collect_open_positions(requester)

    assert payload["retCode"] == 0
    assert payload["result"]["count"] == 3
    assert [row["symbol"] for row in payload["result"]["list"]] == ["BTCUSDT", "CHILLGUYUSDT", "GWEIUSDT"]
    assert len(calls) == 2
    assert calls[0]["limit"] == "200"
    assert calls[1]["cursor"] == "page-2"


def test_deduplicates_position_rows_and_propagates_exchange_errors():
    row = {"symbol": "BTCUSDT", "side": "Buy", "size": "0.002", "positionIdx": 0}

    def duplicate_requester(method, path, params):
        return {"retCode": 0, "retMsg": "OK", "result": {"list": [row, dict(row)], "nextPageCursor": ""}}

    payload = collect_open_positions(duplicate_requester)
    assert payload["result"]["count"] == 1

    def failing_requester(method, path, params):
        return {"retCode": 10001, "retMsg": "position query failed", "result": {}}

    failed = collect_open_positions(failing_requester)
    assert failed["retCode"] == 10001
    assert failed["result"]["list"] == []


def test_request_exception_and_invalid_payload_fail_closed():
    def raising_requester(method, path, params):
        raise TimeoutError("exchange timeout")

    timed_out = collect_open_positions(raising_requester)
    assert timed_out["retCode"] == -1
    assert "TimeoutError" in timed_out["retMsg"]
    assert timed_out["result"]["count"] == 0

    invalid = collect_open_positions(lambda method, path, params: {"retCode": 0, "result": {"list": None}})
    assert invalid["retCode"] == -1
    assert "invalid position list" in invalid["retMsg"]


def test_incomplete_or_repeated_pagination_fails_closed():
    def endless_requester(method, path, params):
        current = params.get("cursor") or "start"
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [],
                "nextPageCursor": f"{current}-next",
            },
        }

    incomplete = collect_open_positions(endless_requester, max_pages=2)
    assert incomplete["retCode"] == -1
    assert "pagination limit reached" in incomplete["retMsg"]

    def repeated_cursor_requester(method, path, params):
        cursor = params.get("cursor")
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [],
                "nextPageCursor": "page-2" if not cursor else cursor,
            },
        }

    repeated = collect_open_positions(repeated_cursor_requester)
    assert repeated["retCode"] == -1
    assert "repeated pagination cursor" in repeated["retMsg"]
