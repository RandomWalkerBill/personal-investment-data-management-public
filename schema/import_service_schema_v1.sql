-- 投资数据导入服务内核 v1
-- 目标：在正式库中登记平台适配器、导入运行、校验闸门、人工复核项和可复用处理规则。
-- 说明：本 schema 不保存原始结单隐私内容；原始事实仍由各平台 parser 写入候选库/正式库的 raw fact 表。

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  migration_id TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (datetime('now')),
  description TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_migrations (migration_id, description)
VALUES ('import_service_schema_v1', '投资数据导入服务内核 v1：平台适配器、导入运行、校验闸门、复核项和规则经验库。');

CREATE TABLE IF NOT EXISTS import_service_platforms (
  platform_id TEXT PRIMARY KEY,
  platform_name TEXT NOT NULL,
  broker_name TEXT,
  market_scope TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  default_adapter_id TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT,
  CHECK (status IN ('active', 'draft', 'deprecated'))
);

CREATE TABLE IF NOT EXISTS import_service_adapters (
  adapter_id TEXT PRIMARY KEY,
  platform_id TEXT NOT NULL,
  adapter_name TEXT NOT NULL,
  adapter_kind TEXT NOT NULL,
  source_format TEXT NOT NULL,
  entrypoint TEXT NOT NULL,
  version TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  confidence_notes TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (adapter_kind IN ('pdf_monthly', 'xlsx_annual', 'csv_export', 'api_snapshot', 'manual_staging', 'unknown')),
  CHECK (status IN ('active', 'draft', 'deprecated')),
  FOREIGN KEY (platform_id) REFERENCES import_service_platforms(platform_id)
);

CREATE TABLE IF NOT EXISTS import_service_runs (
  service_run_id TEXT PRIMARY KEY,
  platform_id TEXT,
  adapter_id TEXT,
  input_path TEXT NOT NULL,
  input_fingerprint TEXT,
  candidate_db_path TEXT,
  official_db_path TEXT,
  candidate_import_run_id TEXT,
  stage TEXT NOT NULL DEFAULT 'created',
  status TEXT NOT NULL DEFAULT 'running',
  promote_status TEXT NOT NULL DEFAULT 'not_requested',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  summary_json TEXT NOT NULL DEFAULT '{}',
  notes TEXT,
  CHECK (stage IN ('created', 'planned', 'candidate_import', 'validation', 'review', 'ready_to_promote', 'promoted', 'blocked')),
  CHECK (status IN ('running', 'passed', 'blocked', 'needs_review', 'failed')),
  CHECK (promote_status IN ('not_requested', 'manual_required', 'blocked', 'ready', 'promoted')),
  FOREIGN KEY (platform_id) REFERENCES import_service_platforms(platform_id),
  FOREIGN KEY (adapter_id) REFERENCES import_service_adapters(adapter_id)
);

CREATE TABLE IF NOT EXISTS import_service_run_steps (
  step_id TEXT PRIMARY KEY,
  service_run_id TEXT NOT NULL,
  step_order INTEGER NOT NULL,
  step_name TEXT NOT NULL,
  command TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  exit_code INTEGER,
  summary_json TEXT NOT NULL DEFAULT '{}',
  CHECK (status IN ('running', 'passed', 'blocked', 'needs_review', 'failed', 'skipped')),
  FOREIGN KEY (service_run_id) REFERENCES import_service_runs(service_run_id)
);

CREATE TABLE IF NOT EXISTS import_service_rules (
  rule_id TEXT PRIMARY KEY,
  platform_id TEXT,
  adapter_id TEXT,
  rule_scope TEXT NOT NULL,
  rule_key TEXT NOT NULL,
  rule_title TEXT NOT NULL,
  rule_body TEXT NOT NULL,
  trigger_pattern TEXT,
  canonical_action TEXT,
  confidence TEXT NOT NULL DEFAULT 'reviewed',
  status TEXT NOT NULL DEFAULT 'active',
  created_from_run_id TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  notes TEXT,
  UNIQUE (platform_id, adapter_id, rule_scope, rule_key),
  CHECK (rule_scope IN ('parser', 'mapping', 'validation', 'normalization', 'treatment', 'manual_review', 'promotion')),
  CHECK (confidence IN ('confirmed', 'reviewed', 'inferred', 'draft')),
  CHECK (status IN ('active', 'draft', 'deprecated')),
  FOREIGN KEY (platform_id) REFERENCES import_service_platforms(platform_id),
  FOREIGN KEY (adapter_id) REFERENCES import_service_adapters(adapter_id),
  FOREIGN KEY (created_from_run_id) REFERENCES import_service_runs(service_run_id)
);

