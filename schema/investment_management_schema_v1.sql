-- 个人投资数据库管理层 v1
-- 目标：在已验证的富途 raw fact schema 之上，新增可持续管理、人工修正、人工录入、计算与镜像所需的数据库对象。
-- 约束：raw fact 表继续保存结单原始事实；本管理层通过 overlay / view 承接修改和后续计算，不直接覆盖原始事实。

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  migration_id TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (datetime('now')),
  description TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_migrations (migration_id, description)
VALUES ('investment_management_schema_v1', '个人投资数据库管理层 v1：主数据、人工录入、修正、复核、treatment、计算和查询视图。');

CREATE TABLE IF NOT EXISTS database_metadata (
  metadata_key TEXT PRIMARY KEY,
  metadata_value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT
);

INSERT OR IGNORE INTO database_metadata (metadata_key, metadata_value, notes)
VALUES
  ('database_role', 'primary_investment_database', '数据库主源；Excel 仅作为审阅/导出产物。'),
  ('raw_fact_policy', 'append_or_replace_by_import_run', '原始事实按导入批次管理，不通过人工修正直接覆盖。'),
  ('manual_change_policy', 'overlay_tables_only', '人工修正和补录写入 overlay 管理表。'),
  ('base_mirror_policy', 'db_to_base_one_way', '飞书 Base 仅作为数据库到 Base 的单向可读镜像。');

CREATE TABLE IF NOT EXISTS accounts (
  account_id TEXT PRIMARY KEY,
  owner_label TEXT,
  platform TEXT NOT NULL,
  broker TEXT,
  account_label TEXT NOT NULL,
  base_currency TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT
);

INSERT OR IGNORE INTO accounts (
  account_id, owner_label, platform, broker, account_label, base_currency, status, notes
)
VALUES (
  'futu_hk_main',
  'personal',
  'futu',
  '富途',
  '富途港股主账户',
  'HKD',
  'active',
  '由 2025 年富途月结单初始化；不在数据库中保存敏感账号。'
);

CREATE TABLE IF NOT EXISTS statement_accounts (
  import_run_id TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  link_source TEXT NOT NULL DEFAULT 'default_futu_hk_main',
  confidence TEXT NOT NULL DEFAULT 'inferred',
  notes TEXT,
  PRIMARY KEY (import_run_id, statement_id, account_id),
  FOREIGN KEY (account_id) REFERENCES accounts(account_id),
  FOREIGN KEY (import_run_id, statement_id) REFERENCES raw_statements(import_run_id, statement_id)
);

CREATE TABLE IF NOT EXISTS business_type_catalog (
  business_type TEXT PRIMARY KEY,
  cn_name TEXT NOT NULL,
  definition TEXT NOT NULL,
  calculation_role TEXT NOT NULL,
  source_status TEXT NOT NULL DEFAULT 'active',
  sort_order INTEGER NOT NULL DEFAULT 999
);

INSERT OR IGNORE INTO business_type_catalog (business_type, cn_name, definition, calculation_role, source_status, sort_order)
VALUES
  ('external_transfer', '外部出入金', '账户与外部银行/其他账户之间的现金进出。', 'cash_flow', 'active', 10),
  ('market_trade', '二级市场交易', '股票、ETF、期权等二级市场买卖。', 'trade_event', 'active', 20),
  ('fund_order', '基金订单', '基金申购、赎回及其现金腿。', 'fund_event', 'active', 30),
  ('ipo_subscription', 'IPO 申购', '港股打新申购、退款、手续费、配发等链路。', 'ipo_event', 'active', 40),
  ('corporate_action', '公司行动', '股息、预扣税、ADR fee、公司行动手续费等。', 'corporate_action_event', 'active', 50),
  ('financing_interest', '融资利息', '融资借款或保证金相关利息扣除。', 'financing_cost', 'active', 60),
  ('securities_lending_income', '股票收益计划', '股票收益计划产生的日度收益和现金入账。', 'income_event', 'active', 70),
  ('derivative_exercise', '衍生品行权/到期', '期权到期、指派、行权等现金腿和资产腿。', 'derivative_event', 'active', 80),
  ('broker_reward', '券商奖励', '卡券、奖励金等券商奖励入账。', 'other_income', 'active', 90),
  ('fee_tax_charge', '独立费用/税费', '无法归属到具体父事件时的独立费用或税费。', 'fee_tax_event', 'future', 100),
  ('fx_conversion', '换汇', '账户内货币兑换。', 'cash_flow', 'future', 110),
  ('internal_transfer', '内部划转', '同一平台或同一主体账户之间的内部划转。', 'cash_flow', 'future', 120),
  ('asset_transfer', '资产转入转出', '非交易导致的证券、基金或其他资产迁移。', 'asset_flow', 'future', 130),
  ('cash_interest', '现金利息', '现金余额产生的利息收入或扣费。', 'income_event', 'future', 140),
  ('cash_adjustment', '现金调整', '券商冲正、尾差调整、人工调整等。', 'adjustment_event', 'future', 150);

