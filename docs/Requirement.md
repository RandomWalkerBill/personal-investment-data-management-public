# 个人投资数据全面管理系统：初期梳理

> Project: [`projects/personal-investment-data-management/PROJECT.md`](../../projects/personal-investment-data-management/PROJECT.md)
> 当前 phase / state / active_step 见 [`.pmagent/current-state.json`](.pmagent/current-state.json)

## TL;DR

> 建立一个面向个人投资者的全品类投资数据管理系统，把不同平台、不同账户、不同资产类别的交易、现金、持仓、费用、税费、分红、利息、汇兑和公司行动放到统一数据口径下管理，并为复盘追踪、收益核算、税务申报和账户对账提供可追溯的底层数据。当前不是从零开始：已有一份投资数据记录与收益核算方案草案，以及一个飞书多维表 Demo，可作为初期需求和数据模型设计的起点。

## 范围

**范围内**

- 梳理个人投资数据管理系统的第一阶段需求边界，先把“要管理哪些事实、用什么口径、第一版交付什么”说清楚。
- 第一阶段样本范围聚焦富途港股相关的全量交易数据，用这一条主路径跑通数据库主源、Base 单向镜像、收益核算、追溯和对账方案。
- 富途侧全量细节暂不预设为已知；第一阶段采用“公开信息预判 + 用户结单样本对比 + 交易类型扫描落盘”的方式，逐步确认输入范围、字段、费用项、异常结构和解析规则。
- 以既有草案中的分层模型为基础，覆盖原始数据、主数据、事实流水、成本批次、统计口径这几类核心数据资产。
- 明确第一阶段最小可用能力：账户、标的、交易、现金流水、资产变动、成本批次、成本分摊，以及 FIFO 已实现收益核算。
- 把 IPO/打新、handling fee、融资利息、退款、中签配发、上市卖出等复杂但高价值场景纳入重点边界讨论。
- 明确费用、税费、利息、汇兑等项目如何避免重复统计：同一笔经济影响只能通过一种口径进入收益。
- 明确收益口径 v1：默认采用 `default_economic` 经济口径，把可安全归属的交易费用尽可能资本化或扣减卖出收入，无法安全归属的项目作为期间费用或独立收入展示，并保留后续 profile 切换能力。
- 消化飞书多维表 Demo 的表结构、字段、公式和 dashboard 尝试，识别哪些可以沿用，哪些需要重构。
- 第一版交付方向以数据库 schema / 数据模型为主，同时兼容飞书 Base 模板模式，让 Base 作为可读、协作和展示友好的同步镜像。
- Base 同步在第一阶段采用单向镜像：数据库 -> Base。Base 不作为写入口，不回写数据库。
- 新结单正式入库前必须通过数据校验闸门：先生成 staging / candidate 数据库，自动执行 parser、raw DB、连续性和 Lot / Allocation 校验；有异常进入人工复核，没有异常才允许 promote 到正式 `investment.sqlite`。
- 为后续输出数据库 schema、飞书多维表模板、Excel/CSV 导入模板、券商结单解析规则、收益报表和税务视图预留接口。

**范围外（non-goals）**

- 本 workspace 不直接实现生产级系统、自动化解析器、报表前端或真实税务申报工具。
- 不承诺一次性覆盖所有券商、所有国家/地区税法、所有资产类别的最终细则。
- 第一阶段不覆盖其他券商的端到端导入与核算，也不把美股、基金、债券、期权、加密资产等资产类别作为主流程验收对象；这些先作为 schema 扩展预留。
- 不把当前飞书多维表 Demo 视为最终架构；它是实践参考和样例输入，不是 标准 schema。
- 不直接改动或同步用户现有飞书 Base 数据；如后续需要导入或同步，应单独确认权限、目标 Base 和数据脱敏边界。
- 当前讨论过程不急于同步到飞书 Wiki；本地 PM Data 仍是本轮讨论真相源。
- 不把 Trade 表作为唯一中心表来承载所有业务逻辑；复杂事件需要保留 Event / CashActivity / AssetMovement / Lot / Allocation / TreatmentRule 等分层。

