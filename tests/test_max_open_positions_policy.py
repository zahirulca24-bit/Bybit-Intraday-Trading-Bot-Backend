from backend import secure_server


def test_canonical_runtime_sets_max_open_positions_to_three(monkeypatch):
    monkeypatch.setattr(secure_server.bybit_endpoint_policy, "install", lambda core: None)
    monkeypatch.setattr(secure_server, "install_runtime_security", lambda core: None)
    monkeypatch.setattr(secure_server.verified.guarded, "install_guards", lambda: None)
    monkeypatch.setattr(secure_server, "install_intraday_scanner", lambda core: None)
    monkeypatch.setattr(secure_server.agreement_contract_filter, "install", lambda core: None)
    monkeypatch.setattr(secure_server.cost_policy_fix, "install", lambda core, setup_worker, scanner: None)
    monkeypatch.setattr(secure_server, "install_scanner_review_fixes", lambda core: None)
    monkeypatch.setattr(secure_server, "install_scanner_execution_gate", lambda core: None)
    monkeypatch.setattr(secure_server, "install_replay_accuracy", lambda core: None)
    monkeypatch.setattr(secure_server.verified, "install_position_management", lambda: None)
    monkeypatch.setattr(secure_server.verified, "install_mandatory_entry_protection", lambda: None)
    monkeypatch.setattr(secure_server, "install_durable_runtime", lambda core: None)
    monkeypatch.setattr(
        secure_server.execution_idempotency,
        "install",
        lambda core, execution_handoff: None,
    )
    monkeypatch.setattr(
        secure_server.execution_idempotency_race_fix,
        "install",
        lambda core, execution_handoff: None,
    )
    monkeypatch.setattr(
        secure_server.execution_idempotency_review_fix,
        "install",
        lambda core, execution_handoff: None,
    )
    monkeypatch.setattr(
        secure_server.execution_handoff_safety_hotfix,
        "install",
        lambda core, execution_handoff: None,
    )

    observed = {}

    def fake_start(core, worker, setup_worker, execution_handoff):
        observed["maxOpenPositions"] = core.BOT_STATE["maxOpenPositions"]

    monkeypatch.setattr(secure_server.runtime_orchestrator, "start", fake_start)
    monkeypatch.setitem(secure_server.core.BOT_STATE, "maxOpenPositions", 1)

    secure_server.install_secure_runtime()

    assert observed["maxOpenPositions"] == 3
    assert secure_server.core.BOT_STATE["maxOpenPositions"] == 3
