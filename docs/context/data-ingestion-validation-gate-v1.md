# 数据导入与校验闸门操作指引 v1

日期：2026-06-29

## 目标

后续每次新增结单时，不能直接写入正式投资数据库。必须先进入候选导入环境，完成自动校验；只有没有异常、没有待人工复核项时，才允许 promote / 写入正式主源。

本指引定义：

1. 新结单进入系统后的标准处理步骤。
2. 哪些校验必须自动执行。
3. 哪些异常必须人工介入。
4. 哪些状态允许正式落盘。

## 核心原则

| 原则 | 说明 |
| --- | --- |
| 正式库前置校验 | 新结单先生成 staging / candidate 数据库；正式 `investment.sqlite` 只接收通过 gate 的数据。 |
| 原始事实不可静默修正 | parser 看到什么、抽出什么、忽略什么，都必须可追溯。人工修正写 overlay，不覆盖 raw fact。 |
| 异常先复核后落盘 | 任何 failed / blocker / needs_review / 未批准 warning，都不能自动进入正式库。 |
| 快照只做校验 | 结单期初/期末余额和持仓快照用于对账，不作为收益、税务或持仓变化主计算源。 |
| 计算层可复跑 | continuity、lot/allocation、收益视图都必须可用同一批 raw fact 重跑。 |
| 允许信息型保留 | 0 金额、待结算、金额型证据行等可作为 info/skipped 保留，但必须是已知白名单语义。 |

## 数据状态分层

| 状态 | 可写位置 | 用途 | 是否正式入库 |
| --- | --- | --- | --- |
| source_cache | `cache/` | PDF 文本抽取、原始页缓存、运行中间件。 | 否 |
| staging_db | 临时 `futu_raw_fact.sqlite` 或候选 DB | parser 输出、审阅、连续性和 allocation 校验。 | 否 |
| review_queue | `review_items` / 审阅报告 / 待确认项 | 需要人工判断的问题。 | 否 |
| official_db | `exports/investment-db-v1/investment.sqlite` | 当前正式个人投资数据库主源。 | 是 |
| mirror_export | CSV / Excel / Feishu Base | 审阅、展示、只读镜像。 | 否，除非来自 official_db |

## 标准导入流程

### 1. 接收结单文件

检查项：

| 检查 | 通过标准 | 异常处理 |
| --- | --- | --- |
| 文件可读取 | PDF 存在、可打开、没有损坏。 | blocker，停止。 |
| 账户可识别 | 能识别券商账户、结单 period。 | blocker，停止。 |
| period 不冲突 | 不与已有正式结单重复，除非明确 replace。 | needs_review。 |
| 来源保留 | 文件名、路径、hash 或 source ref 可追溯。 | needs_review。 |

### 2. 运行 parser 到候选库

命令模板：

```bash
python3 tools/futu_ingest_cli.py \
  --pdf-dir <pdf-dir> \
  --run-id <candidate-run-id> \
  --db-path <candidate-db> \
  --review-xlsx <candidate-review-xlsx> \
  --replace-db \
  --strict
```

`--strict` 是导入 gate 的默认要求：parser 验收失败或存在待复核问题时，应返回非 0，不允许继续自动 promote。

必须检查：

| 检查 | 自动通过标准 | 异常处理 |
| --- | --- | --- |
| parser status | `passed` | failed -> blocker。 |
| review workbook | 跨表校验 OK，待确认项 0 | 有待确认项 -> needs_review。 |
| parser issues | 无 blocker / needs_review | 进入人工复核。 |
| unclassified tables | 只允许已知 layout noise | 新业务行 -> needs_review。 |
| source refs | 业务事实都有来源引用 | 缺 source_ref -> needs_review。 |

### 3. 候选数据库基础校验

命令模板：

```bash
python3 tools/investment_db_cli.py status --db-path <candidate-db>
sqlite3 <candidate-db> "pragma integrity_check;"
```

必须检查：

| 检查 | 自动通过标准 | 异常处理 |
| --- | --- | --- |
| SQLite integrity | `ok` | blocker。 |
| open review items | `0` | needs_review。 |
| required tables/views | raw fact + management schema 都存在 | blocker。 |
| row count sanity | 新 period 相关表行数非异常为 0 | needs_review。 |
| duplicate semantics | 不误合并真实重复流水，不重复入账 text/table 候选 | parser_fix_required。 |

### 4. 现金与持仓连续性校验

命令模板：

```bash
python3 tools/investment_continuity_check.py \
  --db-path <candidate-db> \
  --import-run-id <candidate-run-id> \
  --run-id <continuity-run-id> \
  --report-md <continuity-report-md>
```

必须通过：

| 校验 | 自动通过标准 | 异常处理 |
| --- | --- | --- |
| cash_carry_forward | failed=0, missing_anchor=0 | needs_review 或 parser_fix_required。 |
| cash_flow_reconciliation | failed=0 | 逐项定位 parser 漏抽、业务解释或新类型。 |
| position_quantity_carry_forward | failed=0, missing_anchor=0 | needs_review。 |
| position_pending_amount_carry_forward | failed=0 | needs_review。 |

说明：

- 当前 2025 全年 run `continuity_2025_full_v4` 已证明这些检查可以作为硬 gate。
- 新月份如果是第一个月或跨年边界，缺少前后锚点可以进入 `missing_anchor`，但不能自动正式落盘；必须人工确认为合理边界。