## 约束与已定决策

- 已有方案的核心原则暂定为初始架构原则：事实层全量记录，统计层按口径归因。
- 已有方案明确反对单纯 Trade 表中心的设计，推荐以 Event + CashActivity + AssetMovement + Lot + Allocation + TreatmentRule 承接复杂场景。
- 当前 Demo 已证明飞书多维表可以承接一版 Account / Security / Trade / OpenLot / Allocation / Cash Activity 的关系和公式，但仍缺少原始结单、公司行动、多币种估值、税务口径等关键层。
- 已确认第一版架构方向：数据库作为 标准真相源，飞书 Base 作为兼容模板和可读同步镜像。同步应服务可读性与协作体验，但数据库侧负责一致性、约束、计算和长期演进；Base 第一阶段只读展示，不回写数据库。
- 已确认第一阶段资产与券商范围：先聚焦富途港股的全量交易数据，以真实可导出的富途港股交易、现金、费用、税费、IPO/打新、分红/利息等相关事实跑通端到端流程。
- 已确认富途输入发现方法：先通过公开资料和常见港股交易场景形成候选清单，再用用户手头的富途结单 PDF 做逐项对比；公开信息只作为预判来源，结单和实际导出数据才是 schema 与解析规则的验收来源。
- 已确认 IPO 默认口径：申购阶段的显式 handling fee 和申购款/退款/配发本金之间的隐含差额，默认统一归入 `ipo_platform_fee`，进入总经济收益，同时在默认口径中分门别类展示；最终确认阶段仍支持按不同策略改为资本化、费用化、待复核或税务专用处理。
- 已形成全量字段与结单入库框架 v1：第一阶段按原始来源层、主数据层、事实流水层、费用与处理层、派生计算层和校验快照层分层入库；当前真实样本已覆盖 14 类抽取结构，后续字段确认应从这份 v1 框架逐模块收敛到 SQL schema 和 Base 镜像。
- 已确认第一阶段优先级：先把结单里的所有原始数值、事件、现金变化、资产变化和交易行为按统一框架准确还原为完整时间轴；费用/税费 item、treatment、税务和个人决策口径属于基于原始事实的后处理/标记/运算策略，不反向影响原始数据抽取。
- 已确认 Lot / Allocation v1 范围：先处理正股交易和 IPO 中签配发；IPO 中签生成 `ipo_allotment` lot，默认把 IPO 显式申购费和中签隐含费用作为 lot 成本组件；年初已有持仓生成临时 `opening_position` lot；卖出默认 FIFO allocation；收益、税务、多策略、基金、空投和期权 allocation 后置。
- 已确认融资利息暂不分摊到 IPO lot：富途融资利息先作为期间费用保留，未来如需 IPO 维度成本再按融资金额、利率和占用天数估算分摊。
- 已确认正式入库 gate：新结单不得直接写入正式数据库；必须先进入候选导入环境，通过 parser gate、raw DB gate、continuity gate 和 allocation gate；blocker / failed / needs_review / 未批准 warning 均阻止正式落盘，info/skipped 仅限已批准白名单语义。
- 已确认收益口径 v1：`default_economic` 作为默认展示口径；交易相关费用优先进入成本基础或扣减 proceeds；融资利息默认期间费用；股息按 gross / withholding / fee / net 展开，默认 net 进入总经济收益；opening lot 继续标记为 `provisional`，必须和 final 分开展示。
- 已预留收益口径 profile：`trading_decision` 用于个人交易复盘，避免把融资利息、券商奖励等强行摊到单个交易决策；`tax_prepare` 用于税务资料准备，只输出结构化证据和候选分类，不直接内置具体辖区税务结论。
- 已确认多平台收益展示的第一版边界：展示层可以按 `canonical_instrument_id` 汇总同一标的；Lot / Allocation 和成本基础默认仍保留账户 + 平台维度，只有存在明确转仓链路时才延续跨平台成本基础。
- 涉及账户号、用户姓名、券商账号等个人敏感信息时，需求文档只记录结构和口径，不复制具体值。
- 税务相关输出只作为数据口径和计算支持，不能替代专业税务建议；具体税务辖区、申报主体和合规边界仍需确认。

