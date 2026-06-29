-- Canonical instrument mapping schema v1.
--
-- Purpose:
--   Normalize raw/platform-specific instrument identifiers into a stable
--   canonical instrument id before return calculation and frontend display.
--
-- Privacy:
--   This schema contains no personal data and no raw investment records.

PRAGMA foreign_keys = ON;

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

