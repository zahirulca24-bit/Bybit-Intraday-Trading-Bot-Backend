# PostgreSQL Recovery Runtime Checklist

1. Deploy the backend branch.
2. Verify durable-state reports backend `postgresql`, `restartSafe: true`, and `degraded: false`.
3. Verify Daily Top100 reports `persisted: true` after PostgreSQL recovery.
4. Restart the backend and confirm the persisted Daily Top100 snapshot reloads.
5. Confirm the frontend moves from degraded/wait to current truth through its existing five-second refresh without fabricated PASS states.