## 开放问题

- 第一版数据库产物要做到什么深度：概念模型 + ERD、可迁移 SQL schema、还是带导入/同步脚本的可运行原型？
- 全量字段与入库框架 v1 已经形成，`corporate_action` 已展开字段草案，2025-03 `market_trade` 样本已结构化重建；本轮进一步完成未确认模块的批量推进与 P0 验收基线，并已落地 parser v1。当前基线为：现金事实 39 条、基金交易证据行 33 条、公司行动现金腿 8 条。下一步优先从 parser v1 输出推进标准入库表和对账，而不是继续依赖第一轮旧 CSV。`fee_tax_items` 和 treatment 层后置为基于原始事实的标记/计算层。
- 富途公开 API / 帮助文档与结单 PDF 的字段、命名和颗粒度是否一致？哪些事实只出现在 PDF 表格、备注、汇总区或多行组合中，需要解析后推断？
- 税务视图的目标辖区和主体是什么？当前 v1 只确认 `tax_prepare` 是税务资料准备口径，不直接做申报规则计算。
- 多币种口径如何定义：账户基础币种、交易币种、报表币种、汇率来源、汇兑损益与证券收益如何拆分？
- 现金流水与交易流水的关系应如何约束：是否要求每笔 TradeSettlement 都能对账到 CashActivity，还是允许阶段性手工关联？
- IPO handling fee 和中签后的退款减少需要链路级运算逻辑解释；当前默认把可解释的显式费用和隐含差额资本化到 IPO lot 并单独列示，仍需在最终确认阶段支持不同 treatment strategy。
- Lot / Allocation v1 的字段、状态和校验项还需要逐字段确认：`position_lots`、`lot_cost_components`、`lot_allocations`、`allocation_runs`、`allocation_validation_items`。
- 收益计算工程还需新增 `return_treatment_rules`、`return_calculation_runs`、`return_items` 和 `return_summary_*`，并基于当前富途样本生成前端可读的收益总览、分标的收益和费用税费明细。
- 后续工程需要把当前分散的导入、连续性校验、allocation 校验和 promote 命令封装为统一的 validated ingestion gate CLI，默认 fail closed：有异常即停止并输出复核项。

---

## 详情

### 背景

用户已经有一份《全品类投资数据记录与收益核算方案草案》和一个飞书多维表 Demo。草案提出的方向不是“记录几笔交易”这么窄，而是把个人投资活动拆成可追溯的经济事实，再根据不同统计口径生成收益、税务、对账和复盘视图。

当前方案尤其重视两个问题：

- 复杂投资事件不能被简化成普通买卖。IPO、退款、中签、handling fee、融资利息、公司行动、债券利息、换汇、转仓等，都需要独立事实表达。
- 收益口径必须防重复统计。同一笔费用、税费或利息，要么资本化进入成本批次，要么作为期间费用进入损益，不能两边都算。但该判断属于后续 treatment / 计算层；第一阶段应先保证原始结单事实完整、准确、可追溯。

### 初始数据模型方向

草案建议的分层包括：

- 原始数据层：保存券商结单原始记录，支撑追溯和重跑解析。
- 主数据层：统一账户、券商、标的、币种、市场和汇率。
- 事实流水层：记录交易、现金、资产进出、公司行动等真实经济事件。
- 成本批次层：管理 Lot 和卖出分摊，用于持仓成本和已实现收益。
- 统计口径层：通过 TreatmentRule / PnLView / TaxView 控制费用、税费、收益如何进入不同报表。

飞书 Demo 已经实践了部分核心链路：账户、标的、交易、现金流水、开仓批次、卖出分摊和已实现收益公式。它适合作为第一版 schema 验证素材，但还需要补齐原始数据、资产变动、公司行动、多币种、税务口径和自动化解析边界。

