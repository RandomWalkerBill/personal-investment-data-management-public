# 收益口径与 Treatment Profile 方案 v1

日期：2026-06-29

## 背景

当前 2025 全年富途样本已经完成：

- 原始事实入库
- 现金 / 持仓连续性校验
- 正股 / IPO / 期权 / 基金 / 股票短仓 Lot / Allocation
- 分层 realized PnL

下一步需要把“收益到底怎么算、费用放到哪里、不同展示口径如何切换”正式设计出来，为后续前端展示、管理功能、口径切换和多平台汇总打基础。

用户已确认的原则：

1. 相关费用尽可能往成本上靠。
2. 不能资本化或无法安全归属到具体 lot 的，再作为期间费用。
3. 现阶段先把富途数据清楚展示出来。
4. 未来多平台进入后，同一个标的应能合并管理，但保留平台 / 账户维度。

## 外部参考摘要

本轮参考了以下公开资料，用作数据系统设计参考，不作为税务建议：

| 来源 | 对本项目的启发 |
| --- | --- |
| IRS Publication 551 | 股票/债券成本基础通常是买入价加购买相关成本；basis 用于计算出售损益，并要求保留影响 basis 的记录。参考：https://www.irs.gov/publications/p551 |
| IRS Publication 550 | 出售时的 amount realized 是收到的价值减去与出售相关费用；成本基础可用 specific identification 或 FIFO 等方法。参考：https://www.irs.gov/publications/p550 |
| Portfolio Performance: Acquisition cost methodology | 市面投资组合工具会同时支持 FIFO 和移动平均；不同成本法影响 realized/unrealized 的分配，但总经济结果应可解释。参考：https://help.portfolio-performance.info/en/concepts/cost-methodology/ |
| Portfolio Performance: Buy/Sell transaction | 买入交易通常按股数 * 价格 + fees + taxes 得到实际扣款；多币种下交易币种、账户币种、费用币种要分开。参考：https://help.portfolio-performance.info/en/reference/transaction/buy-sell/ |
| Portfolio Performance: Fees & taxes | 费用税费既可能随买卖交易记账，也可能在不同时间单独记录，因此需要独立事实与关联机制。参考：https://help.portfolio-performance.info/en/reference/transaction/fees-taxes/ |
| Portfolio Performance: TWR / MWR | 绩效口径要区分外部出入金和组合内部交易；TWR 用于剔除入出金影响，MWR/IRR 反映资金规模和时点影响。参考：https://help.portfolio-performance.info/en/concepts/performance/time-weighted/ 和 https://help.portfolio-performance.info/en/concepts/performance/money-weighted/ |

核心结论：

- 成本基础 / realized PnL 与绩效回报率不是一件事。前者回答“这笔卖出赚亏多少”，后者回答“组合表现如何”。
- 买卖相关费用应优先进入买入成本或卖出净收入，避免在期间费用里重复统计。
- 税务口径可能与个人复盘口径不同，因此不能把唯一税务规则写死在原始事实层。
- 多账户、多平台下，lot / tax lot 仍应保留账户维度；展示层可以按 canonical instrument 合并。

## Profile 分层

建议第一版支持三个 profile。

| profile_id | 名称 | 目的 | 默认使用 |
| --- | --- | --- | --- |
| `default_economic` | 默认经济口径 | 尽量把交易相关费用资本化 / 净额化，剩余费用作为期间费用，得到最清晰的经济收益。 | 是 |
| `trading_decision` | 个人交易决策口径 | 用于看交易行为本身是否有效，融资利息、券商奖励等沉没成本或外部奖励单独展示。 | 否 |
| `tax_prepare` | 税务准备口径 | 不直接给税务结论，只输出成本、收入、预扣税、费用、来源证据和待确认项。 | 否 |

### default_economic

目标：清楚反映“这一年账户内投资事实带来的经济结果”。

规则：

| 事实 | 默认处理 |
| --- | --- |
| 二级市场买入费用 / 税费 | 资本化到 lot cost。 |
| 二级市场卖出费用 / 税费 | 从卖出收入中扣除，使用 net proceeds。 |
| IPO 中签本金 | 资本化到 IPO lot。 |
| IPO 显式申购费 | 若中签，资本化到 IPO lot；若未中签且无持仓生成，则作为 IPO activity period expense。 |
| IPO 中签隐含费用 / levy | 默认用 `中签金额 * 1.0085%` 资本化到 IPO lot，并单独列示。 |
| 基金申购 / 赎回 | 申购生成 fund lot；赎回按 FIFO 产生 realized PnL。 |
| 基金交易费用 | 若与申购/赎回直接绑定，申购资本化、赎回扣减 proceeds；当前样本非 0 基金费用为 0。 |
| 期权开平仓费用 | 包含在 signed cash 中，随期权 lot 关闭确认 realized PnL。 |
| 股票短仓 | signed cash 口径，卖空开仓现金 + 买回平仓现金。 |
| 股息 | 作为 investment income，按 gross / withholding / fee / net 分列。 |
| 预扣税 / ADR fee / 公司行动手续费 | 不进入股票 lot 成本；作为 income deduction / tax withheld 附在对应公司行动上。 |
| 融资利息 | 默认 period expense，不分摊到 IPO 或股票 lot；后续可增加估算分摊 profile。 |
| 股票收益计划 / 证券出借收入 | 作为 investment income。 |
| 券商奖励 / 卡券 | 默认作为 other account income 单独展示；是否纳入“投资收益”由前端过滤控制。 |
| 外部出入金 | 不属于收益，作为 performance-neutral flow。 |
| 换汇 | 当前样本未建 realized FX PnL，先保留现金事实；未来单独建 FX lot 或汇兑损益模型。 |
| opening lot | 历史成本未知时用期初市值，标记 `provisional`，前端必须提示。 |