CREATE TABLE IF NOT EXISTS import_service_rule_evidence (
  evidence_id TEXT PRIMARY KEY,
  rule_id TEXT NOT NULL,
  service_run_id TEXT,
  source_ref TEXT,
  observed_text TEXT,
  decision TEXT,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (rule_id) REFERENCES import_service_rules(rule_id),
  FOREIGN KEY (service_run_id) REFERENCES import_service_runs(service_run_id)
);

CREATE TABLE IF NOT EXISTS import_service_review_items (
  review_item_id TEXT PRIMARY KEY,
  service_run_id TEXT NOT NULL,
  severity TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  source_ref TEXT,
  issue_type TEXT NOT NULL,
  message TEXT NOT NULL,
  suggested_action TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_at TEXT,
  resolution_notes TEXT,
  CHECK (severity IN ('blocker', 'needs_review', 'warning', 'info')),
  CHECK (status IN ('open', 'accepted', 'rejected', 'resolved', 'snoozed')),
  FOREIGN KEY (service_run_id) REFERENCES import_service_runs(service_run_id)
);

CREATE TABLE IF NOT EXISTS import_service_promote_decisions (
  decision_id TEXT PRIMARY KEY,
  service_run_id TEXT NOT NULL,
  decision_status TEXT NOT NULL,
  decided_by TEXT,
  decided_at TEXT NOT NULL DEFAULT (datetime('now')),
  decision_reason TEXT NOT NULL,
  official_db_backup_path TEXT,
  promote_record_path TEXT,
  notes TEXT,
  CHECK (decision_status IN ('blocked', 'manual_required', 'approved', 'promoted')),
  FOREIGN KEY (service_run_id) REFERENCES import_service_runs(service_run_id)
);

CREATE INDEX IF NOT EXISTS idx_import_service_runs_status
ON import_service_runs (status, stage, created_at);

CREATE INDEX IF NOT EXISTS idx_import_service_rules_lookup
ON import_service_rules (platform_id, adapter_id, rule_scope, status);

CREATE INDEX IF NOT EXISTS idx_import_service_review_items_run
ON import_service_review_items (service_run_id, status, severity);

INSERT OR IGNORE INTO import_service_platforms (
  platform_id, platform_name, broker_name, market_scope, status, default_adapter_id, notes
)
VALUES
  ('futu', '富途证券', '富途证券国际（香港）有限公司', 'HK/US/CN cross-market statements', 'active', 'futu_pdf_statement_v1', '当前已验证平台；PDF 月结单与官方年度账单均可进入统一 raw fact 框架。'),
  ('longbridge', '长桥证券', 'Long Bridge HK Limited', 'HK/US multi-currency statements', 'active', 'longbridge_pdf_monthly_v1', '长桥 PDF 月结单 adapter 已完成 2024-03 至 2026-05 候选导入验证；正式 promote 前仍走人工确认。'),
  ('xueying', '雪盈证券', 'SNB Finance Holdings Limited / IBKR', 'HK/US IBKR-style annual activity statements', 'active', 'xueying_pdf_annual_activity_v1', '雪盈年度 Activity Statement adapter；使用 PDF 文本/表格层抽取，按统一 raw fact schema 入库。'),
  ('unknown', '未知平台', NULL, NULL, 'draft', 'unknown_manual_staging', '新平台进入时先登记为 unknown，人工确认字段、业务类型和 parser 规则后再转为 active 平台。');

INSERT OR IGNORE INTO import_service_adapters (
  adapter_id, platform_id, adapter_name, adapter_kind, source_format, entrypoint, version, status, confidence_notes
)
VALUES
  ('futu_pdf_statement_v1', 'futu', '富途 PDF 月结单 parser v1', 'pdf_monthly', 'pdf', 'tools/futu_ingest_cli.py', 'v1', 'active', '已覆盖 2016-2026 多版富途月结单；老版结单通过专门 parser 分支进入同一 raw fact schema。'),
  ('futu_annual_xlsx_v1', 'futu', '富途官方年度账单 parser v1', 'xlsx_annual', 'xlsx', 'tools/futu_annual_bill_ingest_cli.py', 'v1', 'active', '用于历史年度账单快速补录和官方口径对齐；粒度可能低于月结单。'),
  ('longbridge_pdf_monthly_v1', 'longbridge', '长桥 PDF 月结单 parser v1', 'pdf_monthly', 'pdf', 'tools/longbridge_ingest_cli.py', 'v1', 'active', '使用密码保护 PDF 的文本层抽取；已覆盖现金、持仓、股票/期权交易、基金订单、公司行动现金腿和资产变动。'),
  ('xueying_pdf_annual_activity_v1', 'xueying', '雪盈年度 Activity Statement parser v1', 'pdf_monthly', 'pdf', 'tools/xueying_ingest_cli.py', 'v1', 'active', '读取 IBKR 风格年度活动账单表格层；覆盖交易、转仓、公司行动、费用、利息、股息、证券借出和现金/持仓锚点。'),
  ('unknown_manual_staging', 'unknown', '未知平台人工 staging', 'manual_staging', 'unknown', 'manual_review', 'v1', 'draft', '无法自动识别平台时，只生成 review item，不进入自动入库。');

