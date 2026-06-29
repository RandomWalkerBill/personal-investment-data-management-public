-- Canonical instrument mapping schema v1.
--
-- Purpose:
--   Normalize raw/platform-specific instrument identifiers into a stable
--   canonical instrument id before return calculation and frontend display.
--
-- Layering:
--   raw fact -> instrument resolution / master data -> lot/allocation
--   -> return_items -> frontend.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  migration_id TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (datetime('now')),
  description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS canonical_instruments (
  canonical_instrument_id TEXT PRIMARY KEY,
  canonical_symbol TEXT NOT NULL,
  canonical_name TEXT,
  instrument_type TEXT NOT NULL,
  primary_market TEXT,
  listing_currency TEXT,
  isin TEXT,
  cusip TEXT,
  sedol TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  review_status TEXT NOT NULL DEFAULT 'auto_resolved',
  source TEXT NOT NULL DEFAULT 'derived_from_private_facts',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT,
  CHECK (status IN ('active', 'inactive', 'merged', 'needs_review')),
  CHECK (review_status IN ('auto_resolved', 'manual_confirmed', 'needs_review'))
);

CREATE TABLE IF NOT EXISTS platform_instrument_mappings (
  mapping_id TEXT PRIMARY KEY,
  platform_id TEXT NOT NULL,
  account_id TEXT,
  platform_instrument_key TEXT NOT NULL,
  raw_instrument_text TEXT,
  raw_symbol TEXT,
  raw_name TEXT,
  instrument_type TEXT,
  canonical_instrument_id TEXT NOT NULL,
  mapping_confidence TEXT NOT NULL DEFAULT 'medium',
  mapping_status TEXT NOT NULL DEFAULT 'auto',
  effective_from TEXT,
  effective_to TEXT,
  source_refs TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT,
  UNIQUE (platform_id, account_id, platform_instrument_key, effective_from),
  FOREIGN KEY (canonical_instrument_id) REFERENCES canonical_instruments(canonical_instrument_id),
  CHECK (mapping_confidence IN ('high', 'medium', 'low')),
  CHECK (mapping_status IN ('auto', 'manual_confirmed', 'needs_review', 'ignored'))
);

CREATE TABLE IF NOT EXISTS instrument_resolution_queue (
  queue_id TEXT PRIMARY KEY,
  source_table TEXT NOT NULL,
  source_pk TEXT NOT NULL,
  platform_id TEXT,
  account_id TEXT,
  platform_instrument_key TEXT,
  raw_instrument_text TEXT,
  raw_symbol TEXT,
  raw_name TEXT,
  instrument_type TEXT,
  suggested_canonical_instrument_id TEXT,
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_at TEXT,
  resolved_by TEXT,
  notes TEXT,
  CHECK (status IN ('open', 'resolved', 'ignored'))
);

