-- Lot / Allocation 计算层 v1
-- 目标：在 raw fact + management layer 之上生成可复跑的持仓批次、成本组件、FIFO 分配和收益汇总。
-- 当前范围：正股 / ETF long position、IPO 中签配发、二级市场买入与卖出平仓、期权、基金、股票短仓。

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations (migration_id, description)
VALUES ('lot_allocation_schema_v1', 'Lot / Allocation 计算层 v1：position lots、cost components、FIFO allocations、validation items 和收益视图。');

CREATE TABLE IF NOT EXISTS lot_allocation_runs (
  allocation_run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  import_run_id TEXT,
  account_id TEXT NOT NULL,
  method TEXT NOT NULL DEFAULT 'fifo',
  scope TEXT NOT NULL DEFAULT 'stock_and_ipo_v1',
  status TEXT NOT NULL DEFAULT 'created',
  opening_cost_policy TEXT NOT NULL DEFAULT 'first_statement_market_value',
  ipo_fee_policy TEXT NOT NULL DEFAULT 'handling_fee_plus_allotment_amount_times_1_0085_percent',
  notes TEXT,
  FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE TABLE IF NOT EXISTS position_lots (
  allocation_run_id TEXT NOT NULL,
  lot_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  instrument_key TEXT NOT NULL,
  instrument_code TEXT,
  instrument_name TEXT,
  market TEXT,
  currency TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_table TEXT NOT NULL,
  source_pk TEXT NOT NULL,
  source_ref TEXT,
  open_date TEXT,
  settlement_date TEXT,
  original_quantity NUMERIC NOT NULL,
  remaining_quantity NUMERIC NOT NULL,
  cost_basis_total NUMERIC NOT NULL,
  cost_basis_principal NUMERIC NOT NULL,
  cost_basis_fee NUMERIC NOT NULL DEFAULT 0,
  cost_basis_currency TEXT NOT NULL,
  cost_basis_status TEXT NOT NULL,
  cost_basis_source TEXT NOT NULL,
  unit_cost NUMERIC NOT NULL,
  lot_status TEXT NOT NULL DEFAULT 'open',
  notes TEXT,
  PRIMARY KEY (allocation_run_id, lot_id),
  FOREIGN KEY (allocation_run_id) REFERENCES lot_allocation_runs(allocation_run_id),
  FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE INDEX IF NOT EXISTS idx_position_lots_run_instrument
ON position_lots (allocation_run_id, instrument_key, open_date, lot_id);

CREATE TABLE IF NOT EXISTS lot_cost_components (
  allocation_run_id TEXT NOT NULL,
  component_id TEXT NOT NULL,
  lot_id TEXT NOT NULL,
  component_type TEXT NOT NULL,
  amount NUMERIC NOT NULL,
  currency TEXT NOT NULL,
  source_table TEXT,
  source_pk TEXT,
  source_ref TEXT,
  cost_treatment TEXT NOT NULL DEFAULT 'capitalized_to_lot',
  formula TEXT,
  notes TEXT,
  PRIMARY KEY (allocation_run_id, component_id),
  FOREIGN KEY (allocation_run_id, lot_id) REFERENCES position_lots(allocation_run_id, lot_id)
);

CREATE INDEX IF NOT EXISTS idx_lot_cost_components_lot
ON lot_cost_components (allocation_run_id, lot_id, component_type);

CREATE TABLE IF NOT EXISTS lot_allocations (
  allocation_run_id TEXT NOT NULL,
  allocation_id TEXT NOT NULL,
  close_event_table TEXT NOT NULL,
  close_event_id TEXT NOT NULL,
  close_event_date TEXT,
  close_settlement_date TEXT,
  close_source_ref TEXT,
  instrument_key TEXT NOT NULL,
  instrument_code TEXT,
  instrument_name TEXT,
  currency TEXT NOT NULL,
  lot_id TEXT NOT NULL,
  allocation_method TEXT NOT NULL DEFAULT 'fifo',
  quantity_allocated NUMERIC NOT NULL,
  proceeds_allocated NUMERIC NOT NULL,
  cost_allocated NUMERIC NOT NULL,
  realized_pnl NUMERIC NOT NULL,
  cost_basis_status TEXT NOT NULL,
  pnl_status TEXT NOT NULL,
  notes TEXT,
  PRIMARY KEY (allocation_run_id, allocation_id),
  FOREIGN KEY (allocation_run_id, lot_id) REFERENCES position_lots(allocation_run_id, lot_id)
);

CREATE INDEX IF NOT EXISTS idx_lot_allocations_run_instrument
ON lot_allocations (allocation_run_id, instrument_key, close_event_date, close_event_id);

CREATE TABLE IF NOT EXISTS lot_allocation_validation_items (
  allocation_run_id TEXT NOT NULL,
  validation_item_id TEXT NOT NULL,
  check_code TEXT NOT NULL,
  status TEXT NOT NULL,
  severity TEXT NOT NULL,
  instrument_key TEXT,
  source_table TEXT,
  source_pk TEXT,
  expected_value NUMERIC,
  actual_value NUMERIC,
  diff_value NUMERIC,
  message TEXT NOT NULL,
  notes TEXT,
  PRIMARY KEY (allocation_run_id, validation_item_id),
  FOREIGN KEY (allocation_run_id) REFERENCES lot_allocation_runs(allocation_run_id)
);

CREATE INDEX IF NOT EXISTS idx_lot_validation_run_status
ON lot_allocation_validation_items (allocation_run_id, status, severity, check_code);

CREATE TABLE IF NOT EXISTS option_contract_lots (
  allocation_run_id TEXT NOT NULL,
  option_lot_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  option_contract_key TEXT NOT NULL,
  option_code TEXT NOT NULL,
  underlying_symbol TEXT,
  underlying_instrument_key TEXT,
  expiry_date TEXT,
  strike_price NUMERIC,
  option_type TEXT,
  contract_multiplier NUMERIC,
  market TEXT,
  currency TEXT NOT NULL,
  position_side TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_table TEXT NOT NULL,
  source_pk TEXT NOT NULL,
  source_ref TEXT,
  open_date TEXT,
  settlement_date TEXT,
  original_contracts NUMERIC NOT NULL,
  remaining_contracts NUMERIC NOT NULL,
  opening_net_cash_amount NUMERIC NOT NULL,
  remaining_opening_cash_amount NUMERIC NOT NULL,
  opening_gross_amount NUMERIC,
  opening_fee_total NUMERIC,
  premium_status TEXT NOT NULL DEFAULT 'open',
  notes TEXT,
  PRIMARY KEY (allocation_run_id, option_lot_id),
  FOREIGN KEY (allocation_run_id) REFERENCES lot_allocation_runs(allocation_run_id),
  FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE INDEX IF NOT EXISTS idx_option_contract_lots_run_contract
ON option_contract_lots (allocation_run_id, option_contract_key, position_side, open_date, option_lot_id);

CREATE TABLE IF NOT EXISTS option_lot_allocations (
  allocation_run_id TEXT NOT NULL,
  option_allocation_id TEXT NOT NULL,
  option_lot_id TEXT NOT NULL,
  option_contract_key TEXT NOT NULL,
  option_code TEXT NOT NULL,
  underlying_symbol TEXT,
  underlying_instrument_key TEXT,
  expiry_date TEXT,
  strike_price NUMERIC,
  option_type TEXT,
  currency TEXT NOT NULL,
  position_side TEXT NOT NULL,
  close_event_type TEXT NOT NULL,
  close_outcome TEXT NOT NULL,
  close_event_table TEXT NOT NULL,
  close_event_id TEXT NOT NULL,
  close_event_date TEXT,
  close_settlement_date TEXT,
  close_source_ref TEXT,
  allocation_method TEXT NOT NULL DEFAULT 'fifo',
  contracts_allocated NUMERIC NOT NULL,
  opening_cash_allocated NUMERIC NOT NULL,
  closing_cash_allocated NUMERIC NOT NULL,
  realized_pnl NUMERIC NOT NULL,
  pnl_status TEXT NOT NULL DEFAULT 'final',
  notes TEXT,
  PRIMARY KEY (allocation_run_id, option_allocation_id),
  FOREIGN KEY (allocation_run_id, option_lot_id) REFERENCES option_contract_lots(allocation_run_id, option_lot_id)
);

CREATE INDEX IF NOT EXISTS idx_option_lot_allocations_run_contract
ON option_lot_allocations (allocation_run_id, option_contract_key, close_event_date, close_event_id);

CREATE TABLE IF NOT EXISTS option_underlying_links (
  allocation_run_id TEXT NOT NULL,
  link_id TEXT NOT NULL,
  option_allocation_id TEXT NOT NULL,
  option_lot_id TEXT NOT NULL,
  option_contract_key TEXT NOT NULL,
  link_type TEXT NOT NULL,
  underlying_event_table TEXT NOT NULL,
  underlying_event_id TEXT NOT NULL,
  underlying_instrument_key TEXT,
  underlying_quantity NUMERIC,
  strike_price NUMERIC,
  underlying_gross_amount NUMERIC,
  confidence TEXT NOT NULL DEFAULT 'inferred',
  notes TEXT,
  PRIMARY KEY (allocation_run_id, link_id),
  FOREIGN KEY (allocation_run_id, option_allocation_id) REFERENCES option_lot_allocations(allocation_run_id, option_allocation_id)
);

CREATE INDEX IF NOT EXISTS idx_option_underlying_links_run_option
ON option_underlying_links (allocation_run_id, option_contract_key, option_allocation_id);

CREATE TABLE IF NOT EXISTS fund_position_lots (
  allocation_run_id TEXT NOT NULL,
  fund_lot_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  fund_key TEXT NOT NULL,
  fund_code TEXT NOT NULL,
  fund_name TEXT,
  currency TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_table TEXT NOT NULL,
  source_pk TEXT NOT NULL,
  source_ref TEXT,
  open_date TEXT,
  original_units NUMERIC NOT NULL,
  remaining_units NUMERIC NOT NULL,
  cost_basis_total NUMERIC NOT NULL,
  remaining_cost NUMERIC NOT NULL,
  cost_basis_currency TEXT NOT NULL,
  cost_basis_status TEXT NOT NULL,
  cost_basis_source TEXT NOT NULL,
  unit_cost NUMERIC NOT NULL,
  cash_source_status TEXT,
  settlement_status TEXT NOT NULL DEFAULT 'settled',
  lot_status TEXT NOT NULL DEFAULT 'open',
  notes TEXT,
  PRIMARY KEY (allocation_run_id, fund_lot_id),
  FOREIGN KEY (allocation_run_id) REFERENCES lot_allocation_runs(allocation_run_id),
  FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE INDEX IF NOT EXISTS idx_fund_position_lots_run_fund
ON fund_position_lots (allocation_run_id, fund_key, settlement_status, open_date, fund_lot_id);

CREATE TABLE IF NOT EXISTS fund_lot_allocations (
  allocation_run_id TEXT NOT NULL,
  fund_allocation_id TEXT NOT NULL,
  fund_lot_id TEXT NOT NULL,
  redemption_order_id TEXT NOT NULL,
  redemption_date TEXT,
  redemption_source_ref TEXT,
  fund_key TEXT NOT NULL,
  fund_code TEXT NOT NULL,
  fund_name TEXT,
  currency TEXT NOT NULL,
  allocation_method TEXT NOT NULL DEFAULT 'fifo',
  units_allocated NUMERIC NOT NULL,
  proceeds_allocated NUMERIC NOT NULL,
  cost_allocated NUMERIC NOT NULL,
  realized_pnl NUMERIC NOT NULL,
  cost_basis_status TEXT NOT NULL,
  pnl_status TEXT NOT NULL,
  notes TEXT,
  PRIMARY KEY (allocation_run_id, fund_allocation_id),
  FOREIGN KEY (allocation_run_id, fund_lot_id) REFERENCES fund_position_lots(allocation_run_id, fund_lot_id)
);

CREATE INDEX IF NOT EXISTS idx_fund_lot_allocations_run_fund
ON fund_lot_allocations (allocation_run_id, fund_key, redemption_date, redemption_order_id);

CREATE TABLE IF NOT EXISTS short_stock_lots (
  allocation_run_id TEXT NOT NULL,
  short_lot_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  instrument_key TEXT NOT NULL,
  instrument_code TEXT,
  instrument_name TEXT,
  market TEXT,
  currency TEXT NOT NULL,
  source_table TEXT NOT NULL,
  source_pk TEXT NOT NULL,
  source_ref TEXT,
  open_date TEXT,
  settlement_date TEXT,
  original_quantity NUMERIC NOT NULL,
  remaining_quantity NUMERIC NOT NULL,
  opening_net_cash_amount NUMERIC NOT NULL,
  remaining_opening_cash_amount NUMERIC NOT NULL,
  opening_gross_amount NUMERIC,
  opening_fee_total NUMERIC,
  lot_status TEXT NOT NULL DEFAULT 'open',
  notes TEXT,
  PRIMARY KEY (allocation_run_id, short_lot_id),
  FOREIGN KEY (allocation_run_id) REFERENCES lot_allocation_runs(allocation_run_id),
  FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE INDEX IF NOT EXISTS idx_short_stock_lots_run_instrument
ON short_stock_lots (allocation_run_id, instrument_key, open_date, short_lot_id);

CREATE TABLE IF NOT EXISTS short_stock_allocations (
  allocation_run_id TEXT NOT NULL,
  short_allocation_id TEXT NOT NULL,
  short_lot_id TEXT NOT NULL,
  close_event_table TEXT NOT NULL,
  close_event_id TEXT NOT NULL,
  close_event_date TEXT,
  close_settlement_date TEXT,
  close_source_ref TEXT,
  instrument_key TEXT NOT NULL,
  instrument_code TEXT,
  instrument_name TEXT,
  currency TEXT NOT NULL,
  allocation_method TEXT NOT NULL DEFAULT 'fifo',
  quantity_allocated NUMERIC NOT NULL,
  opening_cash_allocated NUMERIC NOT NULL,
  closing_cash_allocated NUMERIC NOT NULL,
  realized_pnl NUMERIC NOT NULL,
  pnl_status TEXT NOT NULL DEFAULT 'final',
  notes TEXT,
  PRIMARY KEY (allocation_run_id, short_allocation_id),
  FOREIGN KEY (allocation_run_id, short_lot_id) REFERENCES short_stock_lots(allocation_run_id, short_lot_id)
);

CREATE INDEX IF NOT EXISTS idx_short_stock_allocations_run_instrument
ON short_stock_allocations (allocation_run_id, instrument_key, close_event_date, close_event_id);

DROP VIEW IF EXISTS v_lot_allocations_enriched;
DROP VIEW IF EXISTS v_lot_realized_pnl_by_instrument;
DROP VIEW IF EXISTS v_lot_open_positions;
DROP VIEW IF EXISTS v_option_realized_pnl_by_contract;
DROP VIEW IF EXISTS v_option_realized_pnl_by_currency;
DROP VIEW IF EXISTS v_option_open_positions;
DROP VIEW IF EXISTS v_fund_realized_pnl_by_fund;
DROP VIEW IF EXISTS v_fund_realized_pnl_by_currency;
DROP VIEW IF EXISTS v_fund_open_positions;
DROP VIEW IF EXISTS v_fund_pending_positions;
DROP VIEW IF EXISTS v_short_stock_realized_pnl_by_instrument;
DROP VIEW IF EXISTS v_short_stock_realized_pnl_by_currency;
DROP VIEW IF EXISTS v_short_stock_open_positions;
DROP VIEW IF EXISTS v_total_realized_pnl_by_currency_status;

CREATE VIEW IF NOT EXISTS v_lot_allocations_enriched AS
SELECT
  a.allocation_run_id,
  a.allocation_id,
  a.close_event_date,
  a.close_event_table,
  a.close_event_id,
  a.instrument_key,
  a.instrument_code,
  a.instrument_name,
  a.currency,
  a.lot_id,
  l.source_type AS lot_source_type,
  l.open_date AS lot_open_date,
  a.quantity_allocated,
  a.proceeds_allocated,
  a.cost_allocated,
  a.realized_pnl,
  a.cost_basis_status,
  a.pnl_status,
  a.notes
FROM lot_allocations a
JOIN position_lots l
  ON l.allocation_run_id = a.allocation_run_id
 AND l.lot_id = a.lot_id;

CREATE VIEW IF NOT EXISTS v_lot_realized_pnl_by_instrument AS
SELECT
  allocation_run_id,
  instrument_key,
  MIN(instrument_code) AS instrument_code,
  MIN(instrument_name) AS instrument_name,
  currency,
  ROUND(SUM(quantity_allocated), 6) AS quantity_sold,
  ROUND(SUM(proceeds_allocated), 2) AS proceeds_total,
  ROUND(SUM(cost_allocated), 2) AS cost_total,
  ROUND(SUM(realized_pnl), 2) AS realized_pnl,
  CASE
    WHEN SUM(CASE WHEN pnl_status = 'provisional' THEN 1 ELSE 0 END) > 0 THEN 'provisional'
    ELSE 'final'
  END AS pnl_status
FROM lot_allocations
GROUP BY allocation_run_id, instrument_key, currency;

CREATE VIEW IF NOT EXISTS v_option_realized_pnl_by_contract AS
SELECT
  allocation_run_id,
  option_contract_key,
  MIN(option_code) AS option_code,
  MIN(underlying_symbol) AS underlying_symbol,
  MIN(expiry_date) AS expiry_date,
  MIN(strike_price) AS strike_price,
  MIN(option_type) AS option_type,
  currency,
  position_side,
  close_outcome,
  ROUND(SUM(contracts_allocated), 6) AS contracts_closed,
  ROUND(SUM(opening_cash_allocated), 2) AS opening_cash_total,
  ROUND(SUM(closing_cash_allocated), 2) AS closing_cash_total,
  ROUND(SUM(realized_pnl), 2) AS realized_pnl,
  pnl_status
FROM option_lot_allocations
GROUP BY allocation_run_id, option_contract_key, currency, position_side, close_outcome, pnl_status;

CREATE VIEW IF NOT EXISTS v_option_realized_pnl_by_currency AS
SELECT
  allocation_run_id,
  currency,
  pnl_status,
  ROUND(SUM(contracts_allocated), 6) AS contracts_closed,
  ROUND(SUM(opening_cash_allocated), 2) AS opening_cash_total,
  ROUND(SUM(closing_cash_allocated), 2) AS closing_cash_total,
  ROUND(SUM(realized_pnl), 2) AS realized_pnl
FROM option_lot_allocations
GROUP BY allocation_run_id, currency, pnl_status;

CREATE VIEW IF NOT EXISTS v_option_open_positions AS
SELECT
  allocation_run_id,
  option_contract_key,
  MIN(option_code) AS option_code,
  MIN(underlying_symbol) AS underlying_symbol,
  MIN(expiry_date) AS expiry_date,
  MIN(strike_price) AS strike_price,
  MIN(option_type) AS option_type,
  MIN(contract_multiplier) AS contract_multiplier,
  currency,
  position_side,
  ROUND(SUM(original_contracts), 6) AS original_contracts,
  ROUND(SUM(remaining_contracts), 6) AS remaining_contracts,
  ROUND(SUM(opening_net_cash_amount), 2) AS opening_net_cash_amount,
  ROUND(SUM(remaining_opening_cash_amount), 2) AS remaining_opening_cash_amount
FROM option_contract_lots
WHERE remaining_contracts != 0
GROUP BY allocation_run_id, option_contract_key, currency, position_side;

CREATE VIEW IF NOT EXISTS v_fund_realized_pnl_by_fund AS
SELECT
  allocation_run_id,
  fund_key,
  MIN(fund_code) AS fund_code,
  MIN(fund_name) AS fund_name,
  currency,
  ROUND(SUM(units_allocated), 6) AS units_redeemed,
  ROUND(SUM(proceeds_allocated), 2) AS proceeds_total,
  ROUND(SUM(cost_allocated), 2) AS cost_total,
  ROUND(SUM(realized_pnl), 2) AS realized_pnl,
  CASE
    WHEN SUM(CASE WHEN pnl_status = 'provisional' THEN 1 ELSE 0 END) > 0 THEN 'provisional'
    ELSE 'final'
  END AS pnl_status
FROM fund_lot_allocations
GROUP BY allocation_run_id, fund_key, currency;

CREATE VIEW IF NOT EXISTS v_fund_realized_pnl_by_currency AS
SELECT
  allocation_run_id,
  currency,
  pnl_status,
  ROUND(SUM(units_allocated), 6) AS units_redeemed,
  ROUND(SUM(proceeds_allocated), 2) AS proceeds_total,
  ROUND(SUM(cost_allocated), 2) AS cost_total,
  ROUND(SUM(realized_pnl), 2) AS realized_pnl
FROM fund_lot_allocations
GROUP BY allocation_run_id, currency, pnl_status;

CREATE VIEW IF NOT EXISTS v_fund_open_positions AS
SELECT
  allocation_run_id,
  fund_key,
  MIN(fund_code) AS fund_code,
  MIN(fund_name) AS fund_name,
  currency,
  ROUND(SUM(original_units), 6) AS original_units,
  ROUND(SUM(remaining_units), 6) AS remaining_units,
  ROUND(SUM(cost_basis_total), 2) AS original_cost_total,
  ROUND(SUM(remaining_cost), 2) AS remaining_cost,
  CASE
    WHEN SUM(CASE WHEN cost_basis_status != 'final' THEN 1 ELSE 0 END) > 0 THEN 'provisional'
    ELSE 'final'
  END AS cost_basis_status
FROM fund_position_lots
WHERE remaining_units != 0
  AND settlement_status = 'settled'
GROUP BY allocation_run_id, fund_key, currency;

CREATE VIEW IF NOT EXISTS v_fund_pending_positions AS
SELECT
  allocation_run_id,
  fund_key,
  MIN(fund_code) AS fund_code,
  MIN(fund_name) AS fund_name,
  currency,
  ROUND(SUM(original_units), 6) AS original_units,
  ROUND(SUM(remaining_units), 6) AS remaining_units,
  ROUND(SUM(cost_basis_total), 2) AS original_cost_total,
  ROUND(SUM(remaining_cost), 2) AS remaining_cost,
  settlement_status
FROM fund_position_lots
WHERE remaining_units != 0
  AND settlement_status != 'settled'
GROUP BY allocation_run_id, fund_key, currency, settlement_status;

CREATE VIEW IF NOT EXISTS v_short_stock_realized_pnl_by_instrument AS
SELECT
  allocation_run_id,
  instrument_key,
  MIN(instrument_code) AS instrument_code,
  MIN(instrument_name) AS instrument_name,
  currency,
  ROUND(SUM(quantity_allocated), 6) AS quantity_closed,
  ROUND(SUM(opening_cash_allocated), 2) AS opening_cash_total,
  ROUND(SUM(closing_cash_allocated), 2) AS closing_cash_total,
  ROUND(SUM(realized_pnl), 2) AS realized_pnl,
  pnl_status
FROM short_stock_allocations
GROUP BY allocation_run_id, instrument_key, currency, pnl_status;

CREATE VIEW IF NOT EXISTS v_short_stock_realized_pnl_by_currency AS
SELECT
  allocation_run_id,
  currency,
  pnl_status,
  ROUND(SUM(quantity_allocated), 6) AS quantity_closed,
  ROUND(SUM(opening_cash_allocated), 2) AS opening_cash_total,
  ROUND(SUM(closing_cash_allocated), 2) AS closing_cash_total,
  ROUND(SUM(realized_pnl), 2) AS realized_pnl
FROM short_stock_allocations
GROUP BY allocation_run_id, currency, pnl_status;

CREATE VIEW IF NOT EXISTS v_short_stock_open_positions AS
SELECT
  allocation_run_id,
  instrument_key,
  MIN(instrument_code) AS instrument_code,
  MIN(instrument_name) AS instrument_name,
  MIN(market) AS market,
  currency,
  ROUND(SUM(original_quantity), 6) AS original_quantity,
  ROUND(SUM(remaining_quantity), 6) AS remaining_quantity,
  ROUND(SUM(opening_net_cash_amount), 2) AS opening_net_cash_amount,
  ROUND(SUM(remaining_opening_cash_amount), 2) AS remaining_opening_cash_amount
FROM short_stock_lots
WHERE remaining_quantity != 0
GROUP BY allocation_run_id, instrument_key, currency;

CREATE VIEW IF NOT EXISTS v_total_realized_pnl_by_currency_status AS
SELECT
  allocation_run_id,
  'stock_or_etf' AS pnl_layer,
  currency,
  pnl_status,
  ROUND(SUM(realized_pnl), 2) AS realized_pnl
FROM lot_allocations
GROUP BY allocation_run_id, currency, pnl_status
UNION ALL
SELECT
  allocation_run_id,
  'option' AS pnl_layer,
  currency,
  pnl_status,
  ROUND(SUM(realized_pnl), 2) AS realized_pnl
FROM option_lot_allocations
GROUP BY allocation_run_id, currency, pnl_status
UNION ALL
SELECT
  allocation_run_id,
  'fund' AS pnl_layer,
  currency,
  pnl_status,
  ROUND(SUM(realized_pnl), 2) AS realized_pnl
FROM fund_lot_allocations
GROUP BY allocation_run_id, currency, pnl_status
UNION ALL
SELECT
  allocation_run_id,
  'short_stock' AS pnl_layer,
  currency,
  pnl_status,
  ROUND(SUM(realized_pnl), 2) AS realized_pnl
FROM short_stock_allocations
GROUP BY allocation_run_id, currency, pnl_status;

CREATE VIEW IF NOT EXISTS v_lot_open_positions AS
SELECT
  allocation_run_id,
  instrument_key,
  MIN(instrument_code) AS instrument_code,
  MIN(instrument_name) AS instrument_name,
  MIN(market) AS market,
  currency,
  ROUND(SUM(original_quantity), 6) AS original_quantity,
  ROUND(SUM(remaining_quantity), 6) AS remaining_quantity,
  ROUND(SUM(cost_basis_total), 2) AS original_cost_total,
  ROUND(SUM(unit_cost * remaining_quantity), 2) AS remaining_cost_estimate,
  CASE
    WHEN SUM(CASE WHEN cost_basis_status != 'final' THEN 1 ELSE 0 END) > 0 THEN 'provisional'
    ELSE 'final'
  END AS cost_basis_status
FROM position_lots
WHERE remaining_quantity != 0
GROUP BY allocation_run_id, instrument_key, currency;
