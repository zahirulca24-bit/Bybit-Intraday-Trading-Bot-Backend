from __future__ import annotations

from pathlib import Path


def test_canonical_runtime_installs_handoff_hotfix_before_workers_start():
    source = (Path(__file__).resolve().parents[1] / "backend" / "secure_server.py").read_text(
        encoding="utf-8"
    )

    install_call = "execution_handoff_safety_hotfix.install(core, execution_handoff)"
    worker_start = "runtime_orchestrator.start(core, worker, setup_worker, execution_handoff)"

    assert install_call in source
    assert worker_start in source
    assert source.index(install_call) < source.index(worker_start)