CREATE TABLE IF NOT EXISTS cash_leg_type_catalog (
  cash_leg_type TEXT PRIMARY KEY,
  business_type TEXT,
  cn_name TEXT NOT NULL,
  definition TEXT NOT NULL,
  source_status TEXT NOT NULL DEFAULT 'active',
  sort_order INTEGER NOT NULL DEFAULT 999,
  FOREIGN KEY (business_type) REFERENCES business_type_catalog(business_type)
);

INSERT OR IGNORE INTO cash_leg_type_catalog (cash_leg_type, business_type, cn_name, definition, source_status, sort_order)
VALUES
  ('deposit', 'external_transfer', '入金', '外部现金流入。', 'active', 10),
  ('withdrawal', 'external_transfer', '出金', '外部现金流出。', 'active', 20),
  ('subscription_cash_out', 'fund_order', '基金申购现金流出', '基金申购对应的现金扣款。', 'active', 30),
  ('redemption_cash_in', 'fund_order', '基金赎回现金流入', '基金赎回对应的现金入账。', 'active', 40),
  ('application_handling_fee', 'ipo_subscription', 'IPO 申购手续费', 'IPO 申购显式手续费。', 'active', 50),
  ('application_payment', 'ipo_subscription', 'IPO 申购款', 'IPO 申购冻结/扣款。', 'active', 60),
  ('refund', 'ipo_subscription', 'IPO 退款', 'IPO 未中签或部分中签后返还现金。', 'active', 70),
  ('ipo_cash_other', 'ipo_subscription', 'IPO 其他现金腿', 'IPO 链路中未细分的其他现金腿。', 'active', 80),
  ('cash_dividend', 'corporate_action', '现金股息', '公司行动产生的现金股息。', 'active', 90),
  ('withholding_tax', 'corporate_action', '预扣税', '股息或公司行动相关预扣税。', 'active', 100),
  ('adr_fee', 'corporate_action', 'ADR 费用', 'ADR 托管或相关费用。', 'active', 110),
  ('corporate_action_handling_charge', 'corporate_action', '公司行动手续费', '公司行动处理手续费。', 'active', 120),
  ('other_corporate_action_cash', 'corporate_action', '其他公司行动现金腿', '未细分公司行动现金腿。', 'active', 130),
  ('interest_charge', 'financing_interest', '利息扣除', '融资或保证金利息扣款。', 'active', 140),
  ('income_received', 'securities_lending_income', '收益入账', '股票收益计划现金入账。', 'active', 150),
  ('reward_cash_in', 'broker_reward', '奖励入账', '券商奖励现金入账。', 'active', 160),
  ('exercise_cash_effect', 'derivative_exercise', '行权/到期现金影响', '期权到期、指派、行权产生的现金腿，含 0 金额记录。', 'active', 170),
  ('unknown', NULL, '未知现金腿', '解析器无法确定的现金腿；应进入复核。', 'fallback', 999);

CREATE TABLE IF NOT EXISTS fee_tax_type_catalog (
  fee_tax_type TEXT PRIMARY KEY,
  cn_name TEXT NOT NULL,
  definition TEXT NOT NULL,
  source_status TEXT NOT NULL DEFAULT 'active',
  sort_order INTEGER NOT NULL DEFAULT 999
);

