# 计算逻辑与前端展示交接说明

## 目标

基于现有原始事实层和 Lot / Allocation 层，设计收益计算层与前端展示服务。新工程只读本地私有数据库，不修改原始事实层，不上传个人数据。

## 第一版默认口径

默认 profile 为 `default_economic`：

| 项目 | 默认处理 |
| --- | --- |
| 买入费用 / 税费 | 资本化到 lot cost。 |
| 卖出费用 / 税费 | 扣减 proceeds。 |
| IPO 中签本金 | 资本化到 IPO lot。 |
| IPO 显式申购费 | 若生成中签 lot，资本化到 IPO lot；未中签则作为 IPO activity period expense。 |
| IPO 中签隐含费用 / levy | 资本化到 IPO lot，并单独列示。 |
| 融资利息 | 默认期间费用，不自动分摊到个股 / IPO。 |
| 股息 | 按 gross / withholding / fee / net 展开，默认 net 进入总经济收益。 |
| 证券出借 / 股票收益计划 | 投资收入。 |
| 券商奖励 | 其他账户收入，默认单独展示。 |
| 外部出入金 | 非收益现金流，不进入收益。 |
| opening lot | `provisional`，必须与 `final` 分开展示。 |

## 建议新增计算层

### 先决条件：统一标的映射层

在生成 `return_items` 之前，必须先完成 platform instrument 到 canonical instrument 的映射。前端不应该直接拼接 raw code / raw name 来展示标的。

推荐链路：

`raw fact -> instrument resolution / master data -> lot/allocation -> return_items -> frontend`

最小字段要求：

| 字段 | 说明 |
| --- | --- |
| `instrument_key` | 平台或来源表中的标的 key。 |
| `canonical_instrument_id` | 系统统一标的 id。 |
| `canonical_symbol` | 统一展示代码。 |
| `canonical_display_name` | 统一展示名称。 |
| `instrument_mapping_status` | `auto`, `manual_confirmed`, `needs_review`, `unmapped`。 |

相关 schema：`schema/canonical_instrument_mapping_schema_v1.sql`。

### 先决条件：统一账户映射层

多平台展示不能直接用券商原始账户号或子账户名做汇总主键。账户维度分三层：

| 层 | 说明 |
| --- | --- |
| raw account | 平台 / 结单原始账户，用于追溯和对账。 |
| canonical account | 同一真实券商账户链路，用于处理账户升级、子账户合并和单券商账户汇总。 |
| account group | 多个 canonical account 的分组，用于跨平台、券商、组合、策略或税务视角筛选。 |

前端明细优先读取 account enriched views，例如：

- `v_market_trades_with_accounts`
- `v_fund_orders_with_accounts`
- `v_cash_ledger_entries_with_accounts`
- `v_asset_movement_events_with_accounts`

展示字段建议：

| 字段 | 说明 |
| --- | --- |
| `raw_account_id` / `raw_account_label` | 原始账户证据。 |
| `canonical_account_id` / `canonical_account_label` | 统一账户。 |
| `canonical_account_platform` / `canonical_account_broker` | 平台 / 券商筛选。 |
| `account_group_id` | 通过 `v_account_group_memberships` join 获得，用于跨平台分组筛选。 |

相关 schema：`schema/canonical_account_mapping_schema_v1.sql`。

### `return_treatment_profiles`

收益口径定义表。第一版至少包含：

- `default_economic`
- `trading_decision`
- `tax_prepare`

### `return_treatment_rules`

规则表，用于把来源事实映射到收益桶。

关键字段建议：

| 字段 | 说明 |
| --- | --- |
| `profile_id` | 所属 profile。 |
| `source_table` | 来源表或视图。 |
| `business_type` | 标准业务类型。 |
| `event_subtype` | 子类型。 |
| `fee_tax_type` | 费用/税费类型。 |
| `instrument_type` | 标的类型。 |
| `pnl_bucket` | 收益桶。 |
| `cost_basis_effect` | 成本基础影响。 |
| `tax_policy` | 税务准备候选分类。 |
| `allocation_policy` | 分配策略。 |

### `return_items`

前端直接读取的收益明细表。

关键字段建议：

| 字段 | 说明 |
| --- | --- |
| `return_item_id` | 明细 id。 |
| `profile_id` | 收益口径。 |
| `calculation_run_id` | 计算 run。 |
| `source_table` / `source_pk` | 来源引用。 |
| `event_date` | 日期。 |
| `account_id` | 账户。 |
| `raw_account_id` | 原始账户 id。 |
| `canonical_account_id` | 统一账户 id。 |
| `account_group_id` | 可选，账户组筛选维度。 |
| `platform_id` | 平台 / 券商。 |
| `instrument_key` | 平台标的 key。 |
| `canonical_instrument_id` | 未来多平台合并标的 id。 |
| `currency` | 原币种。 |
| `amount` | 带符号金额。 |
| `pnl_bucket` | 收益桶。 |
| `pnl_sub_bucket` | 子桶。 |
| `pnl_status` | `final`, `provisional`, `pending`, `needs_review`。 |
| `cost_basis_status` | `final`, `provisional`, `not_applicable`。 |
| `source_refs` | 来源引用。 |
| `notes` | 说明。 |

## 前端第一版页面

| 页面 / 区块 | 说明 |
| --- | --- |
| 收益总览 | profile 切换、币种切换、final/provisional 分层。 |
| 分标的收益 | 按 canonical instrument 汇总，保留账户 / 平台 drilldown。 |
| 分账户 / 平台 | 当前先展示单平台，未来扩展多平台。 |
| 收益明细 | `return_items` 可筛选、排序、追溯来源。 |
| 费用与税费 | 交易费用、IPO 费用、融资利息、预扣税、ADR fee 分类展示。 |
| 口径管理 | profile、rule、override 管理。 |
| 校验状态 | 展示最新 parser / continuity / allocation / return calculation gate。 |

## 多平台预留

| 层 | 说明 |
| --- | --- |
| platform instrument | 券商原始代码或合约 id。 |
| canonical instrument | 系统统一标的 id，用于展示层合并。 |
| raw account | 券商原始账户 / 子账户，用于追溯。 |
| canonical account | 同一券商账户链路，用于处理账户升级。 |
| account group | 跨平台汇总和筛选。 |

默认规则：

1. 展示层可以按 canonical instrument 汇总。
2. Lot / Allocation 默认仍在 account + platform 维度计算。
3. 只有存在明确 asset transfer 链路时，才允许跨平台延续成本基础。

## 验收标准

1. 不修改 raw fact 表。
2. 不把真实数据库或收益金额提交到仓库。
3. 能生成 `return_items`，并从明细汇总到总览。
4. final/provisional 不混在一个数字里。
5. 每个收益项能追溯 source table / source pk / source refs。
6. 外部出入金不进入收益。
7. 同一费用不能既进入 lot cost，又作为 period expense 重复统计。
8. 标的列表和交易明细都使用 canonical instrument 字段；raw 字段只作为追溯信息。
