import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith("--")) continue;
    const key = arg.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith("--")) {
      args[key] = "true";
    } else {
      args[key] = next;
      i += 1;
    }
  }
  return args;
}

const args = parseArgs(process.argv.slice(2));
const workspaceRoot = path.resolve(args["workspace-root"] ?? process.cwd());
const sourceDir = path.resolve(args["source-dir"] ?? path.join(workspaceRoot, "work/parser-output"));
const outputDir = path.resolve(args["output-dir"] ?? path.join(workspaceRoot, "work/review-workbook"));
const outputPath = path.resolve(
  args["output-xlsx"] ?? path.join(outputDir, "futu-q1-2025-standard-fact-tables-review.xlsx"),
);
const reportTitle = args.title ?? "富途结单标准事实表审阅包";
const reportScope = args.scope ?? "富途月结单";
const generatedDate = args["generated-date"] ?? localDateString();

const tableFiles = [
  ["cash_ledger_entries", "cash_ledger_entries.csv", "现金事实流水"],
  ["market_trades", "market_trades.csv", "二级市场交易"],
  ["market_trade_fee_items", "market_trade_fee_items.csv", "交易费用明细"],
  ["fund_orders", "fund_orders.csv", "基金订单"],
  ["fund_order_cash_legs", "fund_order_cash_legs.csv", "基金现金腿"],
  ["fund_transactions", "fund_transactions.csv", "基金交易证据行"],
  ["fund_transaction_fee_lines", "fund_transaction_fee_lines.csv", "基金费用证据行"],
  ["corporate_action_cash_legs", "corporate_action_cash_legs.csv", "公司行动现金腿"],
  ["asset_movement_events", "asset_movement_events.csv", "资产变动事件"],
  ["derivative_exercise_events", "derivative_exercise_events.csv", "期权到期/行权事件"],
  ["financing_interest_events", "financing_interest_events.csv", "融资利息事件"],
  ["financing_interest_evidence_items", "financing_interest_evidence_items.csv", "融资利息证据行"],
  ["stock_yield_daily_events", "stock_yield_daily_events.csv", "股票收益计划日度明细"],
  ["stock_yield_cash_entries", "stock_yield_cash_entries.csv", "股票收益计划现金入账"],
  ["parser_issues", "parser_issues.csv", "解析问题与处理状态"],
  ["unclassified_governance", "unclassified_governance.csv", "未分类表治理"],
];

const sheetNames = {
  cash_ledger_entries: "现金流水",
  market_trades: "市场交易",
  market_trade_fee_items: "交易费用",
  fund_orders: "基金订单",
  fund_order_cash_legs: "基金现金腿",
  fund_transactions: "基金证据",
  fund_transaction_fee_lines: "基金费用证据",
  corporate_action_cash_legs: "公司行动",
  asset_movement_events: "资产变动",
  derivative_exercise_events: "期权事件",
  financing_interest_events: "融资利息",
  financing_interest_evidence_items: "融资利息证据",
  stock_yield_daily_events: "股票收益日度",
  stock_yield_cash_entries: "股票收益现金",
  parser_issues: "解析问题",
  unclassified_governance: "未分类治理",
};

const tableRoles = {
  cash_ledger_entries: "核心事实表：所有现金变动的主时间轴，后续现金余额、对账、收益归因均从这里出发。",
  market_trades: "核心事实表：二级市场股票/期权交易主事件。",
  market_trade_fee_items: "子表：二级市场交易费用/税费明细，挂在 market_trades 下。",
  fund_orders: "核心事实表：基金申购/赎回主事件，保留数量、价格和金额。",
  fund_order_cash_legs: "关系表：基金订单与现金流水之间的现金腿匹配关系。",
  fund_transactions: "证据表：富途基金交易原始行，包括完整明细行和 dash-date 金额证据行。",
  fund_transaction_fee_lines: "证据表：基金交易表里的费用/小计证据行，当前样本主要用于 0 费用校验。",
  corporate_action_cash_legs: "核心事实表：股息、预扣税、ADR fee 等公司行动现金腿。",
  asset_movement_events: "核心事实表：非现金资产数量变动，例如 IPO 配发、期权到期导致仓位变化。",
  derivative_exercise_events: "聚合事实表：期权到期/行权事件，关联现金腿和资产腿。",
  financing_interest_events: "核心事实表：月度融资利息现金扣款事件。",
  financing_interest_evidence_items: "证据表：日度融资利息计算证据。",
  stock_yield_daily_events: "证据/明细表：股票收益计划日度收益明细。",
  stock_yield_cash_entries: "核心事实表：股票收益计划实际现金入账。",
  parser_issues: "治理表：解析器发现、修复、合并或仍需复核的问题。",
  unclassified_governance: "治理表：未分类表格行的默认归类与处理状态。",
};

