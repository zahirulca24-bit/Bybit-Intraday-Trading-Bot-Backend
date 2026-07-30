# CI Verification Trigger

This documentation-only change exists to trigger the repository's `Strict Verification` workflow on a pull request after PR #44 introduced the workflow.

No trading, execution, risk, frontend, or deployment behavior is changed.

Acceptance requires the workflow to complete successfully for:

- Python source compilation
- tracked-file secret scan
- canonical secure runtime installation smoke test
- complete available pytest suite
