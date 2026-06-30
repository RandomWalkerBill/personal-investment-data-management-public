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
| `ipo_review_items.csv` | 待复核项和现金链路解释。 |

## 核对重点

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
sale_proceeds > 0
realized_pnl != 0
```

说明 IPO 中签 lot 已经和后续卖出交易形成完整收益闭环。

如果 `remaining_quantity > 0`，说明该 IPO lot 仍持有或后续卖出数据缺失。

## 当前边界

- IPO 报告是只读审阅层，不会修改数据库。
- 它依赖已经完成的 parser 和 Lot / Allocation。
- 它不会自动判断税务口径。
- 它不会把融资利息硬分摊到 IPO，融资利息仍应作为期间费用或后续 profile 策略处理。
- 如果券商格式变了，报告会把无法匹配的现金腿 / lot 放进 review items，而不是静默合并。