const fieldDescriptions = {
  id: ["主键", "表内稳定主键。"],
  cash_entry_id: ["现金流水 ID", "现金事实流水主键。"],
  trade_id: ["交易 ID", "二级市场交易主键。"],
  fee_tax_item_id: ["费用/税费项 ID", "交易费用或税费明细主键。"],
  fund_order_id: ["基金订单 ID", "基金申购/赎回订单主键。"],
  fund_cash_leg_id: ["基金现金腿 ID", "基金订单与现金流水关系主键。"],
  fund_transaction_row_id: ["基金证据行 ID", "基金交易原始证据行主键。"],
  fund_fee_row_id: ["基金费用证据行 ID", "基金费用/小计原始证据行主键。"],
  corporate_action_cash_leg_id: ["公司行动现金腿 ID", "公司行动现金腿主键。"],
  asset_movement_id: ["资产变动 ID", "资产数量/金额变动事件主键。"],
  derivative_exercise_id: ["期权事件 ID", "期权到期/行权聚合事件主键。"],
  financing_interest_event_id: ["融资利息事件 ID", "融资利息现金事件主键。"],
  financing_interest_evidence_id: ["融资利息证据 ID", "日度融资利息证据行主键。"],
  stock_yield_daily_id: ["股票收益日度 ID", "股票收益计划日度明细主键。"],
  stock_yield_cash_id: ["股票收益现金 ID", "股票收益计划现金入账主键。"],
  issue_id: ["解析问题 ID", "解析或治理问题主键。"],
  unclassified_governance_id: ["未分类治理 ID", "未分类行治理记录主键。"],
  statement_id: ["结单 ID", "本阶段用结单周期作为来源结单标识。"],
  period: ["结单周期", "YYYYMM。"],
  filename: ["来源文件名", "本地 PDF 文件名，仅用于追溯，不作为业务字段。"],
  page: ["PDF 页码", "事实或证据所在 PDF 页码。"],
  source_ref: ["来源引用", "单个页码/表格/行号/文本行引用。"],
  source_refs: ["来源引用集合", "可包含多个来源引用；用于去重、多证据和跨行还原。"],
  business_type: ["一级业务类型", "统一框架下的标准业务分类。"],
  cash_leg_type: ["现金腿类型", "现金流水在父业务中的角色。"],
  event_date: ["业务日期", "现金、资产或业务事件发生日期。"],
  trade_datetime: ["成交时间", "交易发生的原始日期时间。"],
  trade_date: ["成交日期", "二级市场交易成交日。"],
  settlement_date: ["交收日期", "交易或现金在结单中给出的交收日。"],
  order_date: ["订单日期", "基金订单日期。"],
  cash_event_date: ["现金扣款日期", "融资利息等事件的现金发生日。"],
  evidence_date: ["证据日期", "日度利息等明细证据日期。"],
  direction_raw: ["原始方向", "富途原文方向，例如 增加 / 減少。"],
  raw_direction: ["原始交易方向", "富途原文交易方向，例如 賣出平倉 / 賣出開倉。"],
  event_type_raw: ["原始类型", "富途现金流水原始事件类型。"],
  transaction_type_raw: ["原始基金交易类型", "富途基金交易表原始交易类型。"],
  raw_label: ["原始费用标签", "富途交易费用原始标签。"],
  raw_line: ["原始行文本", "解析前或拼接后的原始行文本。"],
  raw_row: ["原始表格行", "未分类表格行的原始数组文本。"],
  description: ["原始说明", "现金流水描述。"],
  description_raw: ["原始说明", "父业务或子腿中的原始描述。"],
  instrument_code_raw: ["原始标的代码", "结单中出现的代码/合约代码/标的原文。"],
  instrument_symbol: ["标准标的代码", "解析后的标准代码或合约代码。"],
  instrument_name_raw: ["原始标的名称", "结单中的标的名称。"],
  instrument_type: ["标的类型", "stock / option / fund 等。"],
  instrument_code: ["基金代码", "基金订单或证据行中的基金 ISIN/代码。"],
  instrument_mapping_status: ["标的映射状态", "mapped / needs_review 等。"],
  market: ["市场", "SEHK / XNDQ / BATO 等。"],
  currency: ["币种", "HKD / USD 等。"],
  amount: ["带符号金额", "现金流水金额；收入/入金为正，扣款/支出为负。"],
  cash_amount: ["带符号现金金额", "子表中的现金腿金额，与现金流水金额方向一致。"],
  amount_abs: ["正数金额", "基金交易或费用明细的绝对金额。"],
  fund_amount_abs: ["基金订单金额", "基金订单金额绝对值。"],
  fee_amount: ["费用金额", "基金费用证据行金额。"],
  subtotal: ["小计", "基金费用证据行小计。"],
  gross_amount: ["成交金额", "交易不含费用前的成交金额。"],
  fee_total: ["费用合计", "交易行费用合计。"],
  net_cash_amount: ["净现金金额", "交易进入现金侧的净金额；卖出为正，买入为负。"],
  quantity: ["数量", "股票为股、期权为合约、基金为份额。"],
  quantity_unit: ["数量单位", "share / contract 等。"],
  quantity_basis: ["权益计算数量", "股息、税费、ADR fee 等公司行动的数量基数。"],
  price: ["价格", "交易价格或基金价格。"],
  rate_raw: ["原始费率/每股金额", "公司行动中的 per share 等原始率。"],
  annual_rate_raw: ["原始年利率", "融资利息或股票收益计划原始年化率。"],
  daily_interest: ["当日利息", "融资利息日度明细金额。"],
  interest_amount: ["当日收益/利息", "股票收益计划日度收益。"],
  cumulative_interest: ["累计利息", "融资利息或股票收益计划累计口径。"],
  financing_amount: ["融资金额", "融资利息明细中的融资本金/金额。"],
  settlement_amount: ["结算金额", "股票收益计划日度明细中的结算金额。"],
  collateral_amount: ["抵押金额", "股票收益计划日度明细中的抵押金额。"],
  side: ["买卖方向", "buy / sell。"],
  position_effect: ["开平仓方向", "open / close / unknown。"],
  underlying_symbol: ["底层标的", "期权底层股票代码。"],
  expiry_date: ["到期日", "期权到期日。"],
  strike_price: ["行权价", "期权行权价。"],
  option_type: ["期权类型", "call / put。"],
  exercise_type: ["期权事件类型", "expiry / exercise / assignment 等。"],
  option_instrument_raw: ["原始期权合约", "期权事件中的合约原文。"],
  asset_movement_ids: ["关联资产腿", "期权事件关联的资产变动 ID 列表。"],
  asset_movement_type: ["资产变动类型", "IPO 配发、期权到期平仓等标准类型。"],
  interest_type: ["利息类型", "margin_interest 等。"],
  period_label: ["原始期间说明", "例如 Interest In Jan.。"],
  income_month: ["收益所属月份", "股票收益计划日度明细归属月份。"],
  income_month_guess: ["推断收益月份", "股票收益计划现金描述推断出的收益归属月份。"],
  reconciliation_status: ["对账状态", "明细与现金入账关系的当前状态。"],
  evidence_status: ["证据状态", "complete_detail / amount_only 等。"],
  cash_match_status: ["现金匹配状态", "基金订单资金侧匹配状态，例如 matched_cash_leg / matched_opening_pending / unmatched。"],
  cash_match_source_refs: ["现金匹配来源", "基金订单资金侧匹配的来源引用；本期现金腿或期初 pending 来源。"],
  evidence_row_ids: ["证据行 ID 集合", "父订单关联的原始证据行 ID。"],
  match_status: ["匹配状态", "现金腿与父订单的匹配状态。"],
  candidate_order_ids: ["候选订单 ID", "现金腿匹配到的候选基金订单。"],
  trade_index: ["交易索引", "费用项归属交易 ID。"],
  parent_event_id: ["父事件 ID", "费用/税费等子项归属的父事件。"],
  parent_business_type: ["父业务类型", "父事件的 business_type。"],
  fee_tax_type: ["标准费用/税费类型", "commission / stamp_duty / withholding_tax 等。"],
  corporate_action_group_type: ["公司行动组类型", "dividend_event / adr_fee_event 等。"],
  corporate_action_type: ["公司行动类型", "cash_dividend / withholding_tax / adr_fee 等。"],
  dedupe_status: ["去重状态", "unique / merged 等。"],
  source_count: ["来源数量", "合并后保留的来源候选数量。"],
  mapping_status: ["映射状态", "mapped / needs_review 等。"],
  issue_type: ["问题类型", "duplicate_candidate / unclassified_table_row 等。"],
  severity: ["严重程度", "info / warning / needs_review / blocker。"],
  status: ["处理状态", "open / merged / ignored_layout_noise 等。"],
  message: ["问题说明", "解析器或治理项的可读说明。"],
  default_classification: ["默认分类", "未分类行的默认治理分类。"],
};

