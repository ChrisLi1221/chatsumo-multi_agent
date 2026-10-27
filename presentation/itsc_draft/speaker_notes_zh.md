# ITSC 现场报告讲稿提示（初稿）

英文 slides；14 页主报告 + 4 页备用页。主报告目标时长 **12 分 40 秒**，不含提问。
所有结果依据用户提供的 ITSC_ChatSUMO_Multi_agent.pdf。讲稿提示已同步写入 PowerPoint 的“备注”。
主报告按“交通问题 → 失败原因 → 方案 → 证据 → 局限 → 核心结论”组织。

## 使用方式

- 排练第一遍：按中文提示用自己的英文表达，重点练习每页的最后一句衔接。
- 需要控制到 12 分钟时：第 3、5、6、12 页各缩短约 10 秒。
- 若时段包含 Q&A：主讲压缩到 10–11 分钟；可以跳过第 3 页和第 6 页，保留结果页。
- 默认结束在第 14 页；第 15–18 页仅用于回答问题。

## 英文开场参考

> Good morning. Today I will present Decoupled Intelligence, a multi-agent LLM framework for controllable traffic scenario generation in SUMO. Our question is simple: how can we turn a natural-language traffic request into a reliable, executable simulation workflow? The challenge is keeping the network, demand, configuration, and analysis consistent across many dependent steps.

## 01. Decoupled Intelligence

**时间：00:00–00:35（35 秒）**

开场先讲研究问题：我们希望用户用自然语言描述一个交通场景，系统就能在 SUMO 中完成建网、生成需求、运行和指标分析。真正的难点不是生成某一份 XML，而是让整个多步骤流程保持一致。这篇工作的核心是把推理职责拆开，并把文件状态交给明确的编排层管理。

**英文衔接：** Let me start with a traffic question that sounds simple, but requires a surprisingly long workflow.

**来源：** 论文首页：标题、作者、单位；Sec. I。

## 02. One traffic question becomes five dependent tasks

**时间：00:35–01:30（55 秒）**

用波士顿封路的例子引出问题。对使用者来说，只有一句话；对系统来说，需要先提取路网，再完成封路，然后在修改后的网络上生成每小时 800 辆车的需求，接着运行，最后提取平均等待时间。强调顺序：如果先生成路线再删路，后面就可能出现无效路径。右侧只是示意图，不是波士顿真实地图，也不是实验截图。

**英文衔接：** The challenge is keeping these dependent steps consistent from beginning to end.

**来源：** Sec. I；Fig. 3 请求的简化示例。街道图为演示示意。

## 03. A single context must remember every dependency

**时间：01:30–02:20（50 秒）**

这里说的是论文实际比较的单体 ReAct 基线。它把所有工具和整个历史放进同一上下文，既要理解任务，又要记文件位置，还要处理封路、路由和重试。论文对失败日志的分析指出路径引用错误与推理漂移。参数不一致也是设计动机之一。不要把这一页扩大成“所有单智能体系统都会失败”的结论；问题是这种具体组织方式面对长流程时的可靠性。

**英文衔接：** Our response is to separate the reasoning roles and make the execution state explicit.

**来源：** Sec. IV-A 基线定义；Sec. IV-C / IV-E 失败分析；Sec. I 参数一致性动机。

## 04. Separate reasoning roles. Make state explicit.

**时间：02:20–03:40（80 秒）**

先从上到下介绍，不要逐字读框。Planner 负责把全局目标拆成有顺序的步骤。Orchestrator 负责执行协调、校验和状态传递；SQLite 保存状态、文件和依赖。MCP 提供统一的工具接口。底部是五个工人角色：Builder 建网、Modifier 修改路网或信号、Demand 生成需求、Runner 执行、Analyst 计算指标并反馈。因此总共是一个 Planner 加五种 worker 角色；Orchestrator 是编排组件。虽然图里并排画了五个角色，论文流程按照依赖顺序执行，不是在并行生成路网和路线。

**英文衔接：** The key handover is the network artifact: every downstream step must use the validated topology.

**来源：** Sec. III-A 至 III-C；Fig. 2。

**讲者核对提示：** 明确区分：MCP 是工具协议；文件状态持久化与依赖校验由 Orchestrator 实现。

## 05. Route generation uses the validated network

**时间：03:40–04:40（60 秒）**

用一个具体文件交接解释为什么需要状态管理。Builder 得到原始路网，Modifier 执行封路后得到更新路网。Demand 必须拿到更新后的文件，Runner 再把这份路网和对应路线一起放进配置。这样文件引用不是靠模型从聊天记录里回忆，而是由状态仓库明确提供。这里 v1/v2 只是讲解用的逻辑状态标签；论文描述 Modifier 会更新路网 artifact，并没有声称实现了完整文件版本控制。

**英文衔接：** This dependency rule also appears in how the Planner translates a user request.

**来源：** Sec. III-A 状态与依赖；Sec. III-C Modifier、Demand、Runner。

**讲者核对提示：** v1/v2 是演示标签，不代表实际文件名或版本控制功能。

