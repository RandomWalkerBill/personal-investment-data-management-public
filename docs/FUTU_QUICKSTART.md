# 富途历史结单导入教程

本教程用于把自己的富途历史月结单 PDF / 官方年度账单 XLSX 导入到本地 SQLite，并继续生成标的归一、Lot / Allocation 和 IPO 专项报告。

## 1. 安装环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

月结单 PDF 解析依赖 `pdfplumber`。如果 PDF 是扫描件或图片型 PDF，当前 P0 工具可能无法直接识别，需要另行接 OCR。

## 2. 准备目录

```bash
mkdir -p data/futu/monthly
mkdir -p data/futu/annual
mkdir -p work reports
cp .env.example .env
```

把富途月结单 PDF 放到：

```text
data/futu/monthly/
```

把官方年度账单 XLSX 放到：

```text
data/futu/annual/
```

这些目录都被 `.gitignore` 排除，不要提交。

## 3. 导入月结单 PDF

首次导入：

```bash
python tools/futu_ingest_cli.py \
  --pdf-dir data/futu/monthly \
  --db-path work/investment.sqlite \
  --replace-db \
  --strict \
  --skip-workbook
```

后续追加新月份：

```bash
python tools/futu_ingest_cli.py \
  --pdf-dir data/futu/monthly-new \
  --db-path work/investment.sqlite \
  --append-db \
  --strict \
  --skip-workbook
```

说明：

- `--strict`：parser 验收失败或出现待复核项时返回非 0。
- `--skip-workbook`：不生成 Excel 审阅包，避免额外 Node 依赖。
- `--replace-db`：覆盖目标 DB，只适合首次导入或从头重跑。
- `--append-db`：追加一个新的 import run，用于补月份。

## 4. 导入官方年度账单 XLSX

年度账单适合快速补历史数据。为了让年度账单和月结单主账户对齐，可以设置：

```bash
export FUTU_PRIMARY_ACCOUNT_NUMBER=<your-futu-securities-account-number>
```

导入单个年度账单：

```bash
python tools/futu_annual_bill_ingest_cli.py \
  --xlsx data/futu/annual/2025_年度账单.xlsx \
  --db-path work/investment.sqlite \
  --append-db \
  --strict
```

如果年度账单只作为补充证据，建议先跑月结单主链路，再按年份追加年度账单。

## 5. 查看数据库状态

```bash
python tools/investment_db_cli.py status \
  --db-path work/investment.sqlite
```

如果命令报错，先不要继续做收益计算；把错误和最新 ingest report 给 Codex 复核。

## 6. 标的归一

```bash
python tools/canonical_instrument_mapping_cli.py \
  --db-path work/investment.sqlite \
  run --reset
```

目标是让同一个标的在不同来源中归到同一个 `canonical_instrument_id`，避免前端显示一堆平台原始代码。

## 7. Lot / Allocation

```bash
python tools/lot_allocation_cli.py run \
  --db-path work/investment.sqlite \
  --all-import-runs \
  --account-id futu_hk_main \
  --run-id lot_allocation_futu_history_v1 \
  --replace
```

这一步生成：

- 股票 / ETF FIFO allocation
- IPO 中签 lot
- 期权 lot / allocation
- 基金 lot / allocation
- 股票短仓 allocation
- 已实现收益相关视图和 CSV

如果出现 warning，常见原因包括：

- 早于首份结单的历史成本缺失。
- RSU / 外部转仓需要人工成本。
- 公司行动入账需要 FMV。
- 基金只有金额没有份额。
- 期权 / 权证到期需要特殊识别。

warning 不一定阻塞分析，但不能直接当作税务级精确成本。

## 8. IPO 专项报告

```bash
python tools/ipo_report_cli.py \
  --db-path work/investment.sqlite \
  --output-dir reports/ipo
```

输出：

```text
reports/ipo/ipo-report.md
reports/ipo/ipo_cash_legs.csv
reports/ipo/ipo_asset_events.csv
reports/ipo/ipo_lots.csv
reports/ipo/ipo_review_items.csv
```

详见 [IPO_REPORTING.md](IPO_REPORTING.md)。

## 9. 可选：税务试算

当前税务试算是 P0 工程能力，不是最终申报建议。

```bash
python tools/tax_calculation_cli.py run \
  --db-path work/investment.sqlite \
  --tax-year 2025 \
  --run-id tax_cn_iit_2025_trial_v1 \
  --replace
```

正式使用前需要确认：

- 人民币汇率取数规则。
- 税率和所得分类。
- 亏损是否允许抵扣。
- 境外预扣税是否有凭证和抵免限额。
- 融资利息和其他费用是否可扣除。

## 10. 推荐复核顺序

1. `futu_ingest_cli.py` 是否 strict passed。
2. `investment_db_cli.py status` 是否正常。
3. 标的归一是否 unresolved 为 0 或可解释。
4. `lot_allocation_cli.py` 是否 failed 为 0。
5. IPO 报告里的 `ipo_review_items.csv` 是否只有可接受的 info / warning。
6. 再进入收益展示或税务试算。
