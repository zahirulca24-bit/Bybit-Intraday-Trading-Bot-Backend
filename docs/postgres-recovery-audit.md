# PostgreSQL Recovery Audit

Daily Top100 previously bound the durable state store only during first install. When PostgreSQL was degraded at startup, the module retained no store binding and did not recover automatically after the database became healthy. The recovery patch dynamically rechecks the current durable store before build/ensure/install-guard operations and reloads persisted state when a healthy store becomes available.
