# Personal Investment Data Management

个人投资数据全面管理系统的脱敏工程基线。

本仓库用于承接“计算逻辑与前端展示服务”的独立开发，不包含任何个人原始结单、SQLite 数据库、Excel 审阅包、CSV 导出、PDF、截图或真实收益/持仓金额。

## 当前定位

本仓库只提供：

- 项目需求与边界说明
- 数据库 schema
- Lot / Allocation 与收益口径决策
- 校验 gate 与入库流程约束
- 可复用的管理 / 校验 / allocation 工具代码
- 前端与收益计算层的交接说明

本仓库不提供：

- 原始富途结单 PDF
- `investment.sqlite` 或任何 SQLite 数据库
- Excel / CSV / cache / exports
- 真实交易流水、持仓、收益金额、账户号或用户身份信息
- 税务建议或申报结论

## 推荐下一步

新的计算与前端工程建议从这里开始：

1. 读取 `docs/research/return-treatment-profile-design-v1.md` 和收益口径决策。
2. 基于 `schema/` 理解原始事实层、管理层和 Lot / Allocation 层。
3. 设计 `return_items`、`return_summary_*` 和 profile switch。
4. 用本地私有数据库验证计算结果，但不要把私有数据库或派生金额提交到 Git。
5. 前端先展示富途数据结构，预留 `canonical_instrument_id` 支持多平台同标的汇总。
6. 多平台账户展示使用 `canonical_account_id` 和 `account_group_id`，不要直接用原始券商账号做汇总主键。

## 目录

| 路径 | 说明 |
| --- | --- |
| `docs/Requirement.md` | 需求与边界。 |
| `docs/decisions/` | 已确认的关键决策。 |
| `docs/research/return-treatment-profile-design-v1.md` | 收益口径与 profile 方案。 |
| `docs/context/data-ingestion-validation-gate-v1.md` | 新结单入库校验 gate。 |
| `docs/frontend-handoff-brief.md` | 给计算 / 前端工程的交接说明。 |
| `schema/` | SQLite schema。 |
| `tools/` | 脱敏工具代码，不含真实数据。 |
| `examples/` | 占位说明；不要放真实样本。 |

## 最新补充：统一标的映射层

前端和收益计算需要先把平台原始标的映射为 `canonical_instrument_id`，否则同一标的会在汇总和明细中显示成不同名称。相关文件：

- `docs/decisions/2026-06-29-canonical-instrument-mapping-layer.md`
- `schema/canonical_instrument_mapping_schema_v1.sql`
- `tools/canonical_instrument_mapping_cli.py`

本地私有数据库上可运行：

```bash
python tools/canonical_instrument_mapping_cli.py run --db-path "$INVESTMENT_DB_PATH" --reset
```

## 最新补充：统一账户映射层

账户维度分三层：

- `accounts` / `statement_accounts`：原始平台账户和结单账户证据。
- `canonical_accounts` / `canonical_account_mappings`：同一真实券商账户链路，处理账号升级、子账户合并和平台内部结构变化。
- `account_groups` / `account_group_memberships`：跨平台、券商、组合、策略或税务视角的账户分组。

相关 schema：

- `schema/canonical_account_mapping_schema_v1.sql`

前端展示交易明细时，可以读 `v_market_trades_with_accounts`、`v_fund_orders_with_accounts`、`v_cash_ledger_entries_with_accounts` 等 account enriched views，同时展示原始账户和统一账户。跨平台汇总时按 `canonical_account_id` join `v_account_group_memberships` 过滤。

## 隐私原则

如果一个文件可以反推出用户持仓、交易、收益、账户、结单文件名、路径或身份信息，就不要提交。

更具体的规则见 `DATA_PRIVACY.md`。