INSERT OR IGNORE INTO fee_tax_type_catalog (fee_tax_type, cn_name, definition, source_status, sort_order)
VALUES
  ('commission', '佣金', '券商交易佣金。', 'active', 10),
  ('platform_fee', '平台使用费', '券商平台使用费。', 'active', 20),
  ('settlement_fee', '交收费', '交易交收相关费用。', 'active', 30),
  ('taf', '交易活动费', '美国交易活动费等。', 'active', 40),
  ('stamp_duty', '印花税', '交易印花税。', 'active', 50),
  ('option_regulatory_fee', '期权监管费', '期权监管相关费用。', 'active', 60),
  ('trading_fee', '交易费', '交易所交易费。', 'active', 70),
  ('option_clearing_fee', '期权清算费', '期权清算相关费用。', 'active', 80),
  ('sec_fee', '证监会规费', 'SEC 或同类监管规费。', 'active', 90),
  ('sfc_levy', '证监会征费', '香港证监会征费。', 'active', 100),
  ('frc_levy', '财汇局征费', '香港财汇局征费。', 'active', 110),
  ('option_settlement_fee', '期权交收费', '期权交收费用。', 'active', 120),
  ('trading_system_fee', '交易系统使用费', '交易系统使用相关费用。', 'active', 130),
  ('cat_fee', '综合审计跟踪监管费', 'CAT 等监管费用。', 'active', 140),
  ('unknown_fee', '未知费用', '解析器无法归类的费用标签。', 'fallback', 999);

CREATE TABLE IF NOT EXISTS instruments (
  instrument_id TEXT PRIMARY KEY,
  market TEXT,
  instrument_code TEXT,
  symbol TEXT,
  display_name TEXT,
  instrument_type TEXT,
  currency TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  source TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT,
  UNIQUE (market, instrument_code)
);

CREATE TABLE IF NOT EXISTS instrument_aliases (
  alias_id TEXT PRIMARY KEY,
  instrument_id TEXT,
  alias_type TEXT NOT NULL,
  alias_value TEXT NOT NULL,
  market TEXT,
  source TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  notes TEXT,
  UNIQUE (alias_type, alias_value, market),
  FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);

CREATE TABLE IF NOT EXISTS business_type_mapping_overrides (
  override_id TEXT PRIMARY KEY,
  platform TEXT NOT NULL DEFAULT 'futu',
  raw_type TEXT NOT NULL,
  description_pattern TEXT,
  business_type TEXT NOT NULL,
  subtype TEXT,
  priority INTEGER NOT NULL DEFAULT 100,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT,
  FOREIGN KEY (business_type) REFERENCES business_type_catalog(business_type)
);

CREATE TABLE IF NOT EXISTS instrument_mapping_overrides (
  override_id TEXT PRIMARY KEY,
  platform TEXT NOT NULL DEFAULT 'futu',
  raw_instrument_text TEXT NOT NULL,
  statement_id TEXT,
  market TEXT,
  instrument_code TEXT,
  instrument_id TEXT,
  confidence TEXT NOT NULL DEFAULT 'manual',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT,
  FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);