## 06. The Planner turns intent into ordered contracts

**时间：04:40–05:40（60 秒）**

沿着这五行快速讲一次：先建 Boston 路网，随后执行 Boylston Street 封路，再根据 Newbury Street 到 Beacon Street 的请求生成每小时 800 辆车的流，最后运行并提取平均等待时间。Planner 的每一步只激活一个角色，并明确必要的输入参数。这个例子展示的是任务分解方式，不是新增的实际运行演示。街道名称需要经过工具解析与验证，不能随便杜撰 edge ID。

**英文衔接：** Even with explicit contracts, execution can still fail. That is where feedback enters the workflow.

**来源：** Fig. 3；Appendix II 的单角色步骤约束；Appendix III。

**讲者核对提示：** 原 Fig. 3 出现 1.5 km、1.5 miles 和 distance_miles=0.932056788 的混合表述。本页省略该尺度字段，避免把原图单位不一致带入主讲。若补现场 demo，先统一尺度含义与单位。

## 07. Two feedback loops address two different problems

**时间：05:40–06:45（65 秒）**

明确区分两条返回路径。第一条是执行级错误，例如文件缺失或路线不可达，系统把结构化错误送回 Planner 重新规划。第二条是在仿真执行有效之后，Analyst 检查指标是否满足用户目标；未达到目标则反馈给 Planner 调整。循环有次数上限。这里展示的是论文提出的双层设计；后面的定量实验主要测的是修复预算对任务成功率的影响，不能把它说成已经量化证明了交通 KPI 的最优性或收敛性。

**英文衔接：** We evaluate the system first as an execution architecture, and then as a repair mechanism.

**来源：** Sec. III-B；Algorithm 1；Sec. IV-D。

## 08. 30 tasks test increasing dependency complexity

**时间：06:45–07:40（55 秒）**

实验使用 SUMO 1.21.0 和论文所写的 Qwen3-30B-Coder，共 30 个任务。L1 是基本建网和定性需求；L2 需要特定起终点与参数对齐；L3 增加生成需求前的路网结构修改。评价三个量：成功率表示配置有效且运行没有物理错误；平均 token 是全流程输入输出消耗；Time-to-Insight 是从用户输入到拿到第一个有效 KPI 的总墙钟时间。不要把 TTI 说成纯 SUMO 内核运行时间，也不要把成功率说成所有交通目标都已达成。

**英文衔接：** The largest reliability difference appears in the most complex tasks.

**来源：** Sec. IV-A / IV-B；任务总数为 30，正文未明确给出各层级样本量。

**讲者核对提示：** 沿用论文模型名称，未自行改写成另一模型版本。论文未完整报告硬件、推理服务和计时纳入项；绝对时延应限制在所报告设置内解释。

## 09. Complex-task success rises from 70% to 90%

**时间：07:40–08:50（70 秒）**

先指右侧：在最复杂的 L3，成功率从 70% 提升到 90%，是增加 20 个百分点。然后带观众看左边完整结果：L2 两者都是 100%；L1 单体是 100%，多智能体是 90%。因此这不是每种任务都全面胜出的结果，而是优势主要体现在依赖复杂的任务。可以补充论文失败日志提示路径引用错误与推理漂移，但不要把归因说成独立受控消融已经证明。样本较小且表中没有置信区间，不使用“统计显著”措辞。

**英文衔接：** The reliability gain is accompanied by lower token use and a shorter time to the first valid result.

**来源：** Table I；Sec. IV-C。+20 pp = 90% − 70%。

**讲者核对提示：** 这是整体架构对比，不能拆成“单独增加 Planner / MCP / state 各贡献多少”。L1 降低不能直接用 Table II 的 overthinking 现象作未经验证的因果解释。

## 10. Fewer tokens and a shorter path to the first KPI

**时间：08:50–10:00（70 秒）**

先讲左图：三个难度下 token 都下降，L3 从 11,404.7 到 6,028.7，减少 47.1%，比直接说“减半”更精确。右图是 Time-to-Insight，L3 从 41.4 秒到 8.3 秒，约五倍加速，也就是时间降低约 80%。论文的解释是角色只接收与子任务有关的上下文，减少冗余推理与无效重试。强调这些是该实验配置下的报告均值，不是对所有模型和部署的速度保证。

**英文衔接：** Next, we look separately at what happens when the system is allowed to repair its own errors.

**来源：** Table I。L3 token reduction = (11404.7−6028.7)/11404.7 = 47.14%；TTI ratio = 41.4/8.3 = 4.99。

**讲者核对提示：** 图中 token 标签为四舍五入整数；完整小数见备用页 B1。TTI 不等同于纯仿真运行耗时。

## 11. One repair attempt helps; a second adds cost

**时间：10:00–11:05（65 秒）**

先说明这是另一个修复预算实验，不能与上一页的 90% 成功率直接拼成同一条提升链。Table II 中，L2 和 L3 都从不修复的 30% 提高到一次修复的 50%；第二次修复仍然是 50%。右侧展示代价：L3 token 从约 3,982 增至 6,610，再到 9,214。因此在这些任务里，一次修复已恢复大部分可恢复案例，再增加预算主要增加开销。L1 则出现 90、80、90 的波动，论文提到有时会把无车辆等良性警告当成失败。

