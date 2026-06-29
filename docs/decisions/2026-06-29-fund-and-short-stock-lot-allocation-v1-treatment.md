# 基金与股票短仓 Lot / Allocation v1 处理口径

日期：2026-06-29

## 背景

在正股 / IPO / 期权 Lot / Allocation 已跑通后，继续补齐样本中的基金申赎与股票短仓交易。目标仍然是先还原原始事实时间轴和可复跑的计算层，不在本阶段引入税务或个人交易决策口径。

## 决策

1. 基金使用独立的 `fund_position_lots` 与 `fund_lot_allocations`，不混入正股 `position_lots`。
2. 基金申购生成 fund lot；基金赎回按 FIFO 分配到历史 fund lot。
3. 基金期初持仓生成临时 opening fund lot，成本按 `份额 * 期初净值` 估算，并标记为 `provisional`；期初 `pending_amount` 不计入该 lot 成本。
4. 只有金额、缺少份额的基金订单保留为原始事实和校验信息，不生成 lot 或 allocation。
5. 最新月份申购若尚未出现在期末基金持仓快照中，标记为 `pending_settlement`，保留在 fund lot 表中，但不参与期末持仓校验。
6. 股票短仓使用独立的 `short_stock_lots` 与 `short_stock_allocations`。
7. 股票短仓 realized PnL 使用 signed cash：`卖空开仓净现金 + 买回平仓净现金`。借券费、融资利息或其他 carry cost 不从交易差额中臆造，后续若结单出现单独证据再进入费用/利息事实层或 treatment profile。

## 当前样本影响

- 样本中，基金赎回可以按 FIFO 分配。
- 最新月末附近的基金申购若尚未进入期末持仓快照，标记为 `pending_settlement`，等待下一期结单自然续接。
- 样本中的股票短仓可以完整买回平仓；发布版不保留真实标的、数量或收益金额。

## 后续

基金收益是否作为证券收益、现金管理收益或单独基金收益展示，后续通过 treatment profile / 报表口径处理，不改变原始 fund lot 和 allocation。