CREATE TABLE IF NOT EXISTS review_items (
  review_item_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  status TEXT NOT NULL DEFAULT 'open',
  severity TEXT NOT NULL DEFAULT 'review',
  import_run_id TEXT,
  statement_id TEXT,
  event_date TEXT,
  source_table TEXT,
  source_pk TEXT,
  source_ref TEXT,
  issue_type TEXT,
  title TEXT NOT NULL,
  detail TEXT,
  proposed_action TEXT,
  resolution_notes TEXT,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS manual_corrections (
  correction_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  status TEXT NOT NULL DEFAULT 'active',
  target_table TEXT NOT NULL,
  target_pk TEXT NOT NULL,
  target_field TEXT NOT NULL,
  original_value TEXT,
  corrected_value TEXT NOT NULL,
  value_type TEXT NOT NULL DEFAULT 'text',
  reason TEXT NOT NULL,
  reviewer TEXT,
  effective_from TEXT,
  effective_to TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS manual_events (
  manual_event_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  business_type TEXT NOT NULL,
  event_subtype TEXT,
  currency TEXT,
  amount NUMERIC,
  description TEXT,
  source_label TEXT,
  source_ref TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT,
  FOREIGN KEY (account_id) REFERENCES accounts(account_id),
  FOREIGN KEY (business_type) REFERENCES business_type_catalog(business_type)
);

CREATE TABLE IF NOT EXISTS manual_event_legs (
  manual_leg_id TEXT PRIMARY KEY,
  manual_event_id TEXT NOT NULL,
  leg_role TEXT NOT NULL,
  asset_kind TEXT,
  instrument_id TEXT,
  instrument_code_raw TEXT,
  currency TEXT,
  quantity NUMERIC,
  amount NUMERIC,
  unit_price NUMERIC,
  direction TEXT,
  notes TEXT,
  FOREIGN KEY (manual_event_id) REFERENCES manual_events(manual_event_id),
  FOREIGN KEY (instrument_id) REFERENCES instruments(instrument_id)
);

CREATE TABLE IF NOT EXISTS statement_balance_snapshots (
  import_run_id TEXT NOT NULL,
  balance_snapshot_id TEXT NOT NULL,
  account_id TEXT,
  statement_id TEXT NOT NULL,
  period TEXT,
  snapshot_type TEXT NOT NULL,
  currency TEXT NOT NULL,
  reported_balance NUMERIC NOT NULL,
  source_ref TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  notes TEXT,
  PRIMARY KEY (import_run_id, balance_snapshot_id),
  FOREIGN KEY (account_id) REFERENCES accounts(account_id),
  FOREIGN KEY (import_run_id, statement_id) REFERENCES raw_statements(import_run_id, statement_id)
);

CREATE TABLE IF NOT EXISTS continuity_check_runs (
  continuity_run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  import_run_id TEXT,
  status TEXT NOT NULL,
  check_scope TEXT NOT NULL,
  item_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  warning_count INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS continuity_check_items (
  continuity_item_id TEXT PRIMARY KEY,
  continuity_run_id TEXT NOT NULL,
  check_code TEXT NOT NULL,
  check_scope TEXT NOT NULL,
  severity TEXT NOT NULL,
  status TEXT NOT NULL,
  period_from TEXT,
  period_to TEXT,
  statement_id TEXT,
  account_id TEXT,
  instrument_key TEXT,
  currency TEXT,
  expected_value NUMERIC,
  actual_value NUMERIC,
  difference NUMERIC,
  tolerance NUMERIC,
  source_table TEXT,
  source_pk TEXT,
  detail_json TEXT,
  notes TEXT,
  FOREIGN KEY (continuity_run_id) REFERENCES continuity_check_runs(continuity_run_id)
);

CREATE TABLE IF NOT EXISTS treatment_profiles (
  profile_id TEXT PRIMARY KEY,
  profile_name TEXT NOT NULL,
  definition TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT
);

INSERT OR IGNORE INTO treatment_profiles (profile_id, profile_name, definition, status, is_default, notes)
VALUES
  ('default_economic', '默认经济口径', '费用、税费和可解释差额进入总经济收益，但分门别类展示。', 'active', 1, '当前默认口径。'),
  ('decision_view', '个人交易决策口径', '交易收益与刚性费用可分开展示，避免沉没成本干扰交易复盘。', 'active', 0, '用于个人复盘。'),
  ('tax_preparation', '税务准备口径', '为后续报税准备字段与证据，不在原始事实层直接给出最终税法判断。', 'active', 0, '需结合税务辖区继续设计。');

CREATE TABLE IF NOT EXISTS treatment_assignments (
  assignment_id TEXT PRIMARY KEY,
  profile_id TEXT NOT NULL,
  target_table TEXT NOT NULL,
  target_pk TEXT NOT NULL,
  treatment_type TEXT NOT NULL,
  amount_policy TEXT,
  cost_basis_policy TEXT,
  tax_policy TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT,
  FOREIGN KEY (profile_id) REFERENCES treatment_profiles(profile_id)
);

CREATE TABLE IF NOT EXISTS calculation_runs (
  calculation_run_id TEXT PRIMARY KEY,
  profile_id TEXT,
  calculation_type TEXT NOT NULL,
  input_import_run_id TEXT,
  status TEXT NOT NULL DEFAULT 'created',
  started_at TEXT,
  finished_at TEXT,
  code_version TEXT,
  notes TEXT,
  FOREIGN KEY (profile_id) REFERENCES treatment_profiles(profile_id)
);

CREATE TABLE IF NOT EXISTS calculation_results (
  calculation_result_id TEXT PRIMARY KEY,
  calculation_run_id TEXT NOT NULL,
  result_type TEXT NOT NULL,
  target_table TEXT,
  target_pk TEXT,
  currency TEXT,
  amount NUMERIC,
  quantity NUMERIC,
  result_json TEXT,
  notes TEXT,
  FOREIGN KEY (calculation_run_id) REFERENCES calculation_runs(calculation_run_id)
);

CREATE TABLE IF NOT EXISTS base_mirror_exports (
  export_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  target_name TEXT NOT NULL,
  source_import_run_id TEXT,
  status TEXT NOT NULL DEFAULT 'created',
  exported_table TEXT,
  exported_rows INTEGER,
  output_path TEXT,
  notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_statement_accounts_account
  ON statement_accounts(account_id, import_run_id, statement_id);

CREATE INDEX IF NOT EXISTS idx_review_items_status
  ON review_items(status, severity, source_table);

CREATE INDEX IF NOT EXISTS idx_manual_events_date_type
  ON manual_events(account_id, event_date, business_type, currency);

CREATE INDEX IF NOT EXISTS idx_manual_corrections_target
  ON manual_corrections(target_table, target_pk, target_field, status);

CREATE INDEX IF NOT EXISTS idx_statement_balance_snapshot
  ON statement_balance_snapshots(import_run_id, statement_id, snapshot_type, currency);

CREATE INDEX IF NOT EXISTS idx_continuity_check_items_run_status
  ON continuity_check_items(continuity_run_id, status, severity, check_code);

CREATE INDEX IF NOT EXISTS idx_treatment_assignments_target
  ON treatment_assignments(profile_id, target_table, target_pk, status);

CREATE VIEW IF NOT EXISTS v_latest_import_run AS
SELECT *
FROM import_runs
WHERE created_at = (SELECT MAX(created_at) FROM import_runs);

CREATE VIEW IF NOT EXISTS v_import_run_summary AS
SELECT
  r.import_run_id,
  r.created_at,
  r.status,
  r.statement_count,
  r.acceptance_status,
  COALESCE(SUM(CASE WHEN t.table_name = 'cash_ledger_entries' THEN t.row_count END), 0) AS cash_rows,
  COALESCE(SUM(CASE WHEN t.table_name = 'market_trades' THEN t.row_count END), 0) AS market_trade_rows,
  COALESCE(SUM(CASE WHEN t.table_name = 'fund_orders' THEN t.row_count END), 0) AS fund_order_rows,
  COALESCE(SUM(CASE WHEN t.table_name = 'corporate_action_cash_legs' THEN t.row_count END), 0) AS corporate_action_rows
FROM import_runs r
LEFT JOIN ingest_table_counts t ON t.import_run_id = r.import_run_id
GROUP BY r.import_run_id, r.created_at, r.status, r.statement_count, r.acceptance_status;

CREATE VIEW IF NOT EXISTS v_cash_timeline AS
SELECT
  c.import_run_id,
  COALESCE(sa.account_id, 'futu_hk_main') AS account_id,
  'raw_cash' AS source_kind,
  c.cash_entry_id AS event_id,
  c.statement_id,
  c.period,
  c.event_date,
  c.business_type,
  c.cash_leg_type AS event_subtype,
  c.currency,
  c.amount,
  c.description,
  c.source_refs,
  c.mapping_status,
  c.dedupe_status,
  0 AS is_manual,
  NULL AS manual_status
FROM cash_ledger_entries c
LEFT JOIN statement_accounts sa
  ON sa.import_run_id = c.import_run_id AND sa.statement_id = c.statement_id
UNION ALL
SELECT
  NULL AS import_run_id,
  m.account_id,
  'manual_event' AS source_kind,
  m.manual_event_id AS event_id,
  NULL AS statement_id,
  substr(m.event_date, 1, 7) AS period,
  m.event_date,
  m.business_type,
  m.event_subtype,
  m.currency,
  m.amount,
  m.description,
  m.source_ref AS source_refs,
  'manual' AS mapping_status,
  'manual' AS dedupe_status,
  1 AS is_manual,
  m.status AS manual_status
FROM manual_events m
WHERE m.amount IS NOT NULL;

CREATE VIEW IF NOT EXISTS v_event_timeline AS
SELECT
  'cash_ledger_entries' AS source_table,
  c.cash_entry_id AS event_id,
  c.import_run_id,
  COALESCE(sa.account_id, 'futu_hk_main') AS account_id,
  c.statement_id,
  c.period,
  c.event_date,
  c.business_type,
  c.cash_leg_type AS event_subtype,
  NULL AS instrument_code,
  c.currency,
  NULL AS quantity,
  c.amount,
  c.description,
  c.source_refs
FROM cash_ledger_entries c
LEFT JOIN statement_accounts sa
  ON sa.import_run_id = c.import_run_id AND sa.statement_id = c.statement_id
UNION ALL
SELECT
  'market_trades' AS source_table,
  t.trade_id AS event_id,
  t.import_run_id,
  COALESCE(sa.account_id, 'futu_hk_main') AS account_id,
  t.statement_id,
  t.period,
  t.trade_date AS event_date,
  t.business_type,
  t.side || ':' || t.position_effect AS event_subtype,
  COALESCE(t.instrument_symbol, t.instrument_code_raw) AS instrument_code,
  t.currency,
  t.quantity,
  t.net_cash_amount AS amount,
  t.raw_direction || ' ' || COALESCE(t.instrument_name_raw, '') AS description,
  t.source_refs
FROM market_trades t
LEFT JOIN statement_accounts sa
  ON sa.import_run_id = t.import_run_id AND sa.statement_id = t.statement_id
UNION ALL
SELECT
  'fund_orders' AS source_table,
  f.fund_order_id AS event_id,
  f.import_run_id,
  COALESCE(sa.account_id, 'futu_hk_main') AS account_id,
  f.statement_id,
  f.period,
  COALESCE(f.trade_date, f.order_date) AS event_date,
  'fund_order' AS business_type,
  f.fund_order_type AS event_subtype,
  f.instrument_code AS instrument_code,
  f.currency,
  f.quantity,
  f.fund_amount_abs AS amount,
  f.instrument_name_raw AS description,
  f.source_refs
FROM fund_orders f
LEFT JOIN statement_accounts sa
  ON sa.import_run_id = f.import_run_id AND sa.statement_id = f.statement_id
UNION ALL
SELECT
  'corporate_action_cash_legs' AS source_table,
  ca.corporate_action_cash_leg_id AS event_id,
  ca.import_run_id,
  COALESCE(sa.account_id, 'futu_hk_main') AS account_id,
  ca.statement_id,
  ca.period,
  ca.event_date,
  'corporate_action' AS business_type,
  ca.corporate_action_type AS event_subtype,
  ca.instrument_code_raw AS instrument_code,
  ca.currency,
  ca.quantity_basis AS quantity,
  ca.cash_amount AS amount,
  ca.description_raw AS description,
  ca.source_refs
FROM corporate_action_cash_legs ca
LEFT JOIN statement_accounts sa
  ON sa.import_run_id = ca.import_run_id AND sa.statement_id = ca.statement_id
UNION ALL
SELECT
  'asset_movement_events' AS source_table,
  a.asset_movement_id AS event_id,
  a.import_run_id,
  COALESCE(sa.account_id, 'futu_hk_main') AS account_id,
  a.statement_id,
  a.period,
  a.event_date,
  a.business_type,
  a.asset_movement_type AS event_subtype,
  a.instrument_code_raw AS instrument_code,
  a.currency,
  a.quantity,
  a.amount,
  a.description_raw AS description,
  a.source_ref AS source_refs
FROM asset_movement_events a
LEFT JOIN statement_accounts sa
  ON sa.import_run_id = a.import_run_id AND sa.statement_id = a.statement_id
UNION ALL
SELECT
  'manual_events' AS source_table,
  m.manual_event_id AS event_id,
  NULL AS import_run_id,
  m.account_id,
  NULL AS statement_id,
  substr(m.event_date, 1, 7) AS period,
  m.event_date,
  m.business_type,
  m.event_subtype,
  NULL AS instrument_code,
  m.currency,
  NULL AS quantity,
  m.amount,
  m.description,
  m.source_ref AS source_refs
FROM manual_events m
WHERE m.status = 'active';

CREATE VIEW IF NOT EXISTS v_monthly_cash_by_business_type AS
SELECT
  account_id,
  substr(event_date, 1, 7) AS month,
  business_type,
  currency,
  COUNT(*) AS row_count,
  ROUND(SUM(amount), 2) AS amount_sum
FROM v_cash_timeline
WHERE event_date IS NOT NULL
GROUP BY account_id, substr(event_date, 1, 7), business_type, currency;

CREATE VIEW IF NOT EXISTS v_market_trade_fee_check AS
SELECT
  t.import_run_id,
  t.statement_id,
  t.trade_id,
  t.trade_date,
  COALESCE(t.instrument_symbol, t.instrument_code_raw) AS instrument_code,
  t.currency,
  t.gross_amount,
  t.net_cash_amount,
  t.fee_total,
  ROUND(COALESCE(SUM(f.amount_abs), 0), 2) AS fee_item_sum,
  CASE
    WHEN t.fee_total IS NULL THEN 'no_trade_fee_total'
    WHEN ROUND(COALESCE(SUM(f.amount_abs), 0), 2) = ROUND(t.fee_total, 2) THEN 'ok'
    ELSE 'mismatch'
  END AS check_status
FROM market_trades t
LEFT JOIN market_trade_fee_items f
  ON f.import_run_id = t.import_run_id AND f.parent_event_id = t.trade_id
GROUP BY
  t.import_run_id, t.statement_id, t.trade_id, t.trade_date,
  t.instrument_symbol, t.instrument_code_raw, t.currency,
  t.gross_amount, t.net_cash_amount, t.fee_total;

CREATE VIEW IF NOT EXISTS v_fund_cash_match_status AS
SELECT
  f.import_run_id,
  f.statement_id,
  f.fund_order_id,
  f.fund_order_type,
  f.instrument_code,
  f.currency,
  f.order_date,
  f.trade_date,
  f.fund_amount_abs,
  f.cash_match_status,
  COUNT(l.fund_cash_leg_id) AS linked_cash_leg_count,
  ROUND(COALESCE(SUM(l.cash_amount), 0), 2) AS linked_cash_amount_sum
FROM fund_orders f
LEFT JOIN fund_order_cash_legs l
  ON l.import_run_id = f.import_run_id AND l.fund_order_id = f.fund_order_id
GROUP BY
  f.import_run_id, f.statement_id, f.fund_order_id, f.fund_order_type,
  f.instrument_code, f.currency, f.order_date, f.trade_date,
  f.fund_amount_abs, f.cash_match_status;

CREATE VIEW IF NOT EXISTS v_open_review_items AS
SELECT
  'parser_issues' AS source_table,
  p.issue_id AS review_id,
  p.import_run_id,
  p.statement_id,
  p.source_ref,
  p.issue_type,
  p.severity,
  p.status,
  p.message AS detail
FROM parser_issues p
WHERE
  LOWER(COALESCE(p.severity, '')) IN ('blocker', 'error', 'critical', 'needs_review')
  OR LOWER(COALESCE(p.status, '')) IN ('open', 'ambiguous', 'unmatched')
UNION ALL
SELECT
  'review_items' AS source_table,
  r.review_item_id AS review_id,
  r.import_run_id,
  r.statement_id,
  r.source_ref,
  r.issue_type,
  r.severity,
  r.status,
  COALESCE(r.title, '') || CASE WHEN r.detail IS NULL THEN '' ELSE ': ' || r.detail END AS detail
FROM review_items r
WHERE LOWER(COALESCE(r.status, '')) IN ('open', 'in_progress', 'needs_review');

CREATE VIEW IF NOT EXISTS v_latest_continuity_summary AS
SELECT
  r.continuity_run_id,
  r.created_at,
  r.import_run_id,
  r.status,
  r.check_scope,
  r.item_count,
  r.failed_count,
  r.warning_count
FROM continuity_check_runs r
WHERE r.created_at = (SELECT MAX(created_at) FROM continuity_check_runs);

CREATE VIEW IF NOT EXISTS v_instrument_candidates AS
SELECT DISTINCT
  market,
  COALESCE(instrument_symbol, instrument_code_raw) AS instrument_code,
  instrument_name_raw AS display_name,
  instrument_type,
  currency,
  'market_trades' AS source_table
FROM market_trades
WHERE COALESCE(instrument_symbol, instrument_code_raw) IS NOT NULL
UNION
SELECT DISTINCT
  NULL AS market,
  instrument_code,
  instrument_name_raw AS display_name,
  'fund' AS instrument_type,
  currency,
  'fund_orders' AS source_table
FROM fund_orders
WHERE instrument_code IS NOT NULL
UNION
SELECT DISTINCT
  NULL AS market,
  instrument_code_raw AS instrument_code,
  NULL AS display_name,
  'corporate_action_underlying' AS instrument_type,
  currency,
  'corporate_action_cash_legs' AS source_table
FROM corporate_action_cash_legs
WHERE instrument_code_raw IS NOT NULL
UNION
SELECT DISTINCT
  market,
  code_name AS instrument_code,
  code_name AS display_name,
  asset_category AS instrument_type,
  currency,
  'position_snapshots' AS source_table
FROM position_snapshots
WHERE code_name IS NOT NULL;