INSERT OR IGNORE INTO import_service_rules (
  rule_id, platform_id, adapter_id, rule_scope, rule_key, rule_title, rule_body,
  trigger_pattern, canonical_action, confidence, status, notes
)
VALUES
  (
    'rule_futu_pdf_text_first_ocr_fallback',
    'futu',
    'futu_pdf_statement_v1',
    'parser',
    'pdf_text_layer_first',
    '富途 PDF 优先使用文本层抽取',
    '富途结单目前以 PDF 文本层抽取为主；OCR 作为后续 fallback，不作为 P0 默认链路。若文本层缺失、页结构漂移或 parser_issues 出现 blocker，应停止自动入库并修 parser。',
    'futu pdf statement text layer',
    'use_text_layer_then_review_on_failure',
    'reviewed',
    'active',
    '来自富途月结单抽取能力验证。'
  ),
  (
    'rule_futu_ipo_hidden_fee_10085bp',
    'futu',
    'futu_pdf_statement_v1',
    'treatment',
    'ipo_hidden_fee_1_0085_percent',
    'IPO 中签隐含平台及交易所费用按 1.0085% 解释',
    '富途港股 IPO 中签后，退款差额可能包含中签金额 1% 平台手续费和约 0.0085% 香港市场收费。默认在经济收益口径中归为 IPO 平台费用；融资利息作为期间费用，不从退款差额硬拆。',
    'IPO refund allotment 1.0085',
    'tag_ipo_cost_component_without_overwriting_raw_cash',
    'confirmed',
    'active',
    '用户已确认该解释口径。'
  ),
  (
    'rule_futu_account_upgrade_owner_level',
    'futu',
    'futu_pdf_statement_v1',
    'normalization',
    'account_upgrade_owner_level',
    '富途账户升级不视作真实卖出或买入',
    '富途账户升级导致账户号变化时，在交易原始数据保留原账户来源；owner-level lot/allocation 应把同一主体账户统一归一，避免把内部迁移当作交易收益。',
    'Account Upgrade|账户升级',
    'normalize_to_owner_account_scope',
    'confirmed',
    'active',
    '适用于跨账户统一持仓和收益计算。'
  ),
  (
    'rule_futu_rsu_arrival_date_cost_basis',
    'futu',
    'futu_pdf_statement_v1',
    'treatment',
    'rsu_arrival_date_close_price',
    'RSU 补录成本使用到账日期收盘价',
    '腾讯 RSU 等外部/雇员股权激励到账事件应生成人工补录 lot；成本口径使用结单中的到账日期，并按对应日期标的收盘价估算。归属日仅作为证据，不作为当前默认成本日期。',
    'RSU vesting arrival shares',
    'create_manual_lot_cost_basis_on_arrival_date',
    'confirmed',
    'active',
    '2026-06-30 已按该口径补录腾讯 RSU 成本。'
  ),
  (
    'rule_futu_old_monthly_template_detection',
    'futu',
    'futu_pdf_statement_v1',
    'parser',
    'old_monthly_template_detection',
    '富途老版月结单需要进入老模板分支',
    '2016-2020 左右富途月结单结构与新版不同。若检测到账户文件名形如 <futu-account>-1-YYYYMM 或文本标题为港股现金/保证金账户月结单，应走老版解析分支，但输出仍落到统一 raw fact schema。',
    '<futu-account>-1-YYYYMM|港股现金账户月结单|港股保证金账户月结单',
    'route_to_legacy_futu_monthly_parser_branch',
    'reviewed',
    'active',
    '来自 2016-2020 历史结单导入。'
  ),
  (
    'rule_longbridge_password_env_only',
    'longbridge',
    'longbridge_pdf_monthly_v1',
    'parser',
    'password_env_only',
    '长桥加密 PDF 密码只通过环境变量注入',
    '长桥月结单 PDF 为密码保护文件。导入服务可以接收一次性 --pdf-password，但只注入子进程环境变量，不把明文密码写入 command、service run 或候选库。',
    'statement-monthly-YYYYMM-H*.pdf encrypted',
    'pass_password_via_env_without_persisting_secret',
    'confirmed',
    'active',
    '来自长桥 2024-03 至 2026-05 PDF 导入验证。'
  ),
  (
    'rule_longbridge_zero_cash_anchor_fallback',
    'longbridge',
    'longbridge_pdf_monthly_v1',
    'validation',
    'zero_cash_anchor_fallback',
    '长桥结单无币种现金明细时可用账户总览零值锚点',
    '部分长桥月结单在现金为零时不列币种级现金余额明细。若账户总览现金为 0，parser 可生成 HKD/USD 零余额期初/期末锚点，备注为 longbridge_account_overview_cash_balance_fallback_no_currency_detail，用于连续性校验。',
    '账户总览现金=0 and no currency cash detail',
    'create_zero_cash_balance_validation_anchor',
    'reviewed',
    'active',
    '该规则只服务校验，不作为税务或收益计算源。'
  ),
  (
    'rule_longbridge_us_instrument_currency_inference',
    'longbridge',
    'longbridge_pdf_monthly_v1',
    'mapping',
    'us_instrument_currency_inference',
    '长桥美股/美股期权相关现金腿统一推断为 USD',
    '长桥部分现金行只给中文标的名或期权简写，不直接列币种。若匹配已知美股、美股 ETF、美股期权代码或同日美元公司行动，现金腿、费用和分红应归入 USD。',
    'BABA|BILI|TLT|FUTU|AMD|US option code',
    'infer_usd_currency_from_instrument_or_same_day_corporate_action',
    'reviewed',
    'active',
    '用于消除现金连续性中 HKD/USD 错桶问题。'
  ),
  (
    'rule_longbridge_option_expiry_asset_movement',
    'longbridge',
    'longbridge_pdf_monthly_v1',
    'parser',
    'option_expiry_asset_movement',
    '长桥期权到期未行权不是现金流水',
    '长桥文本行形如“期权到期未行权 ... Put/Call 1.00”表示合约数量出账，不是现金金额。应落 asset_movement_events，不写 cash_ledger_entries。',
    '期权到期未行权 .* Put|Call',
    'record_option_expiry_as_asset_movement',
    'reviewed',
    'active',
    '该修正已将长桥候选库现金连续性 failed 项清零。'
  ),
  (
    'rule_xueying_pdf_table_layer_activity_statement',
    'xueying',
    'xueying_pdf_annual_activity_v1',
    'parser',
    'pdf_table_layer_activity_statement',
    '雪盈年度 Activity Statement 使用 PDF 表格层抽取',
    '雪盈/SNB 结单为 IBKR 风格年度 Activity Statement，PDF 文本与表格层可直接抽取。parser 应按 section title 和关键列识别交易、转账、费用、利息、股息、公司行动、证券借出、现金和持仓锚点；不要按固定页码硬编码。',
    'Activity Statement|SNB Finance Holdings Limited|U*_YYYYMMDD_YYYYMMDD.pdf',
    'extract_tables_by_section_title',
    'reviewed',
    'active',
    '来自 2020-2026 六份雪盈年度账单结构验证。'
  ),
  (
    'rule_xueying_fop_transfer_carryover_basis',
    'xueying',
    'xueying_pdf_annual_activity_v1',
    'treatment',
    'same_owner_fop_transfer_carryover_basis',
    '同一 owner FOP 转入继承来源账户成本',
    '雪盈转账表中的“纯券过户（FOP）/进”是同一主体跨平台证券转入时，不应按转入日市值重置成本，也不应按零成本处理。若能匹配来源账户同日/近邻日期的非应税转出 allocation，应按来源 lot allocation 的 cost_allocated 生成转入 lot 成本。',
    '纯券过户（FOP）|FOP IN|transfer_account=<private-account>',
    'carry_over_previous_transfer_out_cost_basis',
    'confirmed',
    'active',
    '2020-10-15 腾讯控股 1,036 股雪盈转入与富途 SI OUT 对齐。'
  ),
  (
    'rule_unknown_platform_stop_at_review',
    'unknown',
    'unknown_manual_staging',
    'promotion',
    'unknown_platform_never_auto_promote',
    '未知平台不得自动入库',
    '无法识别平台或没有 active adapter 的结单，只能生成 service run 与 review item；必须先确认字段、业务类型映射、现金/持仓口径和 parser 规则，不能直接写入正式库。',
    'unknown platform',
    'block_promotion_until_adapter_confirmed',
    'confirmed',
    'active',
    '跨平台扩展默认安全规则。'
  );