const numericFields = new Set([
  "amount",
  "cash_amount",
  "amount_abs",
  "fund_amount_abs",
  "fee_amount",
  "subtotal",
  "gross_amount",
  "fee_total",
  "net_cash_amount",
  "quantity",
  "price",
  "strike_price",
  "quantity_basis",
  "daily_interest",
  "interest_amount",
  "cumulative_interest",
  "financing_amount",
  "settlement_amount",
  "collateral_amount",
  "source_count",
]);

const dateFields = new Set([
  "event_date",
  "trade_date",
  "settlement_date",
  "order_date",
  "cash_event_date",
  "evidence_date",
  "expiry_date",
]);

const textFields = new Set([
  "statement_id",
  "period",
  "filename",
  "page",
  "source_ref",
  "source_refs",
  "cash_entry_id",
  "trade_id",
  "fund_order_id",
  "fund_cash_leg_id",
  "corporate_action_cash_leg_id",
  "asset_movement_id",
  "derivative_exercise_id",
  "fee_tax_item_id",
  "issue_id",
  "instrument_code",
  "instrument_code_raw",
  "instrument_symbol",
  "underlying_symbol",
  "candidate_order_ids",
  "evidence_row_ids",
  "asset_movement_ids",
]);

function parseCsv(text) {
  const rows = [];
  let row = [];
  let cell = "";
  let inQuotes = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    const next = text[i + 1];
    if (inQuotes) {
      if (ch === '"' && next === '"') {
        cell += '"';
        i += 1;
      } else if (ch === '"') {
        inQuotes = false;
      } else {
        cell += ch;
      }
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ",") {
      row.push(cell);
      cell = "";
    } else if (ch === "\n") {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else if (ch !== "\r") {
      cell += ch;
    }
  }
  if (cell.length > 0 || row.length > 0) {
    row.push(cell);
    rows.push(row);
  }
  return rows.filter((r) => r.length > 1 || r[0] !== "");
}