### 5. Lot / Allocation 校验

命令模板：

```bash
python3 tools/lot_allocation_cli.py run \
  --db-path <candidate-db> \
  --run-id <allocation-run-id> \
  --replace
```

必须通过：

| 校验 | 自动通过标准 | 异常处理 |
| --- | --- | --- |
| allocation run status | `passed` | failed / passed_with_warnings 均进入复核。 |
| lot allocation validation | failed=0 | needs_review 或模型修复。 |
| close quantity allocated | 全部通过 | 若 insufficient lot，检查 opening lot、IPO、买入漏抽。 |
| ending position match | 全部通过 | 检查持仓快照或交易/资产变动漏抽。 |
| option ending position match | 全部通过 | 检查期权解析、到期、指派/行权。 |
| fund ending position match | 全部通过 | 检查基金订单、待结算和跨期申赎。 |
| short stock closed/carried | 已平仓通过；未平仓必须有清晰状态 | 未识别短仓进入复核。 |

当前允许的信息型 skipped：

| check_code | 允许条件 |
| --- | --- |
| `fund_amount_only_order_without_units` | 仅当该行被确认是金额型证据或跨期 pending/cash 事实，且不参与 lot 数量计算。 |

其他 skipped / warning 默认不允许自动正式落盘，除非新增决策文档把它列入白名单。

### 6. 正式 promote / 更新主库

只有满足以下条件时才允许进入正式库：

| 条件 | 要求 |
| --- | --- |
| parser | passed |
| review items | 0 |
| DB integrity | ok |
| continuity | passed，failed=0，missing_anchor=0，或边界 missing 已人工确认 |
| lot/allocation | passed，failed=0 |
| warnings | 0，或全部在已批准白名单 |
| skipped | 仅限 info 且已在本指引或决策文档白名单中 |

正式 promote 命令模板：

```bash
python3 tools/investment_db_cli.py promote \
  --source-db <candidate-db> \
  --target-db exports/investment-db-v1/investment.sqlite \
  --replace \
  --record-path exports/investment-db-v1/promotion-record.json
```

如果是增量工程化实现，未来应改为“候选库通过后生成新版本正式库”，而不是直接原地覆盖。

### 7. 导出与镜像

正式库通过后才允许生成：

| 输出 | 条件 |
| --- | --- |
| Excel/CSV 审阅包 | 可从 staging 生成，但需标记 candidate。 |
| Lot / Allocation CSV | 仅正式通过后作为正式结果使用。 |
| Feishu Base 镜像 | 仅从 official_db 单向导出。 |
| Feishu Wiki 同步 | 仅同步已落盘文档，不作为数据库真相源。 |

## 异常分级

| 级别 | 定义 | 是否自动正式落盘 |
| --- | --- | --- |
| blocker | 文件不可读、数据库损坏、账户/period 无法识别、核心 schema 缺失。 | 否 |
| needs_review | 新业务类型、现金不闭合、持仓不闭合、未匹配现金腿、未知公司行动、未解释 allocation 差异。 | 否 |
| warning | 不影响主账闭合但可能影响解释质量，例如名称映射不完整、展示字段异常。 | 默认否，除非白名单。 |
| info | 已知且不影响主账的状态，例如 0 金额行、已确认待结算、金额型证据行。 | 可自动落盘。 |

## 人工介入 SOP

| 场景 | 处理 |
| --- | --- |
| parser 漏抽 / 漂移 | 修 parser，重新生成 candidate run，不在正式库手改 raw fact。 |
| 映射缺失 | 写入 mapping override 或补规则，重新运行校验。 |
| 业务含义不明 | 进入 review item，用户确认后写 decision / research。 |
| 数据真实但模型未覆盖 | 新增 schema / calculation 模型，重新跑完整 gate。 |
| 少量人工补录 | 写入 `manual_events` / `manual_event_legs`，并重新跑 continuity / allocation。 |
| 人工修正 | 写入 `manual_corrections` overlay，不覆盖 raw fact。 |

人工处理完成后，必须重新跑对应 gate。不能只把异常标为已读后直接正式落盘。

## 当前 v1 白名单

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 0 金额现金腿 / 资产腿 | info | 保留事实，不影响金额闭合。 |
| 期末待结算基金申购 | info | 可进入 `pending_settlement`，不参与当期期末持仓校验。 |
| 金额型基金证据行 | info/skipped | 无份额时不生成 fund lot/allocation，但原始事实保留。 |
| IPO 显式 handling fee 缺失 | info | 若原始现金事实确实没有 HKD 100，则按 0 保留，不臆造。 |

## 当前工程缺口

现在已经有分散 CLI：

- `futu_ingest_cli.py`
- `investment_continuity_check.py`
- `lot_allocation_cli.py`
- `investment_db_cli.py`

但还没有一个统一命令把上述 gate 串成“候选导入 -> 校验 -> 通过才 promote”的完整自动化流程。下一步建议新增一个 orchestration CLI，例如：

```bash
python3 tools/investment_import_gate_cli.py run \
  --pdf-dir <pdf-dir> \
  --candidate-run-id <run-id> \
  --official-db exports/investment-db-v1/investment.sqlite
```

该命令应在发现异常时停止，并输出 anomaly report / review items；只有所有 gate 通过时才执行正式 promote。
