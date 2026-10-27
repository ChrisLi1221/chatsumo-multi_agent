# 组件相互影响反馈 v1

在原联合 KPI、约束和单组件反馈之外，新增 `joint_context.component_interactions`。核心代码为 `agents/interaction_feedback.py`；`agents/joint_optimizer.py` 将证据交给负责当前组件的专家。原 aggregate 模式保持原请求内容，新模式必须使用独立输出目录。

通用入口 `experiment/run_optimization_only.py` 也支持 `--joint-feedback-mode interaction`，需与 `--objective-scope top10`、支持反馈的 proposal source 和正数 `--joint-feedback-rounds` 一起使用。新反馈只帮助当前指定专家选择，组件轮换规则、合法 catalog、约束和最终接受标准保持原样。

## 提供哪些证据

- 同一基线下：A 单独实施、B 单独实施、按原动作顺序实施 A+B 的 Top-10 时间损失、邻边时间损失、全网平均延误、吞吐量、teleport。
- 条件差值：`Y(A+B)−Y(B)` 表示加入 A 的测量差值，反方向同理。两种单独结果都存在时计算 `Y(A+B)−Y(A)−Y(B)+Y(基线)`，以原始单位报告。
- 组合新出现的约束失败；组合新增恶化路段、最大恶化路段、监测邻域外恶化路段、正相互影响残差最大的路段，各列表最多 5 条。包含路段 ID、道路名和网络坐标；未以百分比放大接近零的基线损失。
- 当前 bundle 来自上轮单组件修改时：固定另一组件的前后实测差值。即使新组件没有单独测量，仍可使用这项已有证据。

对时间损失/延误，正残差表示比线性相加参考更差，负残差表示更好；吞吐量的方向相反，反馈明确标记 `lower_is_better`。这些是给定需求、种子、动作顺序下的描述性对照，不是因果归责，也不保证修改后有效。

组件按动作和完整语义参数匹配，允许 ID 改名，不允许用旧参数的测量替代新参数。仅查当前搜索已见候选，不读取隐藏缓存结果，不额外仿真。缺失精确单独测量时对应值和残差为 null；无完整证据时不会声称恶化是组合新产生的。边输出和已保存 KPI 不一致会中止，避免把数据问题当成模型失败。

## 已完成的离线核验

原完整实验 15 个案例、60 个联合父方案决策均已重建。47 个父方案有两个精确组件的单独测量，13 个只有一个；没有借用旧版本参数补齐。记录在 `experiment/interaction_feedback_v1/audit/`。

例如 Albany seed 42 第一个联合父方案：Top-10 基线为 25,639.24 车秒，两个组件单独为 22,388.22、24,691.50 车秒，组合为 19,572.91 车秒，相互影响残差为 −1,867.57 车秒。与此同时，全网平均延误从 113.069 秒增至 115.564 秒。这个案例展示了“瓶颈组合收益好，但全网约束仍失败”，不能仅凭负残差接受方案。原始证据见 `audit/albany_equal_scale_delay_seed_42/round_1_decision_0.json`。

## 命令

```bash
# 仅从真实历史测量重建 60 个父方案的证据；不调用 API 或 SUMO。
/home/chris/.conda/envs/chatsumo/bin/python experiment/interaction_feedback_experiment.py

# 五个城市 seed 42，各对比 aggregate / interaction 两种新的 LLM 联合搜索。
# 相同已测七候选前缀，两轮联合修改、beam=2、候选总预算上限 11。
/home/chris/.conda/envs/chatsumo/bin/python experiment/interaction_feedback_experiment.py --run --workers 3

# 仅重算试运行结果，核验首个请求及每一步证据的来源。
python3 experiment/interaction_feedback_experiment.py --analyze-only --plots

# 后续从初始三方案重新运行全部三种选择策略，启用新反馈。
# 此命令与上述共同起点试运行是不同实验，默认不会自动执行。
/home/chris/.conda/envs/chatsumo/bin/python experiment/full_feedback_experiment.py \
  --joint-feedback-mode interaction --output-dir experiment/full_feedback_interaction_v1 --workers 3
python3 experiment/analyze_full_feedback.py --root experiment/full_feedback_interaction_v1
```

试运行使用 `gpt-5.6-luna`、4096 completion tokens、默认采样，SUMO 1.27.1、需求/SUMO seed 42。原七候选前缀的 LLM 选择固定；两组联合调用均重新生成。跨案例交替两组运行顺序。首个联合请求的父方案、catalog、历史、workspace 和联合 KPI 相同，仅新增交互证据及其解释；后续路径可能分叉。

输出请求、响应、终止原因、父子方案、仿真账本与最终 KPI。实际执行 seed/version 逐项核验。支持断点续跑和 `--repeats 5`；单次小规模试运行不足以证明效果提升或稳定性。全量新协议及五次重复尚未默认运行。

## 已完成的五城试运行

| 反馈 | 平均 Top-10 改善 | 成功数 | 逻辑仿真 |
|---|---:|---:|---:|
| aggregate | 11.811% | 5/5 | 54 |
| interaction | 11.688% | 5/5 | 54 |

四城最终结果持平，Los Angeles 从 6.182% 变为 5.565%，下降 0.617 个百分点。这次试运行未显示新反馈提高最终优化效果；也不能凭每组一次采样认定新反馈普遍更差。

共 40 次实际模型请求，全部正常结束并生成合法选择；新增 SUMO 15 次，缓存逻辑评估 93 次。最初沙箱内的 5 次 DNS 传输故障单独保留，联网后续跑成功，未记为模型输出失败。首个请求一致性、每一步证据来源及固定组件保持不变的核验通过。151 项测试通过，10 个完整运行的断点复用通过。

结果见 [REPORT.md](interaction_feedback_v1/analysis/REPORT.md)、[配对数据](interaction_feedback_v1/analysis/case_results.csv) 和 [结果图](interaction_feedback_v1/analysis/paired_pilot.png)。本次试验不替换完整 15 案例的主实验，也未更新论文结论。