function localDateString() {
  const d = new Date();
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function parseNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const cleaned = String(value).replace(/,/g, "");
  if (!/^[-+]?\d+(\.\d+)?$/.test(cleaned)) return value;
  return Number(cleaned);
}

function parseDate(value) {
  if (!value) return null;
  const normalized = String(value).replaceAll("/", "-");
  const match = normalized.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) return value;
  return new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
}

function coerceValue(header, value) {
  if (value === "") return "";
  if (numericFields.has(header)) return parseNumber(value);
  if (dateFields.has(header)) return parseDate(value);
  if (textFields.has(header)) return String(value);
  return value;
}

function rowsToObjects(matrix) {
  const [headers, ...rows] = matrix;
  return rows.map((row) => {
    const obj = {};
    headers.forEach((h, i) => {
      obj[h] = row[i] ?? "";
    });
    return obj;
  });
}

async function readTable(tableName, fileName) {
  const text = await fs.readFile(path.join(sourceDir, fileName), "utf8");
  const matrix = parseCsv(text);
  const headers = matrix[0];
  const objects = rowsToObjects(matrix);
  const values = [headers, ...objects.map((row) => headers.map((h) => coerceValue(h, row[h])))];
  return { tableName, fileName, headers, rows: objects, values };
}

function amount(v) {
  if (v === "" || v === null || v === undefined) return 0;
  return Number(String(v).replace(/,/g, ""));
}

function counter(rows, field) {
  const out = new Map();
  rows.forEach((r) => out.set(r[field], (out.get(r[field]) ?? 0) + 1));
  return out;
}

function asObjectBy(rows, key) {
  const out = new Map();
  rows.forEach((r) => out.set(r[key], r));
  return out;
}

