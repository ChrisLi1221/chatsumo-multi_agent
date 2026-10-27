# 投稿补充实验：采样稳定性与需求强度

冻结 `wide_feedback_v1` 算法：5 个共同初始方案、2 个父方案各 2 个不同子代、top-4 内最多 2 个兼容 bundle、2 轮联合修改（beam=2），候选仿真上限 15，基线另计。联合反馈为 aggregate；模型 `gpt-5.6-luna`，completion token 上限 4096、默认采样、不指定模型 seed。保留原有 bundle top-4 兼容性限制，没有中途改算法。

## 1. 稳定性

五个城市固定需求/SUMO seed=42，LLM 和随机各 5 次、规则 1 次。复用 rep00，新增 40 次完整搜索。调用与结果仍保存在 `wide_feedback_v1/runs`，独立索引和新增工作账本保存在 `supplement_stability_v1`，不修改主实验的 `execution_accounting.json`。

每个案例固定所有需求、初始方案、父方案及基础 catalog。父方案第一次 mutation 请求完全相同；第二个子代排除第一次所选动作，后续联合搜索可分叉。因此只使用第一次请求计算严格固定 catalog 下的选择频率、熵和两两一致率；最终效果按城市报告 5 次运行的均值、样本标准差、范围与成功率。

随机选择 seed 为 `20260927 + SUMO_seed + 1000*repeat + request_index`，repeat=0..4。模型为独立调用，保留真实响应模型、finish reason、tokens、传输错误及每次原始响应。

统计比较先在每个城市内平均重复结果，再进行五个城市的配对分析；不把 25 次搜索当作 25 个独立城市。五个固定案例初始集合均已有可行解，因此最终成功率有天花板，不能用 100% 成功率证明 LLM 更稳定。

## 2. 需求强度

预先指定 Albany、Manhattan，seed=42/43/44，1,200、2,400、3,600 trips。每个场景三种策略各一次；2,400 复用原版，新增 12 个基线和 36 次完整搜索。需求/SUMO/初始化使用相同案例 seed。

全部保持 `[0,3600)` 秒发车窗口，SUMO 持续到需求完成。以已有 2,400 个合成 OD trips 为母集，按发车顺序每两个一组，用 `random.Random(seed)` 各选一个：

- 1,200：保留这 1,200 个原 trips，保留出发时间、OD 与车辆属性。
- 2,400：原始需求，不重跑已完成搜索。
- 3,600：原始 2,400 加上所选 1,200 个 OD 的副本；副本使用唯一 ID，出发时间加 0.75 秒，其余属性不变。

这种配对构造减少独立重生成 OD 的干扰，但不能完全固定不同强度下的经验 OD 比例，也不是交通需求校准。保存所选源 trip ID、文件哈希、确切 trip 数和时间窗口；没有用实验效果挑选 OD。

每个新场景重新测量基线、冻结 Top-10、诊断与合法初始空间，预先冻结五个初始动作，再仿真。三种策略共享其测量、父方案、基础 catalog、预算及后续规则；初始化失败保留，不补抽。不同需求水平的 Top-10 可以不同，因此跨强度改善百分比的分母和道路集合也可能不同。

每个需求水平有六个 city–seed 案例，但只有两个城市；统计结果为探索性证据。每个场景只做一次模型采样，不能完全分离需求变化与 LLM 采样变化。

实测发现 Manhattan 的 3,600-trip 基线在 seed=42/43/44 下分别有 84/93/100 次 teleport。保留这些拥堵压力测试，不筛掉不理想的基线。全部 trips 被 SUMO 记录为完成并不意味着无 teleport 的物理行程。约束继续使用“不超过对应基线”，必须结合 `teleport_results.csv` 的绝对次数解释延误变化，不能把可行结果写成零 teleport。

## 命令

在仓库根目录运行；已有 `.env` 提供 API 配置，无需把 key 写入脚本。

```bash
/home/chris/.conda/envs/chatsumo/bin/python experiment/supplement_stability.py --workers 3

# 可选：只准备新增需求、基线与初始候选；会运行 SUMO，不调用 LLM。
/home/chris/.conda/envs/chatsumo/bin/python experiment/supplement_demand.py --prepare-only --workers 3
/home/chris/.conda/envs/chatsumo/bin/python experiment/supplement_demand.py --workers 3

python3 experiment/analyze_supplements.py stability
python3 experiment/analyze_supplements.py demand
python3 -m pytest tests -q

# 从已完成的真实结果生成独立、匿名的可编辑英文补充材料。
python3 experiment/build_supplement_paper.py
cd paper/multra_revision/new_experiments
pdflatex -interaction=nonstopmode -halt-on-error supplement.tex
pdflatex -interaction=nonstopmode -halt-on-error supplement.tex
```

全部支持断点续跑，完整结果复用不再调用 API。每个案例采用文件锁；缓存仅用于已选择动作的测量复用，不作为未见过的搜索反馈。

## 输出与统计

两个 `supplement_*_v1/analysis` 目录包含逐运行结果、汇总、配对统计、固定请求审计、实际 SUMO seed/version 审计、图的 PNG/PDF、可编辑 LaTeX 表格。稳定性另含首次固定 catalog 的全部选择频率。需求实验另含基线拥堵程度和分城市结果。

Top-10 配对差以百分点报告；双侧精确符号翻转检验、Wilcoxon 及每组两项比较的 Holm 校正同时保存。城市聚类 bootstrap 在五个／两个城市下仅为描述性区间，不据此声称普遍优势。所有无操作结果计为 0 改善并保留在分母中。

每组另用保存候选做 100 组阈值重筛和已选结果的全网空间副作用统计；改变阈值后的重筛不等于重新搜索。实际新 SUMO、缓存逻辑评估和基线分开记账；沙箱 DNS 故障记为传输错误，允许联网后重试，不能当作模型输出失败。

现有论文正文仍包含旧实验口径。这两个补充实验及其独立英文补充材料必须与最终主实验口径统一后再投稿，不能直接叠加到旧结论上。
