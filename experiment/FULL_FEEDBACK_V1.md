# SimTIO 完整正确反馈实验 v1

入口：`experiment/full_feedback_experiment.py`。输出：`experiment/full_feedback_v1/`。

## 已完成的主实验

15 个案例 × 3 种策略共 45 个运行已完成，每种策略各一次采样。完整报告见 [analysis/REPORT.md](full_feedback_v1/analysis/REPORT.md)，逐阶段图见 [full_search.pdf](full_feedback_v1/analysis/full_search.pdf)。

| 策略 | Top-10 改善均值 ± SD | 联合循环前 | 最终成功数 |
|---|---:|---:|---:|
| LLM | 10.86% ± 10.55% | 8.50% | 12/15 |
| 规则 | 10.58% ± 10.24% | 9.36% | 14/15 |
| 随机 | 10.91% ± 10.69% | 9.55% | 14/15 |

LLM 相对规则和随机的平均配对差分别为 +0.282、−0.052 个百分点；案例级双侧精确符号翻转检验 p=0.793、0.970，两项 Holm 校正 Wilcoxon p 均为 1.000。结果没有显示 LLM 的显著优势。循环前后使用的预算不同，其改善不能单独证明反馈机制优于等预算的其他扩展策略。

新增 SUMO 177 次，复用缓存 312 次，总逻辑评估 489 次（三组各 163 次）。90 次模型请求均正常结束并生成合法选择，另有一次独立记录的 DNS 传输故障。144 项测试通过；完整命令再次执行时 45 个运行全部复用，无新增模型请求或仿真。实际执行的 seed/version 和首轮请求一致性核验均通过。

另已完成 100 组接受阈值重筛、远端边及空间单元恶化分析和真实完整案例导出。平均延误约束仍不能排除局部明显恶化，详见报告。此处是重新生成父方案、反馈和模型选择的完整实验，与之前共同 LLM 候选池上的联合续跑结果属于不同队列，不混合统计。

本版本从每个案例相同的三个初始动作开始，仅复用经核验的初始测量及公共基线。不再继承旧的单方案 mutation、旧父方案选择、旧反馈或旧 bundle。代码重新选择两个父方案，用正确 SUMO seed 的 KPI 构造反馈，重新枚举、验证和去重合法 catalog。三种策略首轮共享同一父方案、catalog、反馈和仅含初始化的历史；两个首轮决策也使用相同的初始历史，不读取另一策略的选择或其缓存候选。

后续流程是：最多两个单方案修改 → 最多两个兼容 bundle → 两轮联合反馈，每轮最多两个父方案、每次只修改一个组件。整个组合从基线重建后重新仿真。新 bundle 可进入下一轮父方案池；旧最佳可行方案及基线均保留。

## 固定设置

- 15 个 city–seed 案例，需求与 SUMO seed 为 42、43、44，SUMO 1.27.1。
- 三组：LLM、catalog_first、catalog_random。
- 3 个初始动作；首轮 beam=2；bundle top-k=4、最多 2 个；追加 joint rounds=2、beam=2。
- 候选仿真预算上限 11，公共基线另计；缓存结果也消耗逻辑预算。仿真前拒绝、非法输出和不可行结果不被替换成额外成功样本。
- 接受条件：Top-10 改善至少 1%，全网平均延误增加不超过 1%，邻边时间损失增加不超过 5%，吞吐量不下降、teleport 不增加，并保留需求。
- LLM 默认 gpt-5.6-luna、4096 completion-token 上限、默认采样、不固定模型采样 seed。
- 随机选择每次请求 seed 为 `20260927 + SUMO_seed + 1000*repeat + request_index`，序号从 0 开始。

首轮可以严格比较相同输入下的选择；联合阶段会因方法而形成不同的父方案、历史与 catalog。因此最终结果衡量相同初始化、相同搜索结构和预算下的完整自适应流程，不把后续所有步骤描述为固定父方案消融。

## 命令

```bash
# 离线冻结新输入；不调用模型、不运行新仿真。
/home/chris/.conda/envs/chatsumo/bin/python experiment/full_feedback_experiment.py --prepare-only

# 一套主实验：15 案例 × 三组，每个案例内串行，三个独立案例并行。
/home/chris/.conda/envs/chatsumo/bin/python experiment/full_feedback_experiment.py --workers 3

# 结果、逐轮曲线、输入一致性核验、配对统计、100 组阈值及远端空间指标。
python3 experiment/analyze_full_feedback.py

# 后续稳定性实验：保留 repeat=0，再补 LLM/随机的四次重复；规则仍只跑一次。
/home/chris/.conda/envs/chatsumo/bin/python experiment/full_feedback_experiment.py --workers 3 --repeats 5
python3 experiment/analyze_full_feedback.py

# 改模型、轮数或预算时使用新输出目录，避免混用协议。
/home/chris/.conda/envs/chatsumo/bin/python experiment/full_feedback_experiment.py \
  --joint-feedback-rounds 4 --sumo-budget 15 --output-dir experiment/full_feedback_four_rounds
```

同一命令可断点续跑：模型请求逐项哈希验证后复用；已记录的模型输出失败也复用，不抽样到成功。没有取得 HTTP 响应的连接故障，或认证、限流、服务异常，会中止该运行并保留可重试账本，不伪装成 LLM 优化失败。同案例进程锁防止并行命令重复执行同一案例。

## 审计数据

- `frozen/`：三个共同初始候选、新父方案、新反馈、完整 catalog、枚举与过滤记录，以及输入文件哈希。
- `runs/<case>/<arm>_repXX/`：协议、模型原始请求与响应、终止原因、逐次评估账本、单方案选择、联合父子关系、最终结果。
- `evaluation_cache/`：本版本实际新增的仿真数据。旧缓存只供已选动作复用，不提前暴露给搜索历史。
- `analysis/`：所有案例含基线保留结果、按阶段的最佳可行值、配对差值、阈值重筛、全网/远端路段恶化指标。
- `source_hashes.json`：代码快照哈希。

每个 city–seed 的基线和初始配置均核验实际 seed 与 SUMO 版本。网络、需求、基线 cfg 和附加输入文件保存哈希。执行 seed 从 tripinfo 内嵌配置核对，不只依赖运行标签；已经仿真但需求保留失败的初始候选也保留并核验。

默认只完成每种策略的一次主实验，五次采样是后续独立步骤。多次重复分析先在同一 city–seed 内取均值，再作配对，不将重复采样当成新增城市案例。全部需求仍是每城 2,400 条未校准合成 trips；阈值敏感性是固定候选池重筛。旧实验及论文 PDF 保持独立，待稳定性结论确定后再整合正文。