function buildChecks(tables, acceptanceReport) {
  const checks = [];
  const add = (code, description, status, actual, expected, notes = "") => {
    checks.push([code, description, status, actual, expected, notes]);
  };

  acceptanceReport.tests.forEach((test) => {
    add(test.code, "parser v1 P0 覆盖测试", test.passed ? "OK" : "FAIL", test.actual, test.expected, "");
  });

  const pkMap = {
    cash_ledger_entries: "cash_entry_id",
    market_trades: "trade_id",
    fund_orders: "fund_order_id",
    fund_order_cash_legs: "fund_cash_leg_id",
    corporate_action_cash_legs: "corporate_action_cash_leg_id",
    asset_movement_events: "asset_movement_id",
    derivative_exercise_events: "derivative_exercise_id",
    financing_interest_events: "financing_interest_event_id",
    financing_interest_evidence_items: "financing_interest_evidence_id",
    stock_yield_daily_events: "stock_yield_daily_id",
    stock_yield_cash_entries: "stock_yield_cash_id",
    market_trade_fee_items: "fee_tax_item_id",
    fund_transactions: "fund_transaction_row_id",
    fund_transaction_fee_lines: "fund_fee_row_id",
    parser_issues: "issue_id",
    unclassified_governance: "unclassified_governance_id",
  };
  Object.entries(pkMap).forEach(([tableName, pk]) => {
    const rows = tables[tableName].rows;
    const counts = counter(rows, pk);
    const duplicateIds = [...counts.entries()].filter(([, count]) => count > 1).map(([id]) => id);
    const missing = rows.filter((r) => !r[pk]).length;
    add(
      `PK-${tableName}`,
      `${tableName}.${pk} 唯一且非空`,
      duplicateIds.length === 0 && missing === 0 ? "OK" : "FAIL",
      `rows=${rows.length}; missing=${missing}; duplicates=${duplicateIds.length}`,
      "missing=0; duplicates=0",
      duplicateIds.slice(0, 3).join("; "),
    );
  });

  const cashById = asObjectBy(tables.cash_ledger_entries.rows, "cash_entry_id");
  const fundById = asObjectBy(tables.fund_orders.rows, "fund_order_id");
  const tradeById = asObjectBy(tables.market_trades.rows, "trade_id");
  const assetById = asObjectBy(tables.asset_movement_events.rows, "asset_movement_id");

  tableFiles
    .map(([tableName]) => tableName)
    .filter((tableName) => tables[tableName].rows.length > 0)
    .forEach((tableName) => {
      const headers = tables[tableName].headers;
      const sourceField = headers.includes("source_refs") ? "source_refs" : headers.includes("source_ref") ? "source_ref" : null;
      if (!sourceField) return;
      const missing = tables[tableName].rows.filter((r) => !r[sourceField]).length;
      add(`SRC-${tableName}`, `${tableName} 来源引用非空`, missing === 0 ? "OK" : "FAIL", missing, 0, "");
    });

  const corporateBad = [];
  tables.corporate_action_cash_legs.rows.forEach((r) => {
    const cash = cashById.get(r.cash_entry_id);
    if (!cash) corporateBad.push(`${r.corporate_action_cash_leg_id}: missing cash`);
    else if (
      cash.business_type !== "corporate_action" ||
      cash.currency !== r.currency ||
      amount(cash.amount) !== amount(r.cash_amount) ||
      cash.event_date !== r.event_date
    ) {
      corporateBad.push(`${r.corporate_action_cash_leg_id}: mismatch`);
    }
  });
  add("FK-CORP-CASH", "公司行动现金腿与现金流水逐条对齐", corporateBad.length === 0 ? "OK" : "FAIL", corporateBad.length, 0, corporateBad.join("; "));

  const fundBad = [];
  const fundMatchCounts = {};
  tables.fund_order_cash_legs.rows.forEach((r) => {
    fundMatchCounts[r.match_status] = (fundMatchCounts[r.match_status] ?? 0) + 1;
    const cash = cashById.get(r.cash_entry_id);
    const order = fundById.get(r.fund_order_id);
    if (!cash) fundBad.push(`${r.fund_cash_leg_id}: missing cash`);
    if (!order) fundBad.push(`${r.fund_cash_leg_id}: missing fund order`);
    if (cash && cash.business_type !== "fund_order") fundBad.push(`${r.fund_cash_leg_id}: cash business_type mismatch`);
    if (cash && amount(cash.amount) !== amount(r.cash_amount)) fundBad.push(`${r.fund_cash_leg_id}: cash amount mismatch`);
  });
  add("FK-FUND-CASH", "基金现金腿与现金流水/基金订单关联有效", fundBad.length === 0 ? "OK" : "FAIL", fundBad.length, 0, JSON.stringify(fundMatchCounts));

  const tradeFees = {};
  const tradeFeeBad = [];
  tables.market_trade_fee_items.rows.forEach((r) => {
    if (!tradeById.has(r.parent_event_id)) tradeFeeBad.push(`${r.fee_tax_item_id}: missing trade`);
    tradeFees[r.parent_event_id] = (tradeFees[r.parent_event_id] ?? 0) + amount(r.amount_abs);
  });
  tables.market_trades.rows.forEach((r) => {
    if (Math.round((tradeFees[r.trade_id] ?? 0) * 100) !== Math.round(amount(r.fee_total) * 100)) {
      tradeFeeBad.push(`${r.trade_id}: fee sum ${tradeFees[r.trade_id] ?? 0} != ${r.fee_total}`);
    }
  });
  add("FK-MT-FEES", "市场交易费用归属有效且合计等于交易 fee_total", tradeFeeBad.length === 0 ? "OK" : "FAIL", tradeFeeBad.length, 0, tradeFeeBad.join("; "));

  const finBad = tables.financing_interest_events.rows.filter((r) => {
    const cash = cashById.get(r.cash_entry_id);
    return !cash || cash.business_type !== "financing_interest" || amount(cash.amount) !== amount(r.cash_amount);
  });
  add("FK-FIN-CASH", "融资利息事件与现金流水对齐", finBad.length === 0 ? "OK" : "FAIL", finBad.length, 0, "");

  const yieldBad = tables.stock_yield_cash_entries.rows.filter((r) => {
    const cash = cashById.get(r.cash_entry_id);
    return !cash || cash.business_type !== "securities_lending_income" || amount(cash.amount) !== amount(r.cash_amount);
  });
  add("FK-SLI-CASH", "股票收益计划现金入账与现金流水对齐", yieldBad.length === 0 ? "OK" : "FAIL", yieldBad.length, 0, "");

  const derivativeBad = [];
  tables.derivative_exercise_events.rows.forEach((r) => {
    const cash = cashById.get(r.cash_entry_id);
    if (!cash || cash.business_type !== "derivative_exercise" || amount(cash.amount) !== amount(r.cash_amount)) {
      derivativeBad.push(`${r.derivative_exercise_id}: cash mismatch`);
    }
    String(r.asset_movement_ids || "")
      .split(/[;,]/)
      .map((x) => x.trim())
      .filter(Boolean)
      .forEach((assetId) => {
        if (!assetById.has(assetId)) derivativeBad.push(`${r.derivative_exercise_id}: missing asset ${assetId}`);
      });
  });
  add("FK-DERIV-LEGS", "期权到期/行权事件现金腿与资产腿关联有效", derivativeBad.length === 0 ? "OK" : "FAIL", derivativeBad.length, 0, derivativeBad.join("; "));

  const cashCounts = counter(tables.cash_ledger_entries.rows, "business_type");
  const statementIds = new Set(tables.cash_ledger_entries.rows.map((r) => r.statement_id).filter(Boolean));
  const isQ1Baseline =
    statementIds.size === 3 && statementIds.has("202501") && statementIds.has("202502") && statementIds.has("202503");
  if (isQ1Baseline) {
    const expectedCashCounts = {
      external_transfer: 4,
      fund_order: 16,
      ipo_subscription: 6,
      corporate_action: 8,
      financing_interest: 1,
      securities_lending_income: 2,
      broker_reward: 1,
      derivative_exercise: 1,
    };
    const countMismatch = Object.entries(expectedCashCounts).filter(([k, v]) => (cashCounts.get(k) ?? 0) !== v);
    add("CNT-CASH-BIZ", "现金流水 business_type 数量符合 Q1 P0 基线", countMismatch.length === 0 ? "OK" : "FAIL", JSON.stringify(Object.fromEntries(cashCounts)), JSON.stringify(expectedCashCounts), "");
  } else {
    const unknownCount = cashCounts.get("unknown") ?? 0;
    add("CNT-CASH-BIZ", "现金流水 business_type 动态月份无 unknown", unknownCount === 0 ? "OK" : "FAIL", JSON.stringify(Object.fromEntries(cashCounts)), "unknown=0", "");
  }

  const zeroCash = tables.cash_ledger_entries.rows.filter((r) => amount(r.amount) === 0);
  const zeroCashOk = isQ1Baseline
    ? zeroCash.length === 1 && zeroCash[0].business_type === "derivative_exercise"
    : zeroCash.every((r) => r.business_type === "derivative_exercise");
  add(
    "CASH-ZERO-KEEP",
    "0 金额现金腿保留",
    zeroCashOk ? "OK" : "FAIL",
    zeroCash.map((r) => r.cash_entry_id).join("; "),
    isQ1Baseline ? "1 derivative_exercise row" : "zero amount rows are derivative_exercise",
    "",
  );

  const blockers = tables.parser_issues.rows.filter((r) => ["blocker", "error", "critical"].includes(String(r.severity).toLowerCase()) && !["fixed", "ignored"].includes(r.status));
  add("ISSUE-NO-BLOCKER", "parser_issues 无阻塞级待处理项", blockers.length === 0 ? "OK" : "FAIL", blockers.length, 0, "");

  return checks;
}