### trading_decision

目标：帮助用户复盘“交易本身好不好”，避免融资利息、券商奖励、税务扣缴等影响交易决策判断。

规则差异：

| 项目 | 处理 |
| --- | --- |
| 交易费用 / IPO 中签费用 | 仍进入交易成本，因为这是实际执行交易的刚性成本。 |
| 融资利息 | 从分标的交易 PnL 中剥离，展示为单独期间费用。 |
| 券商奖励 | 不计入交易 PnL，只在账户收益侧展示。 |
| 股息 / 证券出借收入 | 可在“持有收益”栏目展示，不并入“交易收益”。 |
| 未分摊费用 | 不强行摊到个股；进入“未归属期间费用”。 |

### tax_prepare

目标：为报税准备结构化资料，不在系统里假设具体税法结论。

规则：

| 输出 | 说明 |
| --- | --- |
| proceeds | 处置收入，保留交易币种和来源。 |
| cost_basis | lot 成本基础，含已资本化费用。 |
| realized_gain_loss | 按所选成本法计算的收益。 |
| income_items | 股息、利息、证券出借、基金收益等收入项。 |
| withholding_tax | 预扣税单独列示。 |
| expenses | 融资利息、独立费用等。 |
| provisional_flags | opening lot、手工估算、待结算、待确认项。 |
| source_refs | 每个项目必须能追溯到结单来源。 |

税务 profile 必须保留 `tax_policy=needs_jurisdiction`，直到用户明确税务主体和辖区。

## 当前样本验证方式

发布版不包含个人原始数据库、结单、交易流水、收益金额或持仓金额。实际项目中可以用同一套口径在本地私有数据库上生成验证结果，但不应将结果随公开工程提交。

建议验证输出只保留以下结构，不保留真实金额：

### 交易 realized PnL

| 币种 | final | provisional | 说明 |
| --- | ---: | ---: | --- |
| HKD | `<private>` | `<private>` | final 来自本期新增 lot；provisional 主要来自 opening lot。 |
| USD | `<private>` | `<private>` | final 来自本期新增 lot；provisional 主要来自 opening lot。 |

### 投资收入与期间费用

| bucket | HKD | USD | 默认处理 |
| --- | ---: | ---: | --- |
| corporate_action_net | `<private>` | `<private>` | 投资收入净额；可展开 gross / withholding / fee。 |
| stock_yield_cash | `<private>` | `<private>` | 证券出借 / 股票收益计划收入。 |
| broker_reward | `<private>` | `<private>` | 其他账户收入，默认单独展示。 |
| financing_interest | `<private>` | `<private>` | 期间费用。 |

### 默认经济口径小计

| 币种 | final subtotal | provisional subtotal | total before FX / unrealized |
| --- | ---: | ---: | ---: |
| HKD | `<private>` | `<private>` | `<private>` |
| USD | `<private>` | `<private>` | `<private>` |

说明：

- final subtotal = final trading PnL + 投资收入 + 期间费用。
- provisional subtotal 主要来自 opening lot 期初市值口径，不等于历史真实税务成本。
- 前端必须默认把 final / provisional 分开展示，不能只给一个大总数。

## 数据模型建议

现有 `treatment_profiles` / `treatment_assignments` 可以作为第一版起点，但字段偏少。建议下一步新增计算层表，不覆盖 raw fact。

### return_treatment_profiles

| 字段 | 说明 |
| --- | --- |
| `profile_id` | `default_economic`, `trading_decision`, `tax_prepare`。 |
| `profile_name` | 展示名。 |
| `cost_method` | `fifo`, `moving_average`, `specific_id`；当前默认 `fifo`。 |
| `base_currency_policy` | `native_currency_only`, `reporting_currency_with_fx`；当前默认 native only。 |
| `tax_jurisdiction` | 税务辖区；当前为空或 `needs_jurisdiction`。 |
| `status` | `active`, `draft`, `deprecated`。 |

### return_treatment_rules

