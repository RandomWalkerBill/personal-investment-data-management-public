# 决策：Lot / Allocation v1 范围与默认策略

日期：2026-06-29

## 背景

2025 全年富途结单原始事实层已经闭合：导入、审阅工作簿、现金连续性、持仓连续性和月内现金解释均通过。下一阶段进入 Lot / Allocation 建模。

用户确认第一版不先展开收益、税务、多策略、基金、空投和期权复杂分摊，而是先把正股交易和 IPO 中签生成 lot、卖出按 FIFO 消耗 lot 这条主链路跑通。

## 决策

1. IPO 中签配发在 v1 中生成一个 lot。
   - `source_type = ipo_allotment`
   - 来源为 IPO 配发资产腿 / asset movement。
   - 与普通买入 lot 类似参与后续 FIFO。
   - 它没有二级市场普通买入交易费用；默认口径下，IPO 显式申购费和中签隐含费用作为 IPO lot 的成本组件保留并资本化。
   - 该资本化只针对 IPO 申购费和中签隐含费用；融资利息不在 v1 中分摊到 IPO lot。

2. Allocation 默认使用 FIFO。
   - v1 不支持多策略选择。
   - 不做 specific identification、平均成本或税务专用策略。
   - FIFO 顺序以 lot 的 open date / source order 为准。

3. 年初已有持仓用临时 opening lot 承接。
   - 对 2025 年初 / 首张结单期初已有持仓，创建 `source_type = opening_position` 的 opening lot。
   - opening lot 优先排在本期新建 lot 之前参与 FIFO。
   - opening lot 的历史真实成本暂不强求补齐；若需要计算，可先标记为 `provisional` / `needs_historical_cost`。
   - 后续可以通过历史结单或人工补录更新 opening lot 成本，但不阻塞 v1 allocation。

4. 融资利息暂不分摊到 IPO。
   - 富途月结单中的融资利息先按期间费用 / financing_interest event 保留。
   - 若未来需要 IPO 维度成本，可以基于融资金额、利率、占用天数做估算分摊。
   - 该估算属于后续 treatment / allocation 策略，不影响 v1 lot 生成。

5. 收益和税务口径暂不进入本轮。
   - v1 先解决 lot 和 allocation 数据结构与数量匹配。
   - realized gain / tax view 后续基于稳定 allocation 再讨论。

6. 基金、空投和期权 allocation 暂缓。
   - 本轮先做正股交易和 IPO。
   - 基金申赎、基金空投、期权开平仓 / 到期 / 指派后续单独设计。

## 影响

- 原始事实层不变；Lot / Allocation 是派生计算层。
- v1 可以先实现：
  - opening position -> opening lot
  - market buy stock -> trade lot
  - IPO allotment -> IPO lot
  - market sell stock -> FIFO allocation
- 涉及 opening lot 的 realized PnL 若使用临时成本，必须标记为 provisional。
- IPO 申购费和中签隐含费用默认作为 IPO lot 成本组件；融资利息默认作为期间费用进入后续报表，不默认资本化到 IPO lot。

## 后续

下一步进入 Lot / Allocation 字段定义：

- `position_lots`
- `lot_cost_components`
- `lot_allocations`
- allocation run / validation items
