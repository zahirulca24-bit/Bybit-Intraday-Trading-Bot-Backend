"""PostgreSQL migration for the canonical live Bybit execution ledger."""

LIVE_EXECUTION_MIGRATION = (
    4,
    (
        "CREATE TABLE IF NOT EXISTS live_execution_ledger ("
        "exec_id TEXT PRIMARY KEY,"
        "trading_date TEXT NOT NULL,"
        "exec_time BIGINT NOT NULL,"
        "sequence_no BIGINT,"
        "symbol TEXT NOT NULL,"
        "order_id TEXT,"
        "order_link_id TEXT,"
        "side TEXT NOT NULL,"
        "exec_type TEXT NOT NULL,"
        "exec_qty NUMERIC NOT NULL,"
        "exec_price NUMERIC NOT NULL,"
        "exec_fee NUMERIC NOT NULL,"
        "fee_currency TEXT NOT NULL,"
        "leaves_qty NUMERIC NOT NULL,"
        "api_closed_size NUMERIC NOT NULL,"
        "closed_size NUMERIC NOT NULL,"
        "entry_size NUMERIC NOT NULL,"
        "position_before NUMERIC NOT NULL,"
        "position_after NUMERIC NOT NULL,"
        "action TEXT NOT NULL CHECK(action IN ('ENTRY','ADD','PARTIAL_EXIT','FULL_EXIT','REVERSAL')),"
        "is_maker BOOLEAN,"
        "raw_payload JSONB NOT NULL,"
        "synced_at BIGINT NOT NULL"
        ")",
        "CREATE INDEX IF NOT EXISTS ix_live_execution_date_time "
        "ON live_execution_ledger(trading_date,exec_time DESC,exec_id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_live_execution_symbol_date "
        "ON live_execution_ledger(symbol,trading_date,exec_time DESC)",
        "CREATE INDEX IF NOT EXISTS ix_live_execution_order "
        "ON live_execution_ledger(order_id,exec_time DESC)",
    ),
)