**英文衔接：** These results also define the limits of what we can claim from this study.

**来源：** Table II；Sec. IV-D。

**讲者核对提示：** 论文未充分解释 Table I 与 Table II 各项实验配置的对应关系。主讲只做各表内部比较；答辩前应核对任务子集、初始状态、prompt、修复策略与统计口径。

## 12. What this evidence establishes—and what remains open

**时间：11:05–11:50（45 秒）**

这一页主动划清结论边界。证据支持复杂流程的执行可靠性和效率改善，也支持有限修复预算带来恢复收益。但总共只有 30 个任务，实验只评估了一个 LLM backbone。所谓角色消融实际上是单体与完整框架比较，并未逐一隔离 Planner、状态仓库和角色专门化的贡献。双环架构包含 KPI 反馈，但没有报告 KPI 优化的收敛或最优性曲线。页底列的是基于这些局限提出的后续验证建议，不是假装论文已完成的实验。

**英文衔接：** The main takeaway is a system design principle for reliable traffic simulation workflows.

**来源：** Sec. IV-A、Table I–II、Sec. V；组件归因和 KPI 定量证据范围是对已报告内容的归纳。

## 13. Reliable automation needs explicit coordination

**时间：11:50–12:30（40 秒）**

用三点收束：专门化角色减少上下文负担，持久化 artifact 保证跨步骤交接，有限反馈帮助恢复执行。最后只重复最关键的 L3 数字：90% 成功率，token 约减少 47%，Time-to-Insight 约五倍加速。再把数字与交通应用连接：目标是让自然语言需求变成可执行、可检查的 SUMO 工作流，降低场景搭建和迭代的门槛。

**英文衔接：** Thank you. I would be happy to take your questions.

**来源：** Sec. III；Table I L3；Sec. V。

## 14. Thank you / Q&A

**时间：12:30–12:40（10 秒）**

感谢听众并邀请提问。备用页依次是：B1 完整架构对比结果、B2 完整修复预算结果、B3 角色和文件契约、B4 常见问题与回答边界。若时间已到，停在此页，不继续讲备用页。

**英文衔接：** Thank you for your attention. I would be happy to take your questions.

**来源：** 论文作者与单位；Acknowledgment。

## 15. B1 — Full architecture comparison

**备用页，不计入主讲时间。**

备用页，按提问定位数据。若有人问简单任务表现，明确回答 L1 成功率是单体 100%、多智能体 90%；不能忽略这一点。若问“每个角色分别贡献多少”，说明该表对比的是完整系统与单体基线，并没有单独去掉某个角色的实验。

**英文衔接：** This table compares the two complete architectures; it does not isolate each component.

**来源：** Table I，逐项转录。

## 16. B2 — Full repair-budget experiment

**备用页，不计入主讲时间。**

若被问两张表的成功率为何不同，诚实说明论文将其作为两个实验报告，当前稿件没有充分展开配置对应关系；不能未经核对就归因于更难任务、不同 seed 或另一个模型。展示这一表时只比较 0、1、2 次修复预算。另一个重点是最大修复次数，不表示每个任务都实际用了全部次数。

**英文衔接：** The repair budget is a maximum number of attempts, and these comparisons are within Table II.

**来源：** Table II；Sec. IV-D。

**讲者核对提示：** 答辩前优先从原始运行记录补齐两组实验差异，不能现场推测。

## 17. B3 — Worker contracts

**备用页，不计入主讲时间。**

角色输出遵循结构化 action / params 等字段。Planner 每一步仅激活一个 agent_prompt；没有请求修改就跳过 Modifier。这里是简化的输入输出契约，不是完整 API 文档。对多智能体数量的提问，回答一个 Planner 加五种 worker 角色，Orchestrator 管理状态和调度。

**英文衔接：** The contracts reduce how much each worker has to infer from the conversation history.

**来源：** Sec. III-C；Appendix II–III。

## 18. B4 — Likely questions

**备用页，不计入主讲时间。**

这一页用于准备答辩，不需主动展开。回答采用“本研究比较了什么、证据支持到哪一步”的结构。可以认可更强单体、有状态单体或确定性工作流是值得增加的对照，但不声称已有这些实验。不要把 MCP 说成会自动保证所有正确性；也不要说两次修复无效适用于所有场景。

**英文衔接：** Our strongest claim is about the reported workflow reliability and efficiency under the evaluated setup.

**来源：** Sec. III–V；Table I–II。

## 英文收尾参考

> The main takeaway is that reliable automation needs explicit coordination. Specialized roles reduce the reasoning burden, persistent artifacts keep dependent steps consistent, and bounded feedback helps recover failed executions. In the reported complex-task setting, the framework achieves 90 percent success, uses about 47 percent fewer tokens, and reaches the first valid KPI about five times faster. Thank you for your attention.