第一阶段验证样本收束为富途港股的所有可得交易数据。这里的“所有”不等于覆盖所有资产类别或所有券商，而是指在富途港股主路径内尽量完整承接真实发生的投资事实：成交、资金流水、IPO/打新、手续费/平台费/融资利息、税费、分红、利息、转入转出、可能的公司行动和原始来源引用。其他券商和非港股资产在第一阶段只保留扩展位，不作为跑通闭环的验收对象。

IPO/打新模块优先继承既有飞书 Base Demo 中较完整的设计：申购扣款、退款、handling fee、融资利息、`RelatedIPO` 和隐含费用差额公式都应作为强先验。数据库侧需要把这些实践升级为可审计的父链路、现金腿、资产腿和对账计算；尤其要处理“中签导致退款减少”的金额融合问题，保留原始现金行，同时用链路级计算解释申购款、退款、配发金额和费用差额。当前默认把显式 handling fee 与可由申购款差额解释出的中签相关费用统一纳入 `ipo_platform_fee`：默认报表计入总经济收益，但必须单独列示组成、公式和来源信心，避免被黑箱吞掉。

第一版技术形态已初步确认：先做数据库，以数据库承载 标准 schema、约束、计算和同步逻辑；同时保持与飞书 Base 模板兼容，把 Base 用作更容易阅读、协作和展示的数据镜像。同步方向采用数据库到 Base 的单向镜像；Base 第一阶段不承担数据回写。后续 PRD 需要定义数据库表与 Base 表的字段映射、只读镜像策略、敏感数据脱敏和一致性校验。

富途港股输入发现采用三段式推进：

- 公开信息预判：从富途公开 API / 帮助文档、港股常见交易与费用结构出发，先形成候选交易类型、字段、费用项、异常结构和待验证假设。
- 结单对比验证：进入 PDF 结单抽离交易数据讨论时，用用户手头的富途结单样本逐项对照候选清单，确认哪些字段真实存在、哪些需要从多行/备注/汇总区推断、哪些只适合保留为待复核项。
- 扫描与落盘：把确认过的交易类型、字段、来源位置、解析规则、复核规则和 TreatmentRule 影响沉淀到 research / decisions / PRD，而不是只停留在讨论结论里。

当前全量字段和结单入库框架已经收敛为 6 层：原始来源层、主数据层、事实流水层、费用与处理层、派生计算层和校验快照层。第一阶段不再把每张 PDF 表格直接变成最终业务表，而是先保留 `raw_statements`、`raw_source_refs`、`parser_issues` 等追溯证据，再把现金、交易、基金、IPO、公司行动、融资利息、股票收益计划和行权事件归入标准业务事实。结单净值、持仓快照和保证金汇总只用于校验，不作为主计算源。

当前推进目标进一步锚定为“原始事实时间轴优先”：结单里能看到的原始数值和事件先完整抽取、去重、归类、保留来源证据，再在此基础上生成费用/税费 item、treatment profile、收益计算和税务准备视图。即使某些费用或税费暂时不拆成最终 item，也不能影响原始现金行、交易行、资产变动行和结单校验值的准确落库。

### 第一阶段倾向

第一阶段更适合聚焦“核心记账和收益计算”：

- 能录入或导入基础账户、标的、交易和现金流水。
- 能表达 IPO 配发或其他非普通交易产生的持仓变化。
- 能形成 Lot，并用 FIFO 生成 Allocation。
- 能计算单笔卖出的已实现收益。
- 能保留费用/税费/利息等原始事实，并为后续标记其成本化、费用化、税务处理和是否进入损益预留字段。
- 能发现或至少提示明显的重复统计风险。

### 关键风险