function pendingItems(tables) {
  const items = [];
  tables.parser_issues.rows
    .filter((r) => r.severity === "needs_review" || r.status === "open")
    .forEach((r) => {
      items.push([
        "needs_review",
        "解析/业务复核",
        r.statement_id,
        r.source_ref,
        r.issue_type,
        r.message,
        "固化 schema 前建议确认；不影响当前事实表生成。",
      ]);
    });

  tables.corporate_action_cash_legs.rows
    .filter((r) => r.instrument_mapping_status === "needs_review")
    .forEach((r) => {
      items.push([
        "needs_review",
        "公司行动标的映射",
        r.statement_id,
        r.source_refs,
        r.corporate_action_type,
        `${r.event_date} ${r.currency} ${r.cash_amount}: ${r.description_raw}`,
        "需要确认原始结单是否缺少标的代码，或能否从持仓/上下文补出。",
      ]);
    });

  return items;
}

function businessSummaryRows(cashRows) {
  const grouped = {};
  cashRows.forEach((r) => {
    const key = `${r.business_type}|${r.currency}`;
    grouped[key] ??= { business_type: r.business_type, currency: r.currency, count: 0, total: 0 };
    grouped[key].count += 1;
    grouped[key].total += amount(r.amount);
  });
  return Object.values(grouped)
    .sort((a, b) => `${a.business_type}${a.currency}`.localeCompare(`${b.business_type}${b.currency}`))
    .map((r) => [r.business_type, r.currency, r.count, Math.round(r.total * 100) / 100]);
}

function colName(index) {
  let n = index + 1;
  let name = "";
  while (n > 0) {
    const rem = (n - 1) % 26;
    name = String.fromCharCode(65 + rem) + name;
    n = Math.floor((n - 1) / 26);
  }
  return name;
}

function rangeAddress(rowCount, colCount) {
  return `A1:${colName(colCount - 1)}${rowCount}`;
}

function safeTableName(base) {
  return `tbl_${base}`.replace(/[^A-Za-z0-9_]/g, "_").slice(0, 250);
}

function applySheetBasics(sheet) {
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
}

