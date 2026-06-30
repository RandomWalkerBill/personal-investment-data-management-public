-- 统一账户映射层 v1
-- 目标：保留结单/平台原始账户，同时提供跨账户升级、子账户和 owner-level 展示的统一账户口径。
-- 约束：不改写 raw fact 表；交易、基金、现金、资产变动通过 statement_accounts 关联原始账户，再映射到 canonical account。

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations (migration_id, description)
VALUES (
  'canonical_account_mapping_schema_v1',
  '统一账户映射层 v1：canonical_accounts、原始账户映射、账户迁移事件和按账户 enrich 的事实视图。'
);

INSERT OR IGNORE INTO schema_migrations (migration_id, description)
VALUES (
  'canonical_account_groups_extension_v1',
  '统一账户映射层扩展：account_groups、account_group_memberships 和跨平台账户组筛选视图。'
);

CREATE TABLE IF NOT EXISTS canonical_accounts (
  canonical_account_id TEXT PRIMARY KEY,
  owner_label TEXT,
  platform TEXT NOT NULL,
  broker TEXT,
  canonical_account_label TEXT NOT NULL,
  account_scope TEXT NOT NULL,
  base_currency TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  review_status TEXT NOT NULL DEFAULT 'auto_resolved',
  source TEXT NOT NULL DEFAULT 'manual_seed',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT,
  CHECK (status IN ('active', 'inactive', 'merged', 'needs_review')),
  CHECK (review_status IN ('auto_resolved', 'manual_confirmed', 'needs_review'))
);