- 如果第一版直接追求“全品类 + 全自动 + 全税务规则”，范围会迅速失控。
- 如果“富途港股全量交易数据”的导出格式、字段覆盖和历史完整性不先盘清，数据库 schema 容易基于不完整样本定型。
- 如果只依赖公开资料，容易遗漏结单 PDF 里的表格结构、跨页行、合并单元格、汇总区、备注文本和隐含费用推断；公开资料必须经过真实结单样本校验后才能进入稳定 schema。
- 如果继续把所有逻辑压进飞书公式，长期可维护性、审计性和复杂场景表达会受限。
- 如果没有原始结单和 SourceRef 追溯，后续自动解析、对账和税务解释会缺证据链。
- 如果不先定义报表币种、税务主体和成本法，收益结果可能看似准确但口径不稳定。
- 如果不定义数据库与 Base 的字段映射、只读镜像粒度和一致性校验，两份数据会带来额外对账负担。
- 如果没有正式入库前的自动校验闸门，新结单格式漂移、parser 漏抽或未解释现金/持仓差异可能被混入主库，后续收益、税务和镜像都会建立在脏数据上。

---

## 关联材料

- Strategy: [`../../projects/personal-investment-data-management/strategy/`](../../projects/personal-investment-data-management/strategy/)
- 调研： [`./research/`](./research/)
- 决策： [`./decisions/`](./decisions/)
- 决策： [`./decisions/2026-06-22-database-primary-base-mirror.md`](./decisions/2026-06-22-database-primary-base-mirror.md)
- 决策： [`./decisions/2026-06-23-futu-hk-first-end-to-end-scope.md`](./decisions/2026-06-23-futu-hk-first-end-to-end-scope.md)
- 上下文日志: [`./context/clarifying-log.md`](./context/clarifying-log.md)
- 来源摘要： [`./research/2026-06-22-source-background-digest.md`](./research/2026-06-22-source-background-digest.md)
- 既有方案与 Base 字段清单: [`./research/2026-06-23-existing-plan-and-base-field-inventory.md`](./research/2026-06-23-existing-plan-and-base-field-inventory.md)
- 调研计划： [`./research/2026-06-23-futu-hk-input-discovery-plan.md`](./research/2026-06-23-futu-hk-input-discovery-plan.md)
- 字段映射矩阵： [`./research/2026-06-23-futu-hk-field-mapping-matrix.md`](./research/2026-06-23-futu-hk-field-mapping-matrix.md)
- 全量字段与结单入库框架 v1： [`./research/2026-06-29-full-field-and-ingestion-framework-v1.md`](./research/2026-06-29-full-field-and-ingestion-framework-v1.md)
- 公司行动字段草案： [`./research/2026-06-29-corporate-action-field-draft.md`](./research/2026-06-29-corporate-action-field-draft.md)
- 市场交易时间轴重建： [`./research/2026-06-29-market-trade-timeline-reconstruction.md`](./research/2026-06-29-market-trade-timeline-reconstruction.md)
- 全量原始事实批量推进与验收方案： [`./research/2026-06-29-full-coverage-batch-resolution-and-acceptance-v1.md`](./research/2026-06-29-full-coverage-batch-resolution-and-acceptance-v1.md)
- parser v1 运行记录： [`./research/2026-06-29-parser-v1-run.md`](./research/2026-06-29-parser-v1-run.md)
- 决策：原始事实时间轴优先： [`./decisions/2026-06-29-raw-timeline-first.md`](./decisions/2026-06-29-raw-timeline-first.md)
- 决策：正式入库前必须通过数据校验闸门： [`./decisions/2026-06-29-validated-ingestion-gate-before-official-persist.md`](./decisions/2026-06-29-validated-ingestion-gate-before-official-persist.md)
- 数据导入与校验闸门操作指引： [`./context/data-ingestion-validation-gate-v1.md`](./context/data-ingestion-validation-gate-v1.md)
- PDF 抽取能力验证: [`./research/2026-06-23-pdf-extraction-capability-spike.md`](./research/2026-06-23-pdf-extraction-capability-spike.md)
- PaddleOCR 适配评估： [`./research/2026-06-23-paddleocr-fit-assessment.md`](./research/2026-06-23-paddleocr-fit-assessment.md)
- 富途 Q1 PDF 抽取运行： [`./research/2026-06-23-futu-q1-pdf-extraction-run.md`](./research/2026-06-23-futu-q1-pdf-extraction-run.md)
- PRD: [`./prd/current.md`](./prd/current.md)（如已存在）
