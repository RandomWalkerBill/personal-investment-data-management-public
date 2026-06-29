# 决策：期权 Lot / Allocation v1 处理口径

日期：2026-06-29

## 背景

正股 / IPO Lot / Allocation v1 已跑通后，用户要求继续处理期权交易。样本覆盖美股期权、港股期权，包含卖出开仓、买入开仓、买入平仓、卖出平仓、到期归零、以及短 call 指派 / 行权交割。

## 决策

1. 期权使用独立的 derivative lot，不混入正股 / ETF `position_lots`。
   - 表结构使用 `option_contract_lots` 和 `option_lot_allocations`。
   - 期权合约 key 由底层、到期日、购/沽、行权价构成，例如 `OPT:<UNDERLYING>:<EXPIRY>:<CALL_OR_PUT>:<STRIKE>`。

2. 期权 PnL 使用 signed cash 计算。
   - 开仓、平仓现金均保留原始带符号净现金。
   - realized PnL = `opening_cash_allocated + closing_cash_allocated`。
   - 对 short option，卖出开仓现金为正，买入平仓现金为负。
   - 对 long option，买入开仓现金为负，卖出平仓现金为正。

3. 短期期权权利金不在开仓时立即确认为 realized。
   - short option 卖出开仓后，权利金先留在 open option lot。
   - 只有在买平、到期归零、指派 / 行权关闭时，才确认 realized PnL。
   - 年末仍未关闭的短期期权保留在 `v_option_open_positions`，不进入 realized PnL。

4. 到期归零按关闭事件处理。
   - short option 到期归零：realized PnL 为剩余开仓权利金。
   - long option 到期归零：realized PnL 为剩余开仓成本的损失。

5. 指派 / 行权需要链接到底层证券交易。
   - 期权关闭本身仍记录 option realized PnL。
   - 若结单同时出现同日、同数量、同行权价的底层股票买卖，写入 `option_underlying_links`。
   - 发布版不保留真实期权合约、标的、数量或价格；实际项目中应通过 `option_underlying_links` 记录指派 / 行权与底层交割的关系。

6. 税务口径后置。
   - 当前默认是经济口径 / 交易复盘口径：期权权利金和底层股票交易分别可见，同时通过 link 关联。
   - 若后续税务要求把期权权利金调整到底层股票成本或卖出收入，可在 treatment profile 层实现，不覆盖原始 allocation。

## 影响

- 期权收益可以独立汇总，也可以与正股 / ETF realized PnL 在汇总层并列展示。
- Covered call / cash secured put 等组合行为不会在原始层硬合并，但可通过 link 或后续 strategy grouping 识别。
- 未平仓期权的权利金现金流和 realized PnL 分离，避免提前确认收益。

## 后续

- 股票短仓仍未纳入本轮，需后续单独建 short stock lot / cover allocation。
- 基金 lot / allocation 仍为下一阶段候选重点。
