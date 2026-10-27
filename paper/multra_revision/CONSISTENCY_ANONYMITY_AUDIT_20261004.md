# 提交前一致性与匿名审计（2026-10-04）

## 结论

当前本地稿件的主要数值、表格、数据图和已保存实验结果一致；已修正发现的匿名问题与空间指标定义问题。30 项自动检查全部通过，另完成共同输入/实际 SUMO seed/联合反馈审计、独立源包编译及关键 PDF 页面检查。此次没有运行新的交通仿真或调用实验 LLM。

这不是对全部期刊专属要求的通过声明：Multimodal Transportation 完整作者指南仍返回 HTTP 403，实际上传格式与系统合成审稿 PDF 需要在投稿平台确认。没有提交论文或发送外部消息。

## 核对范围及主要结果

直接读取并复核 166 份唯一运行记录中的 2,268 条带有效 scope KPI 的候选/基线记录。这些记录包含复用和不同端点，不能当成 166 个独立交通案例或 2,268 次新增 SUMO。受控比较和完整搜索各自的主样本仍为 15 个 city–seed 案例。

| 主文证据 | 与日志核对的结果（LLM / Rule / Random） | 来源 |
|---|---|---|
| 摘要、表 3、结论：冻结输入端点 | 8.50% / 9.36% / 9.55%；成功 12/15、14/15、13/15；各 103 次逻辑评估 | full_feedback_v1 原始 result 的 before_selected；strict_prefix 提取及输入哈希 |
| 摘要、表 3、结论：完整搜索 | 11.53% / 12.10% / 14.81%；各 13/15；203/199/210 次逻辑评估 | wide_feedback_v1 原始 result 的 selected |
| 表 4、正文配对推断 | 完整搜索 LLM–Rule −0.57 pp、LLM–Random −3.28 pp；后者 case p=0.078、city p=0.125、Holm–Wilcoxon p=0.169 | 独立重算配对差值、全枚举符号翻转、Wilcoxon、Holm、20,000 次城市 bootstrap |
| 图 3 | 可接受候选为 78/203、72/199、82/210；图中顺序 LLM / Random / Rule 已正确映射 | candidate_results.csv 与 case_results.csv，均从原始运行重建 |
| 图 4 | LLM 选中来源为初始 3、单方案变异 5、初始 bundle 2、联合后代 3、no-op 2 | 原始 selected ID 与候选 stage 匹配 |
| 图 5、正文阶段增益 | 联合前 10.88% / 11.81% / 11.96%；增量 0.65 / 0.29 / 2.84 pp | 逐阶段重筛实测候选池 |
| 表 5、稳定性 | 五城市均值 14.60% / 16.61% / 15.43%；LLM/随机平均组内 SD 3.09/2.55 pp；固定 catalog 中 7/10、10/10 出现多种选择 | 五城市 seed 42、每城 5 次 LLM/随机、1 次规则；共同首请求哈希 |
| 表 6、图 6、需求敏感性 | 1,200/2,400/3,600 trips 数值复算一致；高需求 Manhattan 基线 teleport 84/93/100，LLM 60/93/84 | 需求队列原始结果与 baseline_results/teleport_results |
| 表 7、阈值网格 | 100 个设置的均值排名和成功率复算一致；完整搜索随机在全部设置中均值最高 | 保存候选重筛，非重新搜索 |
| 表 8、图 5、空间副作用 | 每策略 13/15 案例有远端边恶化 >60 车秒；LLM 中位最差边 1,350.3、最大边 5,478.7、最大单元 9,081.1 车秒 | 原始 edgeData XML 重读、全网边并集与 500-m 单元聚合 |
| 表 1、表 2、附录 A | catalog 枚举与实现对应；网络库存和基线延误、真实父方案/过滤/选择/联合后代记录一致 | 冻结输入、网络 XML、调用 JSON 和执行记录 |

9 个生成的 TEX 文件全部与本次审计前逐字节一致，其中包括当前各数值表、结果宏和完整案例。图 3–6 的统计输入重新由已保存记录计算，旧结果图未混入当前稿件。框架图保留了最近确认的白底样式和删除预算条的版本。

## 执行和统计复核

- 完整搜索：612 次逻辑评估 = 225 次新增候选 SUMO（含 22 次共享追加初始化）+387 次缓存使用；108 次成功 LLM 请求，10 次传输错误另列。
- 稳定性新增部分：40 次搜索，568 次逻辑评估 =119 次新增 SUMO+449 次缓存，160 次新增 LLM 请求。包括复用 repeat zero 的模型元数据文件记录 200 次请求，两者统计范围不同。
- 需求新增部分：36 次搜索、12 次新基线，489 次逻辑评估 =305 次新增候选 SUMO+184 次缓存；88 次新增 LLM 请求。包含复用 2,400-trip 结果的元数据文件记录 132 次请求。
- 样本 SD、两侧精确 case/city sign-flip、删除零差值且无连续性校正的近似 Wilcoxon、每协议两项比较的 Holm 校正，与正文一致。城市 bootstrap 为 20,000 次，seed=20260927；五城市/两城市限制仍明确披露。
- 同一初始动作与 parent/catalog、兄弟排除、实际执行 seed/version、联合反馈来源及未改变的组件均由现有分析器重新核验。相同三初始的受控端点与五初始的完整搜索未混为同一因果对照。