function formatTableSheet(sheet, values, tableName) {
  applySheetBasics(sheet);
  const rowCount = values.length;
  const colCount = values[0]?.length ?? 1;
  const address = rangeAddress(rowCount, colCount);
  const fullRange = sheet.getRange(address);
  fullRange.values = values;
  const header = sheet.getRange(`A1:${colName(colCount - 1)}1`);
  header.format = {
    fill: "#143D59",
    font: { bold: true, color: "#FFFFFF" },
    wrapText: true,
  };
  fullRange.format.borders = {
    insideHorizontal: { style: "thin", color: "#E5E7EB" },
    top: { style: "thin", color: "#CBD5E1" },
    bottom: { style: "thin", color: "#CBD5E1" },
  };
  const table = sheet.tables.add(address, true, safeTableName(tableName));
  table.style = "TableStyleMedium2";
  table.showFilterButton = true;
  fullRange.format.autofitColumns();
  fullRange.format.autofitRows();

  const headers = values[0];
  headers.forEach((headerName, i) => {
    const columnRange = sheet.getRangeByIndexes(0, i, rowCount, 1);
    if (numericFields.has(headerName)) {
      columnRange.format.numberFormat = "#,##0.00;[Red]-#,##0.00;-";
      columnRange.format.horizontalAlignment = "right";
    } else if (dateFields.has(headerName)) {
      columnRange.format.numberFormat = "yyyy-mm-dd";
    } else if (["raw_line", "raw_row", "description", "description_raw", "message", "source_refs"].includes(headerName)) {
      columnRange.format.wrapText = true;
      columnRange.format.columnWidthPx = 300;
    } else if (["filename", "source_ref"].includes(headerName)) {
      columnRange.format.wrapText = true;
      columnRange.format.columnWidthPx = 220;
    } else if (String(headerName).endsWith("_id") || textFields.has(headerName)) {
      columnRange.format.numberFormat = "@";
      columnRange.format.columnWidthPx = Math.max(120, Math.min(220, (String(headerName).length + 4) * 9));
    }
  });
}

function writeSimpleTable(sheet, startRow, title, headers, rows, tableName) {
  const colCount = headers.length;
  const titleRange = sheet.getRangeByIndexes(startRow, 0, 1, colCount);
  titleRange.merge();
  titleRange.values = [[title]];
  titleRange.format = {
    fill: "#143D59",
    font: { bold: true, color: "#FFFFFF", size: 12 },
  };
  const matrix = [headers, ...rows];
  const dataRange = sheet.getRangeByIndexes(startRow + 1, 0, matrix.length, colCount);
  dataRange.values = matrix;
  const headerRange = sheet.getRangeByIndexes(startRow + 1, 0, 1, colCount);
  headerRange.format = {
    fill: "#E2E8F0",
    font: { bold: true, color: "#0F172A" },
    wrapText: true,
  };
  dataRange.format.borders = {
    insideHorizontal: { style: "thin", color: "#E5E7EB" },
    top: { style: "thin", color: "#CBD5E1" },
    bottom: { style: "thin", color: "#CBD5E1" },
  };
  const address = `${colName(0)}${startRow + 2}:${colName(colCount - 1)}${startRow + 1 + matrix.length}`;
  const table = sheet.tables.add(address, true, safeTableName(tableName));
  table.style = "TableStyleMedium2";
  dataRange.format.autofitColumns();
  dataRange.format.autofitRows();
  return startRow + matrix.length + 3;
}

function addStatusFill(sheet, rangeAddress) {
  const range = sheet.getRange(rangeAddress);
  range.conditionalFormats.add("containsText", {
    text: "OK",
    format: { fill: "#DCFCE7", font: { color: "#166534", bold: true } },
  });
  range.conditionalFormats.add("containsText", {
    text: "FAIL",
    format: { fill: "#FEE2E2", font: { color: "#991B1B", bold: true } },
  });
  range.conditionalFormats.add("containsText", {
    text: "needs_review",
    format: { fill: "#FEF3C7", font: { color: "#92400E", bold: true } },
  });
}

