-- Tax calculation layer v1.
-- This layer stores derived tax-preparation estimates. It never rewrites raw
-- broker facts, lot/allocation facts, or manual cost-basis overlays.

CREATE TABLE IF NOT EXISTS fx_rate_sets (
  rate_set_id TEXT PRIMARY KEY,
  rate_set_name TEXT NOT NULL,
  base_currency TEXT NOT NULL DEFAULT 'CNY',
  policy_id TEXT NOT NULL,
  rate_source TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fx_rates (
  rate_set_id TEXT NOT NULL,
  currency TEXT NOT NULL,
  rate_date TEXT,
  rate_to_cny NUMERIC NOT NULL,
  rate_type TEXT NOT NULL,
  source_ref TEXT,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (rate_set_id, currency),
  FOREIGN KEY (rate_set_id) REFERENCES fx_rate_sets(rate_set_id)
);

CREATE TABLE IF NOT EXISTS tax_calculation_profiles (
  profile_id TEXT PRIMARY KEY,
  profile_name TEXT NOT NULL,
  jurisdiction TEXT NOT NULL,
  tax_year INTEGER NOT NULL,
  tax_rate NUMERIC NOT NULL,
  fx_rate_set_id TEXT NOT NULL,
  capital_gain_loss_policy TEXT NOT NULL,
  dividend_policy TEXT NOT NULL,
  withholding_credit_policy TEXT NOT NULL,
  financing_interest_policy TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (fx_rate_set_id) REFERENCES fx_rate_sets(rate_set_id)
);

CREATE TABLE IF NOT EXISTS tax_calculation_runs (
  tax_run_id TEXT PRIMARY KEY,
  profile_id TEXT NOT NULL,
  tax_year INTEGER NOT NULL,
  source_allocation_run_id TEXT,
  status TEXT NOT NULL,
  taxable_income_cny NUMERIC,
  tentative_tax_cny NUMERIC,
  foreign_tax_credit_cny NUMERIC,
  estimated_tax_due_cny NUMERIC,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (profile_id) REFERENCES tax_calculation_profiles(profile_id)
);

CREATE TABLE IF NOT EXISTS tax_calculation_items (
  tax_run_id TEXT NOT NULL,
  tax_item_id TEXT NOT NULL,
  item_type TEXT NOT NULL,
  item_subtype TEXT,
  source_table TEXT,
  source_pk TEXT,
  source_currency TEXT,
  source_amount NUMERIC,
  fx_rate_to_cny NUMERIC,
  amount_cny NUMERIC,
  taxable_cny NUMERIC,
  tax_rate NUMERIC,
  tax_cny NUMERIC,
  creditable_tax_cny NUMERIC,
  review_status TEXT NOT NULL DEFAULT 'auto_estimated',
  notes TEXT,
  PRIMARY KEY (tax_run_id, tax_item_id),
  FOREIGN KEY (tax_run_id) REFERENCES tax_calculation_runs(tax_run_id)
);

CREATE TABLE IF NOT EXISTS tax_calculation_summary_items (
  tax_run_id TEXT NOT NULL,
  summary_key TEXT NOT NULL,
  currency TEXT,
  amount NUMERIC,
  amount_cny NUMERIC,
  notes TEXT,
  PRIMARY KEY (tax_run_id, summary_key, currency),
  FOREIGN KEY (tax_run_id) REFERENCES tax_calculation_runs(tax_run_id)
);

CREATE TABLE IF NOT EXISTS tax_calculation_validation_items (
  tax_run_id TEXT NOT NULL,
  validation_item_id TEXT NOT NULL,
  check_code TEXT NOT NULL,
  status TEXT NOT NULL,
  severity TEXT NOT NULL,
  message TEXT NOT NULL,
  notes TEXT,
  PRIMARY KEY (tax_run_id, validation_item_id),
  FOREIGN KEY (tax_run_id) REFERENCES tax_calculation_runs(tax_run_id)
);

CREATE INDEX IF NOT EXISTS idx_tax_items_type
  ON tax_calculation_items(tax_run_id, item_type, item_subtype, source_currency);

CREATE INDEX IF NOT EXISTS idx_tax_summary_key
  ON tax_calculation_summary_items(tax_run_id, summary_key, currency);