| 字段 | 说明 |
| --- | --- |
| `rule_id` | 规则 id。 |
| `profile_id` | 所属 profile。 |
| `rule_order` | 规则优先级。 |
| `source_table` | 来源表或视图。 |
| `business_type` | 标准业务类型。 |
| `event_subtype` | 子类型。 |
| `fee_tax_type` | 费用/税费类型，可为空。 |
| `instrument_type` | `stock_or_etf`, `option`, `fund`, `cash`, `unknown`。 |
| `pnl_bucket` | `realized_trading_pnl`, `investment_income`, `period_expense`, `other_income`, `external_flow`, `unrealized_pnl`, `excluded`。 |
| `cost_basis_effect` | `capitalized_to_lot`, `reduces_proceeds`, `period_expense`, `income_deduction`, `no_effect`, `needs_review`。 |
| `tax_policy` | `tax_prepare_only`, `needs_jurisdiction`, `not_tax_relevant`, `withholding_tax`, `ordinary_income_candidate`, `capital_gain_candidate`。 |
| `allocation_policy` | `direct`, `fifo`, `pro_rata`, `period`, `manual_required`, `none`。 |

### return_items

`return_items` 是 profile 运行后的标准收益明细表，供前端直接展示。

| 字段 | 说明 |
| --- | --- |
| `return_item_id` | 明细 id。 |
| `profile_id` | 使用的收益口径。 |
| `calculation_run_id` | 计算 run。 |
| `source_table` / `source_pk` | 来源。 |
| `event_date` | 日期。 |
| `account_id` | 账户。 |
| `platform_id` | 平台 / 券商。 |
| `instrument_key` | 当前平台或标准标的 key。 |
| `canonical_instrument_id` | 未来多平台合并标的 id。 |
| `currency` | 原币种。 |
| `amount` | 带符号金额。 |
| `pnl_bucket` | 收益桶。 |
| `pnl_sub_bucket` | 子桶，如 `dividend`, `withholding_tax`, `financing_interest`。 |
| `pnl_status` | `final`, `provisional`, `pending`, `needs_review`。 |
| `cost_basis_status` | `final`, `provisional`, `not_applicable`。 |
| `source_refs` | 来源引用。 |
| `notes` | 说明。 |

## 展示层建议

第一版前端先围绕富途数据展示。

### 顶层卡片

| 卡片 | 说明 |
| --- | --- |
| 总经济收益 | 当前 profile 下的 total，默认按币种展示，不强行换算。 |
| 交易已实现收益 | 来自 stock / IPO / option / fund / short stock allocation。 |
| 投资收入 | 股息、证券出借、现金利息等。 |
| 期间费用 | 融资利息、无法资本化的独立费用。 |
| Provisional 项 | opening lot、待结算、估算项。 |
| 待复核项 | needs_review / failed / unapproved warning。 |

### 核心视图

| 视图 | 说明 |
| --- | --- |
| 收益总览 | profile 切换、币种切换、final/provisional 分层。 |
| 分标的收益 | 按 canonical instrument 汇总，保留账户 / 平台 drilldown。 |
| 分账户 / 平台 | Futu 当前作为第一平台，未来支持 IBKR 等。 |
| 收益明细 | `return_items` 明细表，可追溯到 source ref。 |
| 费用与税费 | 交易费用、IPO 费用、融资利息、预扣税、ADR fee 分类展示。 |
| 口径管理 | treatment profile、rule、override 管理。 |

## 多平台扩展口子

未来多平台同一标的合并管理时，应区分两层 key，并在收益计算前新增统一标的映射层：

| 层 | 说明 |
| --- | --- |
| platform instrument | 券商原始代码，例如富途 `HK:00700`、IBKR 的合约 id。 |
| canonical instrument | 系统统一标的，例如 `HKEX:00700`，可挂 ISIN、CUSIP、SEDOL、交易所、币种。 |

默认规则：

1. 展示层可以按 canonical instrument 汇总。
2. lot / allocation 默认仍在 account + platform 维度内计算，避免跨券商税务和成本基础混淆。
3. 若发生转仓，必须有 asset_transfer 链路，才允许把成本基础从一个平台延续到另一个平台。
4. 未来税务 profile 可选择按账户、平台、税务主体或全组合聚合。

补充规则：

1. raw fact 层继续保留券商原始代码、原始名称和原始备注。
2. instrument resolution / master data 层负责将 platform instrument 映射到 `canonical_instrument_id`。
3. return calculation 必须把 `canonical_instrument_id`、`canonical_symbol`、`canonical_display_name` 写入 `return_items` 或 enriched view。
4. 前端默认展示 canonical 字段，raw 字段只作为来源追溯和复核信息。

## 需要决策的问题

本轮建议先确认以下 v1 默认值：

1. `default_economic` 作为默认展示口径。
2. 交易相关费用优先资本化或扣减 proceeds，避免重复作为期间费用。
3. 融资利息默认期间费用，不自动分摊到个股 / IPO。
4. 股息展示 gross / withholding / fee / net，默认 net 进入总经济收益，同时保留明细。
5. opening lot 继续以 `provisional` 标识，前端分开展示。
6. 多平台合并先只在展示层按 canonical instrument 汇总，lot 仍按账户 / 平台计算。

## 下一步工程

1. 新增收益口径 schema：`return_treatment_rules`、`return_calculation_runs`、`return_items`、`return_summary_*`。
2. 新增收益计算 CLI：从 allocation 表和现金事实表生成 `return_items`。
3. 生成第一版富途收益总览 JSON / CSV，供前端开发。
4. 前端先做 profile switch、币种切换、final/provisional 分层、分标的 drilldown。
