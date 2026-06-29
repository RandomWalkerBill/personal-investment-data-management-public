# 决策：正式入库前必须通过数据校验闸门

日期：2026-06-29

## 背景

2025 全年富途结单已经完成端到端跑通：PDF/parser、raw fact、正式数据库、连续性校验、Lot / Allocation 和收益分层均已通过。随着后续新增结单，如果仍然依赖人工临时判断“看起来没问题就加进去”，正式数据库会逐渐积累不可追溯的异常。

用户明确提出：后续接单进来时应自动校验，有异常就人为介入，没有异常才落盘。

## 决策

从本节点开始，正式投资数据库采用“候选导入 + 校验闸门 + 正式落盘”的流程。

1. 新结单不得直接写入正式 `investment.sqlite`。
2. 新结单先进入 staging / candidate 数据库，生成 parser 报告、审阅包、连续性校验和 allocation 校验。
3. 只有所有 gate 通过、且没有 blocker / failed / needs_review / 未批准 warning 时，才允许 promote 到正式库。
4. info/skipped 只有在已记录为白名单语义时才不阻塞落盘，例如 0 金额事实、已确认待结算、金额型基金证据行。
5. 异常必须进入人工复核；复核结论需要落为 decision / research / mapping override / manual correction / parser fix 中的一种，然后重新跑 gate。
6. 原始事实层不做静默覆盖；人工修正写 overlay，parser 问题通过修 parser 后重跑解决。

## Gate 范围

| Gate | 目标 |
| --- | --- |
| parser gate | PDF 可读、period/account 可识别、parser status passed、待确认项 0。 |
| raw DB gate | SQLite integrity ok、schema 完整、open review 0、source ref 可追溯。 |
| continuity gate | 现金跨月、月内现金、持仓数量、pending amount 连续性通过。 |
| allocation gate | 正股 / IPO / 期权 / 基金 / 股票短仓 lot allocation 通过，无 failed validation。 |
| promotion gate | 仅当上述 gate 通过，才生成或替换正式库。 |

## 操作指引

具体操作规范见：

`context/data-ingestion-validation-gate-v1.md`

## 后续工程影响

当前已有分散 CLI 可以完成各项校验，但还没有统一 orchestration 命令。后续工程应新增一个导入闸门 CLI，把候选导入、校验、异常报告、人工复核和正式 promote 串起来。

推荐目标命令：

```bash
python3 tools/investment_import_gate_cli.py run \
  --pdf-dir <pdf-dir> \
  --candidate-run-id <run-id> \
  --official-db exports/investment-db-v1/investment.sqlite
```

该命令必须默认 fail closed：有异常就停止，不自动正式落盘。