CREATE TABLE IF NOT EXISTS canonical_instrument_mapping_runs (
  mapping_run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  source_scope TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  candidate_count INTEGER NOT NULL DEFAULT 0,
  canonical_count INTEGER NOT NULL DEFAULT 0,
  mapping_count INTEGER NOT NULL DEFAULT 0,
  unresolved_count INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  CHECK (status IN ('running', 'passed', 'needs_review', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_canonical_instruments_symbol
  ON canonical_instruments(canonical_symbol, primary_market, instrument_type);

CREATE INDEX IF NOT EXISTS idx_platform_instrument_mappings_lookup
  ON platform_instrument_mappings(platform_id, account_id, platform_instrument_key, effective_from, effective_to);

CREATE INDEX IF NOT EXISTS idx_platform_instrument_mappings_canonical
  ON platform_instrument_mappings(canonical_instrument_id, mapping_status);

CREATE INDEX IF NOT EXISTS idx_instrument_resolution_queue_status
  ON instrument_resolution_queue(status, platform_id, platform_instrument_key);

DROP VIEW IF EXISTS v_canonical_platform_instruments;
CREATE VIEW v_canonical_platform_instruments AS
SELECT
  m.mapping_id,
  m.platform_id,
  m.account_id,
  m.platform_instrument_key,
  m.raw_symbol,
  m.raw_name,
  m.instrument_type AS platform_instrument_type,
  m.canonical_instrument_id,
  c.canonical_symbol,
  c.canonical_name AS canonical_display_name,
  c.instrument_type AS canonical_instrument_type,
  c.primary_market,
  c.listing_currency,
  m.mapping_confidence,
  m.mapping_status,
  m.effective_from,
  m.effective_to
FROM platform_instrument_mappings m
JOIN canonical_instruments c
  ON c.canonical_instrument_id = m.canonical_instrument_id
WHERE m.mapping_status != 'ignored';

DROP VIEW IF EXISTS v_unresolved_instrument_candidates;
CREATE VIEW v_unresolved_instrument_candidates AS
SELECT
  queue_id,
  source_table,
  source_pk,
  platform_id,
  account_id,
  platform_instrument_key,
  raw_instrument_text,
  raw_symbol,
  raw_name,
  instrument_type,
  suggested_canonical_instrument_id,
  reason,
  created_at
FROM instrument_resolution_queue
WHERE status = 'open';

DROP VIEW IF EXISTS v_market_trades_with_canonical_instrument;
CREATE VIEW v_market_trades_with_canonical_instrument AS
SELECT
  t.*,
  m.canonical_instrument_id,
  m.canonical_symbol,
  m.canonical_display_name,
  m.canonical_instrument_type,
  m.mapping_status AS instrument_mapping_status
FROM market_trades t
LEFT JOIN v_canonical_platform_instruments m
  ON m.platform_id = 'futu'
 AND m.platform_instrument_key =
   CASE
     WHEN instr(COALESCE(t.instrument_symbol, t.instrument_code_raw), '(') > 0
      AND substr(COALESCE(t.instrument_symbol, t.instrument_code_raw), 1, instr(COALESCE(t.instrument_symbol, t.instrument_code_raw), '(') - 1)
          GLOB '[A-Z]*[0-9][0-9][0-9][0-9][0-9][0-9][CP][0-9]*'
       THEN 'OPTION:' || substr(COALESCE(t.instrument_symbol, t.instrument_code_raw), 1, instr(COALESCE(t.instrument_symbol, t.instrument_code_raw), '(') - 1)
     WHEN t.instrument_type = 'option' THEN 'OPTION:' || COALESCE(t.instrument_symbol, t.instrument_code_raw)
     WHEN t.currency = 'HKD' AND substr(COALESCE(t.instrument_symbol, t.instrument_code_raw), 1, 5) GLOB '[0-9][0-9][0-9][0-9][0-9]'
       THEN 'HK:' || substr(COALESCE(t.instrument_symbol, t.instrument_code_raw), 1, 5)
     WHEN t.currency = 'HKD' AND substr(COALESCE(t.instrument_symbol, t.instrument_code_raw), 1, 4) GLOB '[0-9][0-9][0-9][0-9]'
       THEN 'HK:0' || substr(COALESCE(t.instrument_symbol, t.instrument_code_raw), 1, 4)
     WHEN t.currency = 'USD' THEN 'US:' || COALESCE(t.instrument_symbol, t.instrument_code_raw)
     ELSE 'RAW:' || COALESCE(t.instrument_symbol, t.instrument_code_raw)
   END;

DROP VIEW IF EXISTS v_fund_orders_with_canonical_instrument;
CREATE VIEW v_fund_orders_with_canonical_instrument AS
SELECT
  f.*,
  m.canonical_instrument_id,
  m.canonical_symbol,
  m.canonical_display_name,
  m.canonical_instrument_type,
  m.mapping_status AS instrument_mapping_status
FROM fund_orders f
LEFT JOIN v_canonical_platform_instruments m
 ON m.platform_id = 'futu'
 AND m.platform_instrument_key = 'FUND:' || f.instrument_code;

DROP VIEW IF EXISTS v_lot_allocations_with_canonical_instrument;
CREATE VIEW v_lot_allocations_with_canonical_instrument AS
SELECT
  a.*,
  m.canonical_instrument_id,
  m.canonical_symbol,
  m.canonical_display_name,
  m.canonical_instrument_type,
  m.mapping_status AS instrument_mapping_status
FROM lot_allocations a
LEFT JOIN v_canonical_platform_instruments m
  ON m.platform_id = 'futu'
 AND m.platform_instrument_key = a.instrument_key;

DROP VIEW IF EXISTS v_option_lot_allocations_with_canonical_instrument;
CREATE VIEW v_option_lot_allocations_with_canonical_instrument AS
SELECT
  a.*,
  m.canonical_instrument_id,
  m.canonical_symbol,
  m.canonical_display_name,
  m.canonical_instrument_type,
  m.mapping_status AS instrument_mapping_status
FROM option_lot_allocations a
LEFT JOIN v_canonical_platform_instruments m
 ON m.platform_id = 'futu'
 AND m.platform_instrument_key = 'OPTION:' || a.option_code;

DROP VIEW IF EXISTS v_fund_lot_allocations_with_canonical_instrument;
CREATE VIEW v_fund_lot_allocations_with_canonical_instrument AS
SELECT
  a.*,
  m.canonical_instrument_id,
  m.canonical_symbol,
  m.canonical_display_name,
  m.canonical_instrument_type,
  m.mapping_status AS instrument_mapping_status
FROM fund_lot_allocations a
LEFT JOIN v_canonical_platform_instruments m
  ON m.platform_id = 'futu'
 AND m.platform_instrument_key = a.fund_key;

DROP VIEW IF EXISTS v_short_stock_allocations_with_canonical_instrument;
CREATE VIEW v_short_stock_allocations_with_canonical_instrument AS
SELECT
  a.*,
  m.canonical_instrument_id,
  m.canonical_symbol,
  m.canonical_display_name,
  m.canonical_instrument_type,
  m.mapping_status AS instrument_mapping_status
FROM short_stock_allocations a
LEFT JOIN v_canonical_platform_instruments m
  ON m.platform_id = 'futu'
 AND m.platform_instrument_key = a.instrument_key;

DROP VIEW IF EXISTS v_canonical_instrument_resolution_coverage;
CREATE VIEW v_canonical_instrument_resolution_coverage AS
SELECT 'market_trades' AS source_table,
       COUNT(*) AS row_count,
       SUM(CASE WHEN canonical_instrument_id IS NOT NULL THEN 1 ELSE 0 END) AS resolved_count,
       SUM(CASE WHEN canonical_instrument_id IS NULL THEN 1 ELSE 0 END) AS unresolved_count
FROM v_market_trades_with_canonical_instrument
UNION ALL
SELECT 'fund_orders',
       COUNT(*),
       SUM(CASE WHEN canonical_instrument_id IS NOT NULL THEN 1 ELSE 0 END),
       SUM(CASE WHEN canonical_instrument_id IS NULL THEN 1 ELSE 0 END)
FROM v_fund_orders_with_canonical_instrument
UNION ALL
SELECT 'lot_allocations',
       COUNT(*),
       SUM(CASE WHEN canonical_instrument_id IS NOT NULL THEN 1 ELSE 0 END),
       SUM(CASE WHEN canonical_instrument_id IS NULL THEN 1 ELSE 0 END)
FROM v_lot_allocations_with_canonical_instrument
UNION ALL
SELECT 'option_lot_allocations',
       COUNT(*),
       SUM(CASE WHEN canonical_instrument_id IS NOT NULL THEN 1 ELSE 0 END),
       SUM(CASE WHEN canonical_instrument_id IS NULL THEN 1 ELSE 0 END)
FROM v_option_lot_allocations_with_canonical_instrument
UNION ALL
SELECT 'fund_lot_allocations',
       COUNT(*),
       SUM(CASE WHEN canonical_instrument_id IS NOT NULL THEN 1 ELSE 0 END),
       SUM(CASE WHEN canonical_instrument_id IS NULL THEN 1 ELSE 0 END)
FROM v_fund_lot_allocations_with_canonical_instrument
UNION ALL
SELECT 'short_stock_allocations',
       COUNT(*),
       SUM(CASE WHEN canonical_instrument_id IS NOT NULL THEN 1 ELSE 0 END),
       SUM(CASE WHEN canonical_instrument_id IS NULL THEN 1 ELSE 0 END)
FROM v_short_stock_allocations_with_canonical_instrument;

INSERT OR IGNORE INTO schema_migrations (migration_id, description)
VALUES (
  'canonical_instrument_mapping_schema_v1',
  '统一标的映射层 v1：canonical instruments、platform mappings、resolution queue 和 enriched views。'
);
