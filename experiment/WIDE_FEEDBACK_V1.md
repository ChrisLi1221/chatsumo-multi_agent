# 五个共同初始方案、每父方案两个不同子代

入口：`experiment/wide_feedback_experiment.py`。输出：`experiment/wide_feedback_v1/`。原实验保留。

流程：**5 个初始方案 → 2 个父方案 × 2 个不同子代 → 最多 2 个 bundle → 2 轮联合修改（每轮 beam=2）**。候选仿真上限为 **15**；公共基线另计，缓存同样消耗逻辑预算。保留过去最佳可行方案与 no-op。

## 初始方案

保留 `full_feedback_v1` 经实际 seed 核验的前三个初始候选及测量，包括其中不可行或被拒绝的方案。用相同基线诊断、合法候选池和需求 seed，重放 grounded-random 的五次抽样；先断言前三个语义动作完全一致，再使用第 4、5 个动作。全部五个动作在新增仿真前保存，不能根据新结果换起点。

初始生成器按专家角色轮转，在角色内部先抽动作类型再抽参数配置，并做合法性校验与去重。新增候选不由 LLM 生成，也不使用 mutation 或 bundle 结果选取。五个候选及测量对三种方法和所有重复完全相同。公共新增初始化只实际执行一次，每个方法均在逻辑预算中支付五个初始候选的实际评估次数。

## 双子代

从五个已测初始方案选两个父方案，规则与原版一致。每个父方案独立选择两个 mutation，第二个子代仍直接修改原父方案，不能从第一个子代继续修改。

- 两次请求共享同一父方案、初始历史和父方案反馈，不读取第一个子代的 KPI。
- 第二次 catalog 排除第一个合法子代的完整语义动作；保留原 mutation ID 和排序，按配置而非 ID 去重。
- 即使第一个合法子代仿真失败，也不再次选择它。跨父方案重复的动作不重新仿真，不补抽额外“成功样本”。
- catalog 不足、输出失败或验证失败均记录；不能承诺在这些情况下凑满四个有效子代。
- 三种方法共享父方案和基础 catalog；每个父方案的首次请求完全相同。第二次剩余 catalog 可能因首次选择而不同，不将其写成逐次完全冻结 catalog 的消融。

子代仍是原有合法单参数 mutation，未扩大参数范围。初始 bundle top-k=4、最多两个，随后两轮联合修改、组件轮换、约束阈值与原版一致。使用 **aggregate** 联合反馈，避免把搜索宽度与组件相互影响提示同时改变。

## 命令

在仓库根目录执行：

```bash
# 冻结五个共同初始候选；会评估新增两个候选，但不调用 LLM。
/home/chris/.conda/envs/chatsumo/bin/python experiment/wide_feedback_experiment.py --prepare-only --workers 3

# 完整 15 案例 × LLM/规则/随机三组；支持断点续跑。
/home/chris/.conda/envs/chatsumo/bin/python experiment/wide_feedback_experiment.py --workers 3

# 表、图、双子代及种子审计、配对统计、100 组阈值重筛、空间副作用。
python3 experiment/analyze_full_feedback.py --root experiment/wide_feedback_v1

# 后续重复采样（本轮默认不执行；规则保持一次）。
/home/chris/.conda/envs/chatsumo/bin/python experiment/wide_feedback_experiment.py --workers 3 --repeats 5
```

需求/SUMO/初始化 seed=42、43、44；随机 mutation 选择 seed=`20260927 + SUMO_seed + 1000*repeat + request_index`。LLM 为 `gpt-5.6-luna`，4096 completion tokens、默认采样，不固定模型采样 seed。SUMO 1.27.1。所有实际执行结果核验 tripinfo 中的 seed/version。

额外语义缓存仅用于已选择动作的测量复用，不能把其中的方案或 KPI 提前加入搜索历史。日志保留初始化抽样、输入文件哈希、两个父方案、完整 catalog、兄弟排除项、模型请求/响应、逐次仿真账本和联合父子关系。

## 比较边界

新三组使用相同结构和预算，可比较选择策略。但本版本同时从 3 增至 5 个初始方案、从 2 增至 4 个子代，并把预算从 11 增至 15；与旧版的改善差异不能单独证明扩大宽度有效。

如果下一步比较宽度与深度，可以另跑同为 15 预算的深度对照：

```bash
# 3 初始 + 2 子代 + 2 bundle + 4 轮×2 联合修改；本轮没有默认运行。
/home/chris/.conda/envs/chatsumo/bin/python experiment/full_feedback_experiment.py \
  --joint-feedback-rounds 4 --sumo-budget 15 --output-dir experiment/deep_feedback_budget15 --workers 3
```

全部仍为固定 2,400 条未校准合成 trips；本轮不更新论文，不把一次模型采样当作稳定性结论。

## 已完成结果

15 个 city–seed 案例 × 3 种策略共 45 个运行已完成，每组每案例一次采样。

| 方法 | Top-10 改善均值 ± SD | 全网平均延误下降 | 成功数 | 实际逻辑评估 |
|---|---:|---:|---:|---:|
| LLM | 11.53% ± 10.57% | 3.17% | 13/15 | 203 |
| 规则 | 12.10% ± 11.07% | 3.22% | 13/15 | 199 |
| 随机 | 14.81% ± 10.55% | 3.37% | 13/15 | 210 |

LLM 相对规则和随机的平均配对差为 −0.569、−3.278 个百分点；双侧案例符号翻转检验 p=0.727、0.078，Holm 校正 Wilcoxon p=0.530、0.169。没有显示 LLM 的优势。旧 11 预算版本中 LLM 的均值为 10.86%，本次为 11.53%；这只是不同搜索结构、预算和采样下的描述性比较。

新增实际 SUMO 225 次（含公共初始化 22 次），缓存逻辑评估 387 次，总逻辑评估 612 次。108 次模型选择请求均成功，没有模型输出失败；沙箱内 10 次 DNS 传输故障单独留档，联网后续跑。新增初始方案在 10/15 个案例中进入父方案集合。新增初始化中的 4 个方案因需求减少而不可行，均保留，不补抽。

155 项测试通过，45 个完整运行的断点复用通过。结果及审计见 [REPORT.md](wide_feedback_v1/analysis/REPORT.md)、[逐案例结果](wide_feedback_v1/analysis/case_results.csv)、[双子代审计](wide_feedback_v1/analysis/sibling_audit.csv) 和 [搜索过程图](wide_feedback_v1/analysis/full_search.png)。

9/45 个运行因 top-k=4 内无兼容组合而无法继续联合搜索；在全部已测单方案中，这 9 个运行都有兼容组合。因此实际评估次数低于预算上限。`analysis/bundle_pool_audit.csv` 记录这项离线结构诊断；替代组合尚未仿真，不能声称它们会改善交通效果。
