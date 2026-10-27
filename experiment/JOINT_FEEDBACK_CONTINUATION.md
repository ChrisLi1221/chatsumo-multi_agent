# 让联合仿真结果进入下一轮优化

当前入口：`experiment/continue_joint_feedback.py`。

已完成 15 案例 × 3 方法。共同起点平均 Top-10 改善 8.72%；两轮后 LLM/规则/随机分别为 11.49%/10.63%/9.66%，成功数 12/12/11（各 15 案例）。LLM 相对规则和随机的优势尚未达到统计显著。详见 [完整报告](joint_feedback_continuation/analysis/REPORT.md) 和 [每轮曲线](joint_feedback_continuation/analysis/joint_progress.pdf)。新增 133 次实际 SUMO 调用、47 次缓存评估、60 次实际 API 请求；139 项测试通过，并验证完整结果续跑不产生新请求或仿真。

它读取 `experiment/llm_output_seed_corrected` 中已经完成的候选池，在 bundle 仿真后继续执行两轮联合反馈。LLM、规则、随机都从同一个 LLM4096 候选池出发，每轮最多保留两个 bundle 父方案，每个父方案修改一个组件。未修改的组件保持固定；完整组合从基线重建、顺序验证并重新仿真。成功仿真的新 bundle 会进入下一轮父方案池。原候选保留，最终仍以约束筛选和基线保护决定输出。

默认预算：原阶段最多 7 次候选仿真，加两轮各最多 2 次，总上限 11 次，公共基线另计。语义缓存命中同样计入逻辑预算。预算耗尽、没有可仿真的 bundle 或合法修改耗尽时停止。专家按组合谱系轮换；去重对象是完整有序组合，不会因为组件曾单独测试过就排除它。

```bash
# 全部 15 个 city–seed，三组选择器；会进行新的 LLM 调用和 SUMO 仿真。
/home/chris/.conda/envs/chatsumo/bin/python -u experiment/continue_joint_feedback.py

# 汇总每轮效果、父子关系、配对统计和实际执行 seed。
python3 experiment/analyze_joint_continuation.py

# 单案例，其他默认设置不变；已有完整结果直接复用。
/home/chris/.conda/envs/chatsumo/bin/python -u experiment/continue_joint_feedback.py \
  --cases ann_arbor_equal_scale_delay_seed_42 --arms llm

# 更长的独立实验：不得覆盖两轮实验。
/home/chris/.conda/envs/chatsumo/bin/python -u experiment/continue_joint_feedback.py \
  --joint-feedback-rounds 4 --sumo-budget 15 \
  --output-dir experiment/joint_feedback_four_rounds
```

输出：`experiment/joint_feedback_continuation/`。每次请求保存父方案、联合 KPI、完整 catalog、指定专家、固定组件、响应及终止原因；每轮保存父子关系和实测增益。中断后用同一命令继续，按请求哈希复用已保存响应，按执行记录复用评估。失败响应也保留，不自动抽样到成功。`failed_attempts/` 独立保留因沙箱 DNS 限制未能联网的预运行。

本次 seed 44 的五个案例在 `experiment/joint_feedback_continuation_seed44/` 独立执行，完成后将 15 份结果按字节哈希核对并汇入主结果目录，记录在 `shard_imports.json`。这些结果中的仿真文件路径仍指向该目录；保存或迁移本次实验时需要同时保留两个目录和原始公共候选池。

该实验检验共同起点下的**自适应联合搜索**。首轮的共同父方案不意味着所有后续决策始终拥有相同父方案和 catalog；历史、父方案和合法集合可以随实测结果分叉。比较 LLM、规则和随机时，必须使用相同轮数和预算。与没有额外四次仿真的旧结果相比，增益不能全部归因于 LLM。

共同起点仍来自之前保存的单方案选择，部分旧选择依赖修复前反馈。本实验的所有新增联合反馈均来自正确 seed 的测量，但没有重新执行起点之前的 LLM 单方案选择。仍需另做从修正初始化反馈开始的完整搜索及采样稳定性实验。

若从头运行主程序 `experiment/run_optimization_only.py`，现有联合循环可通过 `--search-rounds 2 --joint-feedback-rounds 2 --feedback-beam-width 2 --sumo-budget 11 --feedback-max-completion-tokens 4096` 开启（另需正常的案例、proposal-source 和 bundle 参数）。主程序旧默认值保持不变，避免静默改变既有实验协议。