## 本次实际修改

1. **自引匿名**：§2 两条自引改用 Anonymous/year；参考文献 [10]、[11] 保留匿名占位，删除其标题、作者和 DOI。审稿版 BIB 只保留 17 条实际引用。完整书目存于 `../multra_revision_private/references_full_before_anonymization.bib`，不进入审稿包。
2. **空间定义纠正**：§4.5 原来的“edge midpoint”改为实际实现所用的边形状顶点坐标均值；500-m 单元也按该代表坐标分配。数值没有改变。补充材料解释历史字段名 `nearest_target_midpoint_m` 的真实含义。
3. **附件清理**：导出程序只打包正文引用的六张 PDF 图、配套 PNG 和主图编辑依赖；旧 Framework、备用 framework_current 和未引用 stability 图继续保留在本地，不进入正文源包/综合审稿包。
4. **图像披露补全**：Figure 1 图注增加文字单独以 LuaLaTeX 排版、内置图像工具未提供模型/版本标识的事实说明，不猜测工具型号。
5. **文档同步**：消除当前上传指引中“作者尚未确认”“自引尚未处理”等过期状态；生成正式名 `manuscript_source.zip`，旧 draft 名仅保留同内容兼容副本。
6. **图表颜色一致性**：图 5 和图 6 原来将 Rule/Random 使用的颜色与图 3 对调；现统一方法配色为 LLM 蓝色、Rule 绿色、Random 橙色，数值不变。图 4 的颜色表示方案类别，不是选择器，按其单独图例解释。
7. **复核脚本**：新增 `experiment/audit_current_submission.py`，检查原始 KPI 算术、统计、正文重点数值和实际 ZIP 内容；JSON 报告在 `experiment/submission_consistency_audit/audit.json`。

## 匿名、文件及政策检查

已检查实际交付 ZIP 内的 TEX/BIB/BBL、补充 JSON/CSV、PDF 文本与元数据、解压后的 Word XML/属性：未命中作者姓名、单位/资助、个人本地路径、自引 DOI/题名或 API 凭证。标题页和 cover letter 保持独立，没有进入审稿材料。匿名 PDF 为 20 页，摘要 212 词；8 张主文表、6 张图；5 条 Highlights 长度为 82/75/76/75/73 字符。PDF 无未解析引用或 overfull box；独立源 ZIP 包括 BibTeX 在内的重编译通过。

自引处理依据：[Elsevier 双匿名通用说明](https://www.elsevier.support/publishing/answer/what-are-the-requirements-for-doubleanonymized-peer-review)。出版方明确要求正文、书目及文件属性的身份处理；本刊若另有特定要求，以本刊为准。

框架图属于解释流程的示意图。当前 [Elsevier 期刊 AI 政策](https://www.elsevier.com/about/policies-and-standards/generative-ai-policies-for-journals)允许特定解释性图像使用 AI 辅助，但要求透明披露和作者核验。稿件已披露工具及用途；工具未公开版本，因此如编辑要求精确标识，仍需如实说明不可获取。该图不是研究实测图，也不作为 graphical abstract 上传。不能把通用政策解释为期刊已预先批准图稿。

## 仍待解决或由平台确认

- 本刊完整 Guide for Authors 返回 403：PDF-only 是否接受、是否必传源文件以及其他专属排版要求仍需以实际投稿界面为准。
- 出版方通用标题页说明提及电话；未提供真实电话。如本刊/平台要求，应补充。其余作者确认已经落实，不需重复询问。
- 上传后必须检查平台合成的审稿 PDF 和附件可见性；本地检查不能替代这一步。
- 没有观测交通量/校准 OD，未补出虚构校准案例；多模式、中央单代理及等预算宽度/深度和联合/独立探索控制仍未评估。正文已限定结论，不据此宣称 LLM 优势或真实城市收益。
- 本次复核没有重新逐篇验证全部参考文献的出版元数据，也没有逐页检验 Word 的最终分页；用户选择的 PDF 为主稿。

## 复现命令

从仓库根目录运行；以下只读已有实验并重建分析/稿件，不调用 API 或运行 SUMO：

```bash
MPLCONFIGDIR=/tmp/simtio_audit_wide python3 experiment/analyze_full_feedback.py --root experiment/wide_feedback_v1
MPLCONFIGDIR=/tmp/simtio_audit_stability python3 experiment/analyze_supplements.py stability
MPLCONFIGDIR=/tmp/simtio_audit_demand python3 experiment/analyze_supplements.py demand
MPLCONFIGDIR=/tmp/simtio_audit_build python3 experiment/build_current_manuscript.py
cd paper/multra_revision
pdflatex -interaction=nonstopmode -halt-on-error main.tex
bibtex main
pdflatex -interaction=nonstopmode -halt-on-error main.tex
pdflatex -interaction=nonstopmode -halt-on-error main.tex
cd ../..
python3 experiment/export_multra_documents.py
python3 experiment/audit_current_submission.py
```

本轮修改前备份：`../multra_revision_history/pre_submission_consistency_audit_20261004/`。上传使用文件清单见 `PDF_SUBMISSION_FILES.md`。
