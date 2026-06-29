# 收益口径 Profile v1：默认经济口径

日期：2026-06-29

## 状态

已采纳为 v1 设计基线，后续 schema、计算 CLI 和前端展示默认按本决策推进。

## 背景

2025 全年富途样本已经完成原始事实入库、跨月现金 / 持仓连续性校验，以及正股 / IPO / 期权 / 基金 / 股票短仓 Lot / Allocation。当前需要明确收益口径，支撑后续前端展示、收益汇总、分标的收益、费用税费查看和税务资料准备。

用户已确认的基本原则是：

1. 与交易直接相关、能够安全归属到 lot 或处置收入的费用，尽可能进入成本或扣减 proceeds。
2. 无法安全归属到具体 lot 的项目，作为期间费用或独立收入展示。
3. 原始事实层不被收益策略覆盖；收益口径只作为计算和展示层。
4. 未来允许不同 profile 切换，但第一版需要一个默认且清晰的口径。

## 决策

第一版默认收益口径设为 `default_economic`，中文名为“默认经济口径”。

该口径用于回答：在当前样本期内，账户内投资行为和相关现金收益 / 费用带来了多少经济结果。它不是税务申报结论，也不替代专业税务建议。

## 默认处理规则

| 项目 | v1 默认处理 |
| --- | --- |
| 二级市场买入费用 / 税费 | 资本化到 lot cost。 |
| 二级市场卖出费用 / 税费 | 从卖出收入中扣除，使用 net proceeds。 |
| IPO 中签本金 | 资本化到 IPO lot。 |
| IPO 显式申购费 | 若生成中签 lot，资本化到 IPO lot；若未中签且无持仓生成，作为 IPO activity period expense。 |
| IPO 中签隐含费用 / levy | 默认按 `中签金额 * 1.0085%` 资本化到 IPO lot，并单独列示。 |
| 基金申购 / 赎回 | 申购生成 fund lot；赎回按 FIFO 产生 realized PnL。 |
| 基金交易费用 | 若与申购 / 赎回直接绑定，申购资本化、赎回扣减 proceeds。 |
| 期权开平仓费用 | 包含在 signed cash 中，随期权 lot 关闭确认 realized PnL。 |
| 股票短仓 | 按 signed cash 计算，卖空开仓现金 + 买回平仓现金形成 realized PnL。 |
| 股息 | 作为 investment income，按 gross / withholding / fee / net 分列；默认 net 进入总经济收益。 |
| 预扣税 / ADR fee / 公司行动手续费 | 不进入股票 lot 成本；作为 income deduction / tax withheld 附在对应公司行动上。 |
| 融资利息 | 默认 period expense，不自动分摊到 IPO 或股票 lot。 |
| 股票收益计划 / 证券出借收入 | 作为 investment income。 |
| 券商奖励 / 卡券 | 作为 other account income 单独展示；是否纳入总收益由前端过滤控制。 |
| 外部出入金 | 不属于收益，作为 performance-neutral flow。 |
| 换汇 | 当前样本先保留现金事实；未来单独建立 FX lot 或汇兑损益模型。 |
| opening lot | 历史成本未知时使用期初市值，标记 `provisional`，前端必须与 final 分开展示。 |

## Profile 预留

除 `default_economic` 外，第一版预留两个 profile：

| profile_id | 中文名 | 用途 |
| --- | --- | --- |
| `trading_decision` | 个人交易决策口径 | 用于复盘交易行为本身，融资利息、券商奖励等单独展示，不强行摊到个股。 |
| `tax_prepare` | 税务准备口径 | 输出 proceeds、cost basis、realized gain/loss、income、withholding tax、expenses 和 source refs，不直接做税务结论。 |

## 展示约束

1. 前端默认按币种展示，不在 v1 中强制换算成单一报表币种。
2. final 与 provisional 必须分开展示，不能只给一个大总数。
3. 交易 realized PnL、投资收入、期间费用、其他账户收入、外部出入金必须分桶展示。
4. 市场交易费用明细、IPO 费用、融资利息、预扣税和 ADR fee 必须可 drilldown 到来源事实。
5. 当前多平台能力先在展示层按 `canonical_instrument_id` 汇总；lot / allocation 默认仍保留 account + platform 维度。
6. 只有存在转仓链路时，才允许把成本基础从一个平台延续到另一个平台。

## 样本验证边界

发布版不包含个人原始数据库、结单、交易流水、收益金额或持仓金额。实际项目中可以在本地私有数据库上运行同一口径，并用以下结构验证结果：

| 币种 | final trading PnL | provisional trading PnL | investment / other income | period expense | final subtotal | provisional subtotal |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| HKD | `<private>` | `<private>` | `<private>` | `<private>` | `<private>` | `<private>` |
| USD | `<private>` | `<private>` | `<private>` | `<private>` | `<private>` | `<private>` |

说明：

- investment / other income 应至少能拆分为公司行动净额、证券出借 / 股票收益计划收入、券商奖励等。
- final subtotal = final trading PnL + investment / other income + period expense。
- provisional subtotal 当前主要来自 opening lot 期初市值口径，不代表历史真实税务成本。

## 后续影响

后续工程应新增收益计算层，而不是改写原始事实层：

1. `return_treatment_profiles`
2. `return_treatment_rules`
3. `return_calculation_runs`
4. `return_items`
5. `return_summary_*`

前端第一版应围绕 profile 切换、币种展示、final/provisional 分层、分标的收益、费用税费明细和来源追溯构建。