CREATE TABLE IF NOT EXISTS canonical_account_mappings (
  mapping_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  canonical_account_id TEXT NOT NULL,
  mapping_type TEXT NOT NULL,
  effective_from_period TEXT,
  effective_to_period TEXT,
  confidence TEXT NOT NULL DEFAULT 'manual_confirmed',
  status TEXT NOT NULL DEFAULT 'active',
  source TEXT NOT NULL DEFAULT 'manual_seed',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT,
  FOREIGN KEY (account_id) REFERENCES accounts(account_id),
  FOREIGN KEY (canonical_account_id) REFERENCES canonical_accounts(canonical_account_id),
  CHECK (confidence IN ('high', 'medium', 'low', 'manual_confirmed', 'inferred')),
  CHECK (status IN ('active', 'inactive', 'needs_review'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical_account_mappings_active_account
ON canonical_account_mappings(account_id)
WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_canonical_account_mappings_canonical
ON canonical_account_mappings(canonical_account_id, status);

CREATE TABLE IF NOT EXISTS account_migration_events (
  account_migration_id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  migration_date TEXT,
  migration_type TEXT NOT NULL,
  from_account_id TEXT,
  to_account_id TEXT,
  canonical_account_id TEXT NOT NULL,
  evidence_source TEXT,
  evidence_refs TEXT,
  status TEXT NOT NULL DEFAULT 'confirmed',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT,
  FOREIGN KEY (from_account_id) REFERENCES accounts(account_id),
  FOREIGN KEY (to_account_id) REFERENCES accounts(account_id),
  FOREIGN KEY (canonical_account_id) REFERENCES canonical_accounts(canonical_account_id),
  CHECK (status IN ('confirmed', 'inferred', 'needs_review'))
);

CREATE TABLE IF NOT EXISTS account_groups (
  account_group_id TEXT PRIMARY KEY,
  owner_label TEXT,
  group_label TEXT NOT NULL,
  group_type TEXT NOT NULL,
  platform TEXT,
  broker TEXT,
  base_currency TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  review_status TEXT NOT NULL DEFAULT 'auto_resolved',
  source TEXT NOT NULL DEFAULT 'manual_seed',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT,
  CHECK (group_type IN ('owner', 'platform', 'broker', 'portfolio', 'strategy', 'tax_scope', 'custom')),
  CHECK (status IN ('active', 'inactive', 'needs_review')),
  CHECK (review_status IN ('auto_resolved', 'manual_confirmed', 'needs_review'))
);

CREATE TABLE IF NOT EXISTS account_group_memberships (
  membership_id TEXT PRIMARY KEY,
  account_group_id TEXT NOT NULL,
  canonical_account_id TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member',
  effective_from_period TEXT,
  effective_to_period TEXT,
  confidence TEXT NOT NULL DEFAULT 'manual_confirmed',
  status TEXT NOT NULL DEFAULT 'active',
  source TEXT NOT NULL DEFAULT 'manual_seed',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT,
  FOREIGN KEY (account_group_id) REFERENCES account_groups(account_group_id),
  FOREIGN KEY (canonical_account_id) REFERENCES canonical_accounts(canonical_account_id),
  CHECK (confidence IN ('high', 'medium', 'low', 'manual_confirmed', 'inferred')),
  CHECK (status IN ('active', 'inactive', 'needs_review'))
);

CREATE INDEX IF NOT EXISTS idx_account_group_memberships_group
ON account_group_memberships(account_group_id, status);

CREATE INDEX IF NOT EXISTS idx_account_group_memberships_canonical
ON account_group_memberships(canonical_account_id, status);

DROP VIEW IF EXISTS v_account_group_memberships;
CREATE VIEW v_account_group_memberships AS
SELECT
  gm.membership_id,
  gm.account_group_id,
  ag.group_label,
  ag.group_type,
  ag.platform AS group_platform,
  ag.broker AS group_broker,
  ag.base_currency AS group_base_currency,
  gm.canonical_account_id,
  ca.canonical_account_label,
  ca.platform AS canonical_account_platform,
  ca.broker AS canonical_account_broker,
  ca.account_scope AS canonical_account_scope,
  gm.role,
  gm.effective_from_period,
  gm.effective_to_period,
  gm.confidence,
  gm.status,
  gm.notes
FROM account_group_memberships gm
JOIN account_groups ag
  ON ag.account_group_id = gm.account_group_id
JOIN canonical_accounts ca
  ON ca.canonical_account_id = gm.canonical_account_id;

DROP VIEW IF EXISTS v_accounts_with_canonical_account;
CREATE VIEW v_accounts_with_canonical_account AS
SELECT
  a.account_id AS raw_account_id,
  a.owner_label AS raw_owner_label,
  a.platform AS raw_platform,
  a.broker AS raw_broker,
  a.account_label AS raw_account_label,
  a.base_currency AS raw_base_currency,
  a.status AS raw_account_status,
  a.notes AS raw_account_notes,
  m.canonical_account_id,
  ca.owner_label AS canonical_owner_label,
  ca.platform AS canonical_account_platform,
  ca.broker AS canonical_account_broker,
  ca.canonical_account_label,
  ca.account_scope AS canonical_account_scope,
  ca.base_currency AS canonical_base_currency,
  m.mapping_type AS account_mapping_type,
  m.confidence AS account_mapping_confidence,
  m.status AS account_mapping_status,
  m.notes AS account_mapping_notes
FROM accounts a
LEFT JOIN canonical_account_mappings m
  ON m.account_id = a.account_id
 AND m.status = 'active'
LEFT JOIN canonical_accounts ca
  ON ca.canonical_account_id = m.canonical_account_id;

DROP VIEW IF EXISTS v_statement_accounts_with_canonical_account;
CREATE VIEW v_statement_accounts_with_canonical_account AS
SELECT
  sa.import_run_id,
  sa.statement_id,
  rs.period,
  rs.filename,
  sa.account_id AS raw_account_id,
  a.account_label AS raw_account_label,
  a.platform AS raw_account_platform,
  a.broker AS raw_account_broker,
  a.base_currency AS raw_account_base_currency,
  sa.link_source AS statement_account_link_source,
  sa.confidence AS statement_account_confidence,
  m.canonical_account_id,
  ca.owner_label AS canonical_owner_label,
  ca.platform AS canonical_account_platform,
  ca.broker AS canonical_account_broker,
  ca.canonical_account_label,
  ca.account_scope AS canonical_account_scope,
  ca.base_currency AS canonical_account_base_currency,
  m.mapping_type AS account_mapping_type,
  m.confidence AS account_mapping_confidence
FROM statement_accounts sa
JOIN raw_statements rs
  ON rs.import_run_id = sa.import_run_id
 AND rs.statement_id = sa.statement_id
JOIN accounts a
  ON a.account_id = sa.account_id
LEFT JOIN canonical_account_mappings m
  ON m.account_id = sa.account_id
 AND m.status = 'active'
LEFT JOIN canonical_accounts ca
  ON ca.canonical_account_id = m.canonical_account_id;

DROP VIEW IF EXISTS v_market_trades_with_accounts;
CREATE VIEW v_market_trades_with_accounts AS
SELECT
  mt.*,
  va.raw_account_id,
  va.raw_account_label,
  va.raw_account_platform,
  va.raw_account_broker,
  va.raw_account_base_currency,
  va.statement_account_link_source,
  va.statement_account_confidence,
  va.canonical_account_id,
  va.canonical_owner_label,
  va.canonical_account_platform,
  va.canonical_account_broker,
  va.canonical_account_label,
  va.canonical_account_scope,
  va.canonical_account_base_currency,
  va.account_mapping_type,
  va.account_mapping_confidence
FROM market_trades mt
LEFT JOIN v_statement_accounts_with_canonical_account va
  ON va.import_run_id = mt.import_run_id
 AND va.statement_id = mt.statement_id;

DROP VIEW IF EXISTS v_fund_orders_with_accounts;
CREATE VIEW v_fund_orders_with_accounts AS
SELECT
  fo.*,
  va.raw_account_id,
  va.raw_account_label,
  va.raw_account_platform,
  va.raw_account_broker,
  va.raw_account_base_currency,
  va.statement_account_link_source,
  va.statement_account_confidence,
  va.canonical_account_id,
  va.canonical_owner_label,
  va.canonical_account_platform,
  va.canonical_account_broker,
  va.canonical_account_label,
  va.canonical_account_scope,
  va.canonical_account_base_currency,
  va.account_mapping_type,
  va.account_mapping_confidence
FROM fund_orders fo
LEFT JOIN v_statement_accounts_with_canonical_account va
  ON va.import_run_id = fo.import_run_id
 AND va.statement_id = fo.statement_id;

DROP VIEW IF EXISTS v_cash_ledger_entries_with_accounts;
CREATE VIEW v_cash_ledger_entries_with_accounts AS
SELECT
  ce.*,
  va.raw_account_id,
  va.raw_account_label,
  va.raw_account_platform,
  va.raw_account_broker,
  va.raw_account_base_currency,
  va.statement_account_link_source,
  va.statement_account_confidence,
  va.canonical_account_id,
  va.canonical_owner_label,
  va.canonical_account_platform,
  va.canonical_account_broker,
  va.canonical_account_label,
  va.canonical_account_scope,
  va.canonical_account_base_currency,
  va.account_mapping_type,
  va.account_mapping_confidence
FROM cash_ledger_entries ce
LEFT JOIN v_statement_accounts_with_canonical_account va
  ON va.import_run_id = ce.import_run_id
 AND va.statement_id = ce.statement_id;

DROP VIEW IF EXISTS v_asset_movement_events_with_accounts;
CREATE VIEW v_asset_movement_events_with_accounts AS
SELECT
  ame.*,
  va.raw_account_id,
  va.raw_account_label,
  va.raw_account_platform,
  va.raw_account_broker,
  va.raw_account_base_currency,
  va.statement_account_link_source,
  va.statement_account_confidence,
  va.canonical_account_id,
  va.canonical_owner_label,
  va.canonical_account_platform,
  va.canonical_account_broker,
  va.canonical_account_label,
  va.canonical_account_scope,
  va.canonical_account_base_currency,
  va.account_mapping_type,
  va.account_mapping_confidence
FROM asset_movement_events ame
LEFT JOIN v_statement_accounts_with_canonical_account va
  ON va.import_run_id = ame.import_run_id
 AND va.statement_id = ame.statement_id;

DROP VIEW IF EXISTS v_position_snapshots_with_accounts;
CREATE VIEW v_position_snapshots_with_accounts AS
SELECT
  ps.*,
  va.raw_account_id,
  va.raw_account_label,
  va.raw_account_platform,
  va.raw_account_broker,
  va.raw_account_base_currency,
  va.statement_account_link_source,
  va.statement_account_confidence,
  va.canonical_account_id,
  va.canonical_owner_label,
  va.canonical_account_platform,
  va.canonical_account_broker,
  va.canonical_account_label,
  va.canonical_account_scope,
  va.canonical_account_base_currency,
  va.account_mapping_type,
  va.account_mapping_confidence
FROM position_snapshots ps
LEFT JOIN v_statement_accounts_with_canonical_account va
  ON va.import_run_id = ps.import_run_id
 AND va.statement_id = ps.statement_id;

DROP VIEW IF EXISTS v_statement_balance_snapshots_with_accounts;
CREATE VIEW v_statement_balance_snapshots_with_accounts AS
SELECT
  bs.*,
  va.raw_account_id,
  va.raw_account_label,
  va.raw_account_platform,
  va.raw_account_broker,
  va.raw_account_base_currency,
  va.statement_account_link_source,
  va.statement_account_confidence,
  va.canonical_account_id,
  va.canonical_owner_label,
  va.canonical_account_platform,
  va.canonical_account_broker,
  va.canonical_account_label,
  va.canonical_account_scope,
  va.canonical_account_base_currency,
  va.account_mapping_type,
  va.account_mapping_confidence
FROM statement_balance_snapshots bs
LEFT JOIN v_statement_accounts_with_canonical_account va
  ON va.import_run_id = bs.import_run_id
 AND va.statement_id = bs.statement_id;
