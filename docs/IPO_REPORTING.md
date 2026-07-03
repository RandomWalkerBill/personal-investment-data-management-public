# IPO 专项提取与报告

IPO 是富途历史结单里最容易“看起来有现金流水、但实际链路不完整”的部分。本项目把 IPO 拆成三层事实：

| 层 | 表 | 含义 |
| --- | --- | --- |
| 现金事实 | `cash_ledger_entries` | 申购款、申购手续费、退款、其他 IPO 现金腿。 |
| 资产事实 | `asset_movement_events` | 中签 / 配发导致的股票数量增加。 |
| 成本和收益 | `position_lots`、`lot_cost_components`、`lot_allocations` | 中签 lot、成本组件、后续卖出 allocation 和 realized PnL。 |

## 为什么要单独做 IPO 报告

港股 IPO 退款金额可能已经扣除了中签本金和隐含费用。因此不能只看现金净额，也不能把 IPO 简单当作普通买入。

默认处理原则：

- 申购款、退款、显式申购手续费保留原始现金腿。
- 中签配发生成 `source_type = ipo_allotment` 的 lot。
- 中签本金进入 `ipo_allotment_principal`。
- 富途港股 IPO 的隐含中签费用默认按 `中签金额 * 1.0085%` 解释为 `ipo_allotment_fee_or_levy`。
- 显式 IPO handling fee 作为 `application_handling_fee` 成本组件。
- 后续卖出仍是普通 market trade，通过 FIFO allocation 与 IPO lot 关联。
- 融资利息来自账户级 margin interest，不默认分摊到单个 IPO lot；IPO 报告会单独列示已扣款和按日息证据归属的融资成本，并用较大值做保守收益扣减。

## 生成报告

```bash
python tools/ipo_report_cli.py \
  --db-path work/investment.sqlite \
  --output-dir reports/ipo
```

只看某个日期区间：

```bash
python tools/ipo_report_cli.py \
  --db-path work/investment.sqlite \
  --output-dir reports/ipo-2025 \
  --start-date 2025-01-01 \
  --end-date 2025-12-31
```

指定某次 allocation run：

```bash
python tools/ipo_report_cli.py \
  --db-path work/investment.sqlite \
  --allocation-run-id lot_allocation_futu_history_v1 \
  --output-dir reports/ipo
```

## 输出文件

| 文件 | 用途 |
| --- | --- |
| `ipo-report.md` | 人读版报告，包含摘要、IPO lots、现金腿和复核项。 |
| `ipo_cash_legs.csv` | IPO 现金腿明细。 |
| `ipo_asset_events.csv` | IPO 配发 / 资产变动明细。 |
| `ipo_lots.csv` | IPO 中签 lot、成本组件、卖出收益摘要。 |
| `ipo_sale_allocations.csv` | IPO lot 后续卖出 allocation、卖出净额和卖出费用估算。 |
| `ipo_financing_interest_summary.csv` | IPO 报告区间内融资利息摘要，包含已扣款、应计日息和保守估计。 |
| `ipo_financing_interest_details.csv` | 融资利息现金扣款和按日息证据汇总的明细。 |
| `ipo_strategy_summary.csv` | 策略口径摘要，适合前端或二次分析直接读取。 |
| `ipo_review_items.csv` | 待复核项和现金链路解释。 |

报告中的标的名称会优先使用 `canonical_instruments` / `platform_instrument_mappings` 的归一名称；如果归一层还没有名称，才保留 parser 抽到的原始描述。

## 核对重点

### 0. 先确认收益指标口径

报告里会同时展示几类金额，不要混用：

| 指标 | 含义 | 典型用途 |
| --- | --- | --- |
| `ipo_lot_total_cost` | 所有 IPO 中签 lot 的总成本，包含仍未卖出的 lot。 | 看中签 lot 总投入，不等于已实现成本。 |
| `sold_lot_cost` | 已卖出的 IPO lot 对应成本。 | 计算已实现收益的成本侧。 |
| `open_lot_cost` | 仍未卖出的 IPO lot 剩余成本。 | 校验持仓，不进入 realized PnL。 |
| `net_sale_proceeds` | IPO lot 卖出后的净现金流入，通常已经扣掉卖出交易费用。 | 计算已实现收益的收入侧。 |
| `realized_pnl_from_sold_lots` | 已卖出 IPO lot 的已实现收益。 | 看“中签并卖出”的收益。 |
| `cash_only_ipo_net_amount` | 有 IPO 现金腿但没有中签 lot 的净额，通常是未中签申购费。 | 还原打新策略整体费用。 |
| `ipo_strategy_realized_pnl_after_cash_only` | `realized_pnl_from_sold_lots + cash_only_ipo_net_amount`。 | 看“打新策略已实现收益”。 |
| `conservative_financing_interest_expense` | 同币种下取 `paid_financing_interest_expense` 和 `accrued_financing_interest_expense` 的较大值。 | 保守估计 IPO 行为带来的融资成本压力。 |
| `ipo_strategy_realized_pnl_after_conservative_financing_interest` | `ipo_strategy_realized_pnl_after_cash_only - conservative_financing_interest_expense`。 | 看扣除融资利息保守估计后的打新收益。 |

