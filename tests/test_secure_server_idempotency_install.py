from __future__ import annotations

from pathlib import Path


def test_canonical_runtime_installs_idempotency_after_durable_store_before_workers():
    source = (Path(__file__).resolve().parents[1] / "backend" / "secure_server.py").read_text(
        encoding="utf-8"
    )

    durable_install = "install_durable_runtime(core)"
    idempotency_install = "execution_idempotency.install(core, execution_handoff)"
    review_fix_install = "execution_idempotency_review_fix.install(core, execution_handoff)"
    hotfix_install = "execution_handoff_safety_hotfix.install(core, execution_handoff)"
    worker_start = "runtime_orchestrator.start(core, worker, setup_worker, execution_handoff)"

    assert durable_install in source
    assert idempotency_install in source
    assert review_fix_install in source
    assert hotfix_install in source
    assert worker_start in source
    assert source.index(durable_install) < source.index(idempotency_install)
    assert source.index(idempotency_install) < source.index(review_fix_install)
    assert source.index(review_fix_install) < source.index(hotfix_install)
    assert source.index(review_fix_install) < source.index(worker_start)


def test_worker_status_exposes_idempotency_policy_and_review_fix():
    source = (Path(__file__).resolve().parents[1] / "backend" / "secure_server.py").read_text(
        encoding="utf-8"
    )

    assert '"executionIdempotency": execution_idempotency.status(core, execution_handoff)' in source
    assert '"executionIdempotencyReviewFix": execution_idempotency_review_fix.status(' in source
