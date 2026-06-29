-- 富途结单原始事实层 v1
-- 目标：保存 PDF 抽取、parser v1 标准事实表、验收结果和导入运行元数据。
-- 约束：这里只保存原始事实、证据、治理和校验锚点，不保存 Lot、税务 treatment 或收益策略结果。

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS import_runs (
  import_run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  extractor_version TEXT NOT NULL,
  status TEXT NOT NULL,
  pdf_dir TEXT NOT NULL,
  pdf_glob TEXT NOT NULL,
  cache_dir TEXT NOT NULL,
  parser_out_dir TEXT NOT NULL,
  db_path TEXT NOT NULL,
  review_xlsx_path TEXT,
  statement_count INTEGER NOT NULL DEFAULT 0,
  acceptance_status TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS raw_statements (
  import_run_id TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  period TEXT NOT NULL,
  filename TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  pages INTEGER,
  source_file TEXT NOT NULL,
  PRIMARY KEY (import_run_id, statement_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS position_snapshots (
  import_run_id TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  period TEXT,
  filename TEXT,
  page INTEGER,
  table_index INTEGER,
  row_index INTEGER,
  section TEXT,
  snapshot_type TEXT,
  asset_category TEXT,
  code_name TEXT,
  market TEXT,
  currency TEXT,
  quantity NUMERIC,
  price NUMERIC,
  multiplier NUMERIC,
  market_value NUMERIC,
  price_date TEXT,
  pending_amount NUMERIC,
  initial_margin_requirement NUMERIC,
  maintenance_margin_requirement NUMERIC,
  maintenance_margin_rate TEXT,
  PRIMARY KEY (import_run_id, statement_id, page, table_index, row_index, section),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
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
  FOREIGN KEY (import_run_id, statement_id) REFERENCES raw_statements(import_run_id, statement_id)
);

CREATE TABLE IF NOT EXISTS cash_ledger_entries (
  import_run_id TEXT NOT NULL,
  cash_entry_id TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  period TEXT,
  filename TEXT,
  page INTEGER,
  event_date TEXT,
  business_type TEXT,
  cash_leg_type TEXT,
  direction_raw TEXT,
  event_type_raw TEXT,
  currency TEXT,
  amount NUMERIC,
  description TEXT,
  raw_line TEXT,
  source_refs TEXT,
  dedupe_status TEXT,
  source_count INTEGER,
  mapping_status TEXT,
  PRIMARY KEY (import_run_id, cash_entry_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS market_trades (
  import_run_id TEXT NOT NULL,
  trade_id TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  period TEXT,
  filename TEXT,
  page INTEGER,
  business_type TEXT,
  trade_datetime TEXT,
  trade_date TEXT,
  settlement_date TEXT,
  raw_direction TEXT,
  side TEXT,
  position_effect TEXT,
  market TEXT,
  currency TEXT,
  instrument_code_raw TEXT,
  instrument_symbol TEXT,
  instrument_name_raw TEXT,
  instrument_type TEXT,
  underlying_symbol TEXT,
  expiry_date TEXT,
  strike_price NUMERIC,
  option_type TEXT,
  quantity NUMERIC,
  quantity_unit TEXT,
  price NUMERIC,
  gross_amount NUMERIC,
  fee_total NUMERIC,
  net_cash_amount NUMERIC,
  source_refs TEXT,
  PRIMARY KEY (import_run_id, trade_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS market_trade_fee_items (
  import_run_id TEXT NOT NULL,
  fee_tax_item_id TEXT NOT NULL,
  trade_index TEXT,
  parent_event_id TEXT,
  parent_business_type TEXT,
  statement_id TEXT NOT NULL,
  period TEXT,
  fee_tax_type TEXT,
  raw_label TEXT,
  currency TEXT,
  amount_abs NUMERIC,
  source_ref TEXT,
  PRIMARY KEY (import_run_id, fee_tax_item_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS fund_orders (
  import_run_id TEXT NOT NULL,
  fund_order_id TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  period TEXT,
  fund_order_type TEXT,
  instrument_code TEXT,
  instrument_name_raw TEXT,
  currency TEXT,
  order_date TEXT,
  trade_date TEXT,
  quantity NUMERIC,
  price NUMERIC,
  fund_amount_abs NUMERIC,
  evidence_status TEXT,
  cash_match_status TEXT,
  cash_match_source_refs TEXT,
  evidence_row_ids TEXT,
  source_refs TEXT,
  PRIMARY KEY (import_run_id, fund_order_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS fund_order_cash_legs (
  import_run_id TEXT NOT NULL,
  fund_cash_leg_id TEXT NOT NULL,
  cash_entry_id TEXT,
  fund_order_id TEXT,
  statement_id TEXT NOT NULL,
  event_date TEXT,
  cash_leg_type TEXT,
  currency TEXT,
  cash_amount NUMERIC,
  match_status TEXT,
  candidate_order_ids TEXT,
  PRIMARY KEY (import_run_id, fund_cash_leg_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS fund_transactions (
  import_run_id TEXT NOT NULL,
  fund_transaction_row_id TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  period TEXT,
  filename TEXT,
  page INTEGER,
  source_ref TEXT,
  transaction_type_raw TEXT,
  fund_order_type TEXT,
  instrument_code TEXT,
  instrument_name_raw TEXT,
  currency TEXT,
  order_date TEXT,
  trade_date TEXT,
  quantity NUMERIC,
  price NUMERIC,
  amount_abs NUMERIC,
  evidence_status TEXT,
  raw_line TEXT,
  PRIMARY KEY (import_run_id, fund_transaction_row_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS fund_transaction_fee_lines (
  import_run_id TEXT NOT NULL,
  fund_fee_row_id TEXT NOT NULL,
  fund_transaction_row_id TEXT,
  statement_id TEXT NOT NULL,
  period TEXT,
  filename TEXT,
  page INTEGER,
  source_ref TEXT,
  fee_amount NUMERIC,
  subtotal NUMERIC,
  raw_line TEXT,
  mapping_status TEXT,
  PRIMARY KEY (import_run_id, fund_fee_row_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS corporate_action_cash_legs (
  import_run_id TEXT NOT NULL,
  corporate_action_cash_leg_id TEXT NOT NULL,
  cash_entry_id TEXT,
  statement_id TEXT NOT NULL,
  period TEXT,
  event_date TEXT,
  instrument_code_raw TEXT,
  instrument_mapping_status TEXT,
  corporate_action_group_type TEXT,
  corporate_action_type TEXT,
  currency TEXT,
  cash_amount NUMERIC,
  quantity_basis NUMERIC,
  rate_raw TEXT,
  description_raw TEXT,
  source_refs TEXT,
  dedupe_status TEXT,
  PRIMARY KEY (import_run_id, corporate_action_cash_leg_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS asset_movement_events (
  import_run_id TEXT NOT NULL,
  asset_movement_id TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  period TEXT,
  filename TEXT,
  page INTEGER,
  source_ref TEXT,
  business_type TEXT,
  asset_movement_type TEXT,
  event_date TEXT,
  direction_raw TEXT,
  event_type_raw TEXT,
  instrument_code_raw TEXT,
  currency TEXT,
  quantity NUMERIC,
  amount NUMERIC,
  description_raw TEXT,
  PRIMARY KEY (import_run_id, asset_movement_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS derivative_exercise_events (
  import_run_id TEXT NOT NULL,
  derivative_exercise_id TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  period TEXT,
  event_date TEXT,
  exercise_type TEXT,
  option_instrument_raw TEXT,
  cash_entry_id TEXT,
  cash_amount NUMERIC,
  asset_movement_ids TEXT,
  source_refs TEXT,
  description_raw TEXT,
  PRIMARY KEY (import_run_id, derivative_exercise_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS financing_interest_events (
  import_run_id TEXT NOT NULL,
  financing_interest_event_id TEXT NOT NULL,
  cash_entry_id TEXT,
  statement_id TEXT NOT NULL,
  period TEXT,
  cash_event_date TEXT,
  interest_type TEXT,
  period_label TEXT,
  currency TEXT,
  cash_amount NUMERIC,
  source_refs TEXT,
  PRIMARY KEY (import_run_id, financing_interest_event_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS financing_interest_evidence_items (
  import_run_id TEXT NOT NULL,
  financing_interest_evidence_id TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  period TEXT,
  evidence_date TEXT,
  currency TEXT,
  financing_amount NUMERIC,
  annual_rate_raw TEXT,
  daily_interest NUMERIC,
  cumulative_interest NUMERIC,
  source_ref TEXT,
  raw_line TEXT,
  PRIMARY KEY (import_run_id, financing_interest_evidence_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS stock_yield_daily_events (
  import_run_id TEXT NOT NULL,
  stock_yield_daily_id TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  period TEXT,
  source_ref TEXT,
  event_date TEXT,
  instrument_code_raw TEXT,
  market TEXT,
  currency TEXT,
  interest_type_raw TEXT,
  quantity NUMERIC,
  settlement_amount NUMERIC,
  collateral_amount NUMERIC,
  annual_rate_raw TEXT,
  interest_amount NUMERIC,
  cumulative_interest NUMERIC,
  income_month TEXT,
  PRIMARY KEY (import_run_id, stock_yield_daily_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS stock_yield_cash_entries (
  import_run_id TEXT NOT NULL,
  stock_yield_cash_id TEXT NOT NULL,
  cash_entry_id TEXT,
  statement_id TEXT NOT NULL,
  period TEXT,
  event_date TEXT,
  currency TEXT,
  cash_amount NUMERIC,
  description_raw TEXT,
  income_month_guess TEXT,
  reconciliation_status TEXT,
  source_refs TEXT,
  PRIMARY KEY (import_run_id, stock_yield_cash_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS parser_issues (
  import_run_id TEXT NOT NULL,
  issue_id TEXT NOT NULL,
  statement_id TEXT,
  source_ref TEXT,
  issue_type TEXT,
  severity TEXT,
  status TEXT,
  message TEXT,
  PRIMARY KEY (import_run_id, issue_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS unclassified_governance (
  import_run_id TEXT NOT NULL,
  unclassified_governance_id TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  period TEXT,
  source_ref TEXT,
  raw_row TEXT,
  default_classification TEXT,
  status TEXT,
  PRIMARY KEY (import_run_id, unclassified_governance_id),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS acceptance_tests (
  import_run_id TEXT NOT NULL,
  code TEXT NOT NULL,
  actual TEXT,
  expected TEXT,
  passed INTEGER NOT NULL,
  PRIMARY KEY (import_run_id, code),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE TABLE IF NOT EXISTS ingest_table_counts (
  import_run_id TEXT NOT NULL,
  table_name TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  PRIMARY KEY (import_run_id, table_name),
  FOREIGN KEY (import_run_id) REFERENCES import_runs(import_run_id)
);

CREATE INDEX IF NOT EXISTS idx_cash_ledger_statement_date
  ON cash_ledger_entries(import_run_id, statement_id, event_date, business_type, currency);

CREATE INDEX IF NOT EXISTS idx_statement_balance_snapshot
  ON statement_balance_snapshots(import_run_id, statement_id, snapshot_type, currency);

CREATE INDEX IF NOT EXISTS idx_market_trades_statement_date
  ON market_trades(import_run_id, statement_id, trade_date, instrument_symbol);

CREATE INDEX IF NOT EXISTS idx_fund_orders_statement_date
  ON fund_orders(import_run_id, statement_id, order_date, instrument_code);

CREATE INDEX IF NOT EXISTS idx_asset_movement_statement_date
  ON asset_movement_events(import_run_id, statement_id, event_date, business_type);

CREATE INDEX IF NOT EXISTS idx_parser_issues_status
  ON parser_issues(import_run_id, severity, status, issue_type);