如果报告里还有未卖出的 IPO lot，`ipo_net_sale_proceeds - ipo_lot_total_cost` 不是正确的已实现收益，因为它把未卖出的成本也扣进去了。应优先看 `realized_pnl_from_sold_lots`；如果要按打新策略口径扣除未中签申购费，看 `ipo_strategy_realized_pnl_after_cash_only`；如果要保守考虑 IPO 融资成本，看 `ipo_strategy_realized_pnl_after_conservative_financing_interest`。

### 1. 现金腿是否完整

每个真实 IPO 申购通常至少应该看到：

- `application_payment`
- `refund`
- 可选 `application_handling_fee`

如果只有 0 金额行，可能来自年度账单摘要，适合做历史补充，但不一定能还原日级现金占用。

### 2. 配发是否存在

中签应有 `asset_movement_events.business_type = ipo_subscription`，并且后续 Lot / Allocation 后应生成：

```text
position_lots.source_type = ipo_allotment
```

如果有现金腿但没有 lot，可能是：

- 未中签。
- 年度账单只记录申购活动摘要。
- 还没有跑 `lot_allocation_cli.py`。
- parser 没识别到配发资产腿。

### 3. 隐含差额是否解释

报告中的 `ipo_cash_reconciliation` 会列出：

```text
application_payment_abs
refund
allotment_principal
implied_diff
explicit_handling_fee
```

对于富途港股 IPO，`implied_diff` 通常应接近中签金额的 `1.0085%`。如果差异很大，需要检查：

- 是否有融资利息另行结算。
- 是否有多笔申购 / 多账户混在一起。
- 是否缺少退款行。
- 是否年度账单只有净额。

### 4. 后续卖出是否闭环

如果 `ipo_lots.csv` 里：

```text
remaining_quantity = 0
net_sale_proceeds > 0
realized_pnl != 0
```

说明 IPO 中签 lot 已经和后续卖出交易形成完整收益闭环。

如果 `remaining_quantity > 0`，说明该 IPO lot 仍持有或后续卖出数据缺失。

### 5. 卖出交易成本是否算进去

Lot / Allocation 使用的是卖出交易的净现金流，所以 `net_sale_proceeds` 和 `realized_pnl_from_sold_lots` 默认已经体现卖出交易费用。

为了让审阅更清楚，报告还会输出 `ipo_sale_allocations.csv`：

| 字段 | 含义 |
| --- | --- |
| `net_sale_proceeds` | allocation 分摊到该 IPO lot 的卖出净现金流入。 |
| `sell_fee_allocated_estimate` | 从 `market_trades.fee_total` 按成交数量分摊来的卖出费用估算。 |
| `gross_sale_amount_estimate` | `net_sale_proceeds + sell_fee_allocated_estimate`，用于反查成交额。 |
| `fee_source` | 费用来源。若为 `not_available`，说明当前库里没有足够的市场交易费用字段可审计。 |

如果 `sell_fee_allocated_estimate = 0`，不要立刻理解成“没有交易费”。需要看 `fee_source`：如果是 `not_available`，代表报告无法独立还原卖出费用，但 allocation 的净额仍可能已经扣费。

### 6. 融资利息是否算进去

IPO 报告会读取两类融资利息事实：

| 字段 | 含义 |
| --- | --- |
| `paid_financing_interest_expense` | `financing_interest_events` 中已经发生现金扣款的融资利息。 |
| `accrued_financing_interest_expense` | `financing_interest_evidence_items` 中按日息证据归属到报告日期区间的融资利息。 |
| `conservative_financing_interest_expense` | 上述两者的较大值。 |

默认不把融资利息分摊进单个 IPO lot，也不改写 lot cost。原因是券商通常按账户级融资统一计息，无法仅凭月结单稳定证明每一分钱属于哪一个 IPO。但港股打新很容易带来融资成本，因此 IPO 报告会用保守估计单独扣减策略收益：

```text
ipo_strategy_realized_pnl_after_conservative_financing_interest
  = ipo_strategy_realized_pnl_after_cash_only
  - conservative_financing_interest_expense
```

如果某个月的日息已经发生但现金扣款在下个月，`accrued_financing_interest_expense` 会比 `paid_financing_interest_expense` 更保守。报告会选择较大值，避免低估融资成本。

## 当前边界

- IPO 报告是只读审阅层，不会修改数据库。
- 它依赖已经完成的 parser 和 Lot / Allocation。
- 它不会自动判断税务口径。
- 它不会把融资利息硬分摊到单个 IPO lot；融资利息以账户级事实单独列示，并在 IPO 策略摘要里提供保守扣减指标。
- 如果券商格式变了，报告会把无法匹配的现金腿 / lot 放进 review items，而不是静默合并。
