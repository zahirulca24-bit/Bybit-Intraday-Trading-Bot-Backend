from backend import agreement_contract_filter


class Core:
    def __init__(self):
        self._agreement_contract_filter_installed = False

        def universe(*_args, **_kwargs):
            return {
                "symbols": ["BTCUSDT", "MUUSDT", "CLUSDT", "ETHUSDT"],
                "rows": [
                    {"symbol": "BTCUSDT"},
                    {"symbol": "MUUSDT"},
                    {"symbol": "CLUSDT"},
                    {"symbol": "ETHUSDT"},
                ],
                "shortlist": [
                    {"symbol": "MUUSDT"},
                    {"symbol": "ETHUSDT"},
                    {"symbol": "CLUSDT"},
                ],
                "metrics": {},
            }

        self.top_gainer_universe = universe


def test_builtin_agreement_contracts_are_filtered_from_all_universe_views(monkeypatch):
    monkeypatch.delenv("AGREEMENT_REQUIRED_SYMBOLS", raising=False)
    core = Core()

    agreement_contract_filter.install(core)
    result = core.top_gainer_universe(force=True, limit=10)

    assert result["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert [row["symbol"] for row in result["rows"]] == ["BTCUSDT", "ETHUSDT"]
    assert [row["symbol"] for row in result["shortlist"]] == ["ETHUSDT"]
    assert result["agreementRequiredExcludedSymbols"] == ["CLUSDT", "MUUSDT"]


def test_direct_scanner_requests_reject_agreement_contracts(monkeypatch):
    monkeypatch.setenv("AGREEMENT_REQUIRED_SYMBOLS", "TESTUSDT")

    accepted, rejected = agreement_contract_filter.filter_symbols(
        ["BTCUSDT", "muusdt", "CLUSDT", "TESTUSDT"]
    )

    assert accepted == ["BTCUSDT"]
    assert rejected == ["MUUSDT", "CLUSDT", "TESTUSDT"]


def test_environment_can_only_add_exclusions(monkeypatch):
    monkeypatch.setenv("AGREEMENT_REQUIRED_SYMBOLS", "ETHUSDT")

    assert agreement_contract_filter.excluded_symbols() == frozenset(
        {"MUUSDT", "CLUSDT", "ETHUSDT"}
    )