async function main() {
  await fs.mkdir(outputDir, { recursive: true });

  const tableEntries = await Promise.all(tableFiles.map(([tableName, fileName]) => readTable(tableName, fileName)));
  const tables = Object.fromEntries(tableEntries.map((entry) => [entry.tableName, entry]));
  const acceptanceReport = JSON.parse(await fs.readFile(path.join(sourceDir, "acceptance_report.json"), "utf8"));
  const checks = buildChecks(tables, acceptanceReport);
  const pending = pendingItems(tables);

  const workbook = Workbook.create();

  const cover = workbook.worksheets.add("验收总览");
  cover.showGridLines = false;
  cover.getRange("A1:H1").merge();
  cover.getRange("A1").values = [[reportTitle]];
  cover.getRange("A1").format = {
    fill: "#143D59",
    font: { bold: true, color: "#FFFFFF", size: 16 },
  };
  cover.getRange("A3:B9").values = [
    ["生成时间", generatedDate],
    ["范围", reportScope],
    ["解析器版本", acceptanceReport.parser_version],
    ["P0 验收状态", acceptanceReport.status],
    ["跨表校验", checks.every((r) => r[2] === "OK") ? "OK" : "FAIL"],
    ["固化建议", pending.length === 0 ? "可固化" : "可进入固化候选，但需先确认待确认项"],
    ["说明", "本文件只整理原始事实与证据，不在此处计算税务、Lot 或最终收益。"],
  ];
  cover.getRange("A3:A9").format = { fill: "#E2E8F0", font: { bold: true, color: "#0F172A" } };
  cover.getRange("A3:B9").format.borders = { preset: "all", style: "thin", color: "#CBD5E1" };
  cover.getRange("B9").format.wrapText = true;
  cover.getRange("A:B").format.autofitColumns();
  cover.getRange("B:B").format.columnWidth = 58;

  const counts = tableFiles.map(([tableName, fileName, role]) => [
    tableName,
    sheetNames[tableName],
    role,
    tables[tableName].rows.length,
    fileName,
  ]);
  let nextRow = writeSimpleTable(
    cover,
    11,
    "表清单与行数",
    ["table_name", "sheet", "中文名", "行数", "来源 CSV"],
    counts,
    "overview_counts",
  );
  const cashSummary = businessSummaryRows(tables.cash_ledger_entries.rows);
  writeSimpleTable(
    cover,
    nextRow,
    "现金流水按 business_type / currency 汇总",
    ["business_type", "currency", "行数", "金额合计"],
    cashSummary,
    "overview_cash_summary",
  );
  cover.getRange("D:D").format.numberFormat = "#,##0";
  cover.getRange("H:H").format.numberFormat = "#,##0.00;[Red]-#,##0.00;-";

  const map = workbook.worksheets.add("表结构地图");
  const mapRows = tableFiles.map(([tableName, , zh]) => [
    tableName,
    zh,
    tableRoles[tableName],
    ["cash_ledger_entries", "market_trades", "fund_orders", "corporate_action_cash_legs", "asset_movement_events", "financing_interest_events", "stock_yield_cash_entries"].includes(tableName)
      ? "核心事实/主事件"
      : tableName.includes("evidence") || tableName.includes("transactions") || tableName.includes("fee_lines")
        ? "证据表"
        : tableName.includes("issues") || tableName.includes("governance")
          ? "治理表"
          : "关系/子表",
  ]);
  formatTableSheet(map, [["table_name", "中文名", "定位", "层级"], ...mapRows], "table_map");
  map.getRange("C:C").format.columnWidth = 72;
  map.getRange("C:C").format.wrapText = true;

  const dictionary = workbook.worksheets.add("字段字典");
  const dictRows = [];
  tableFiles.forEach(([tableName]) => {
    tables[tableName].headers.forEach((field) => {
      const [label, definition] = fieldDescriptions[field] ?? [field, "当前字段沿用 parser v1 输出命名；后续 SQL schema 固化时可补充更严格类型。"];
      const role = numericFields.has(field)
        ? "decimal/number"
        : dateFields.has(field)
          ? "date"
          : textFields.has(field) || field.includes("id")
            ? "text/id"
            : "text/enum";
      dictRows.push([tableName, sheetNames[tableName], field, label, role, definition]);
    });
  });
  formatTableSheet(dictionary, [["table_name", "sheet", "field_name", "中文名", "类型/口径", "定义"], ...dictRows], "field_dictionary");
  dictionary.getRange("F:F").format.columnWidth = 72;
  dictionary.getRange("F:F").format.wrapText = true;

  const checkSheet = workbook.worksheets.add("校验结果");
  formatTableSheet(checkSheet, [["check_code", "校验内容", "状态", "实际值", "期望值", "说明"], ...checks], "checks");
  addStatusFill(checkSheet, `C2:C${checks.length + 1}`);
  checkSheet.getRange("B:B").format.columnWidth = 56;
  checkSheet.getRange("F:F").format.columnWidth = 60;
  checkSheet.getRange("B:F").format.wrapText = true;

  const pendingSheet = workbook.worksheets.add("待确认项");
  formatTableSheet(
    pendingSheet,
    [["状态", "类别", "statement_id", "source_ref", "对象/类型", "事实摘要", "建议处理"], ...pending],
    "pending_items",
  );
  addStatusFill(pendingSheet, `A2:A${Math.max(2, pending.length + 1)}`);
  pendingSheet.getRange("F:G").format.columnWidth = 64;
  pendingSheet.getRange("D:G").format.wrapText = true;

  tableEntries.forEach((entry) => {
    const sheet = workbook.worksheets.add(sheetNames[entry.tableName]);
    formatTableSheet(sheet, entry.values, entry.tableName);
  });

  const formulaErrors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 300 },
    summary: "final formula error scan",
  });
  console.log(formulaErrors.ndjson);

  const overviewInspect = await workbook.inspect({
    kind: "table",
    sheetId: "验收总览",
    range: "A1:H35",
    include: "values",
    tableMaxRows: 35,
    tableMaxCols: 8,
    maxChars: 6000,
  });
  console.log(overviewInspect.ndjson);

  const renderTargets = [
    "验收总览",
    "表结构地图",
    "字段字典",
    "校验结果",
    "待确认项",
    ...tableFiles.map(([tableName]) => sheetNames[tableName]),
  ];
  for (const sheetName of renderTargets) {
    const preview = await workbook.render({
      sheetName,
      autoCrop: "all",
      scale: 1,
      format: "png",
    });
    const previewPath = path.join(outputDir, `preview-${sheetName}.png`);
    await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
    console.log(`rendered ${sheetName} -> ${previewPath}`);
  }

  const xlsx = await SpreadsheetFile.exportXlsx(workbook);
  await xlsx.save(outputPath);
  console.log(JSON.stringify({ outputPath, checks: checks.length, pending: pending.length, tables: tableFiles.length }, null, 2));
}

await main();
