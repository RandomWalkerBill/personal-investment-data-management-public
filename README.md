# Personal Investment Data Management

个人投资数据管理系统的脱敏工程包。它面向 Codex-assisted 使用场景：用户把自己的券商结单放在本地，Codex 或熟悉命令行的用户按教程运行导入、校验、Lot / Allocation、收益和 IPO 专项报告。

本仓库不包含任何真实结单、SQLite 数据库、收益报告、账户号、交易流水或持仓金额。

## 当前最成熟能力

| 能力 | 状态 |
| --- | --- |
| 富途月结单 PDF 导入 | 已有 parser / ingest CLI，覆盖新旧月结单模板 |
| 富途官方年度账单 XLSX 导入 | 已有年度快速回填 CLI |
| SQLite 标准数据层 | 已有 raw fact、管理层、标的归一、账户归一、Lot / Allocation、税务试算 schema |
| 数据校验 | 已有现金 / 持仓连续性校验、导入 gate 设计 |
| Lot / Allocation | 已支持正股 / ETF、IPO、期权、基金、股票短仓 FIFO |
| IPO 专项报告 | 已有 `tools/ipo_report_cli.py`，导出 Markdown + CSV |
| 税务试算 | P0 级人民币试算层，需自行确认正式税务口径 |

## 快速开始

```bash
git clone https://github.com/RandomWalkerBill/personal-investment-data-management-public.git
cd personal-investment-data-management-public

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

mkdir -p data/futu/monthly work reports
cp .env.example .env
```

把你自己的富途月结单 PDF 放到 `data/futu/monthly/`。然后运行：

```bash
python tools/futu_ingest_cli.py \
  --pdf-dir data/futu/monthly \
  --db-path work/investment.sqlite \
  --replace-db \
  --strict \
  --skip-workbook
```

继续跑标的归一、Lot / Allocation 和 IPO 报告：

```bash
python tools/canonical_instrument_mapping_cli.py \
  --db-path work/investment.sqlite \
  run --reset

python tools/lot_allocation_cli.py run \
  --db-path work/investment.sqlite \
  --all-import-runs \
  --account-id futu_hk_main \
  --run-id lot_allocation_futu_history_v1 \
  --replace

python tools/ipo_report_cli.py \
  --db-path work/investment.sqlite \
  --output-dir reports/ipo
```

生成文件：

- `work/investment.sqlite`：本地私有数据库，不要提交。
- `reports/ipo/ipo-report.md`：IPO 专项审阅报告。
- `reports/ipo/*.csv`：IPO 现金腿、配发、lot、复核项明细。

更完整的富途教程见 [docs/FUTU_QUICKSTART.md](docs/FUTU_QUICKSTART.md)。

IPO 专项说明见 [docs/IPO_REPORTING.md](docs/IPO_REPORTING.md)。

## 历史年度账单快速回填

如果你有富途官方年度账单 XLSX，可以追加到同一个本地数据库：

```bash
export FUTU_PRIMARY_ACCOUNT_NUMBER=<your-futu-securities-account-number>

python tools/futu_annual_bill_ingest_cli.py \
  --xlsx data/futu/annual/2025_年度账单.xlsx \
  --db-path work/investment.sqlite \
  --append-db \
  --strict
```

`FUTU_PRIMARY_ACCOUNT_NUMBER` 只用于把年度账单中的主证券账户映射到 `futu_hk_main`。不要把真实账号写进仓库。

## Codex 使用建议

给 Codex 的推荐提示词：

```text
请读取 README.md、docs/FUTU_QUICKSTART.md 和 docs/IPO_REPORTING.md。
我的富途结单在 data/futu/monthly/，请按教程完成导入、校验、Lot / Allocation 和 IPO 报告。
不要把 PDF、SQLite、CSV、reports、work 或 data 目录提交到 Git。
遇到 failed / needs_review / 缺历史成本时先列出问题，不要静默修改。
```

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `schema/` | SQLite schema。 |
| `tools/` | 导入、校验、归一、Lot / Allocation、IPO 报告、税务试算 CLI。 |
| `docs/` | 需求、决策、富途教程、IPO 报告说明、前端交接说明。 |
| `examples/` | 只放合成样例说明，不放真实数据。 |
| `data/` | 本地私有输入目录，已被 `.gitignore` 排除。 |
| `work/` | 本地私有数据库/中间文件目录，已被 `.gitignore` 排除。 |
| `reports/` | 本地私有报告输出目录，已被 `.gitignore` 排除。 |

## 隐私边界

如果一个文件能反推出个人账户、持仓、交易、收益、结单文件名或本机路径，就不要提交。详见 [DATA_PRIVACY.md](DATA_PRIVACY.md)。

## 当前边界

- 这是工程工具包，不是投资建议、税务建议或券商官方软件。
- 富途链路最成熟；其他平台需要新增 adapter 或让 Codex 协助扩展。
- 新格式结单、缺历史成本、RSU、外部转仓、公司行动 FMV、融资利息税务处理等问题仍需要人工确认。
- 生成的报告用于复核和分析，不应直接作为申报文件。
