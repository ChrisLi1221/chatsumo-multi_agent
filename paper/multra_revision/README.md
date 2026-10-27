# SimTIO — 当前转投修订稿（2026-10-04）

> **图片恢复更新：** 原稿四张图对应内容已恢复或用现行流程/数据更新；正文现有六张编号图。原始机制图完整保留，旧结果图未混入新数值。对应关系和重建命令见 `FIGURE_RESTORATION.md`；此前图数和页数记录已由本次更新取代。

> **2026-10-04 作者确认更新：** Ruimin Ke 为通讯作者；IBM–RPI FCRC 资助；无利益冲突；贡献不变；全体作者已审阅同意；代码尚未公开；仅采用 Codex 辅助。标题页、正文声明、Cover letter 和独立利益冲突文件已据此更新。下方早期审计中这些“待确认”记录已由本更新取代；期刊专属源文件要求已按作者提供文本更正为 Word；两条自引现已按出版方通用双匿名说明处理。详见 `AUTHOR_ACTIONS.md`。

**正文已按已核验的新实验重写。** `main.tex` 为主源文件，`main.pdf` 为当前匿名稿，`manuscript_anonymous.docx` 为按本刊要求准备的 Word 导出（格式细项见指南检查记录）。摘要 212 词。论文将 SimTIO 定位为受约束的仿真场景筛选框架，不声称 LLM 普遍优越、真实城市改善或已验证的多模式效果。

> **参考文献更新：** 现为 28 条，全部在正文引用；PDF 22 页。新增文献及核验来源见 `REFERENCES_EXPANSION_20261004.md`。本刊要求 Word 源文件，PDF 为预览。

## 文件

- [匿名正文 PDF](main.pdf) · [可编辑 LaTeX](main.tex) · [Word 导出](manuscript_anonymous.docx)
- [匿名正文、源文件与当前证据包](simtio_anonymous_submission.zip)：包括正文和当前证据；图目录只打包正文引用的六张图及主图编辑依赖；不含标题页或作者待确认信息。
- [独立标题页](title_page.tex)：作者信息沿用原稿，通讯作者、资助、利益冲突、贡献与最终批准均已按作者确认填写。
- [逐条审稿意见回复](RESPONSE_TO_REVIEWERS.md) · [投稿文件检查](SUBMISSION_CHECKLIST.md) · [作者待确认事项](AUTHOR_ACTIONS.md)
- [实验命令、种子与需求构造](../../experiment/SUPPLEMENT_EXPERIMENTS.md)

## 四个明确分开的实验队列

| 证据 | 范围 | LLM / 规则 / 随机平均 Top-10 改善 | 解释 |
|---|---|---|---|
| 固定输入的选择器比较 | 15 案例；3 初始＋2 mutation＋最多 2 bundle；预算 7 | 8.50% / 9.36% / 9.55% | 从修正 seed 后实验的已保存联合循环前阶段提取；两次 parent/catalog 请求完全相同，不是新增独立队列 |
| 当前完整自适应搜索 | 15 案例；5 初始＋每父双子代＋bundle＋两轮联合反馈；预算 15 | 11.53% / 12.10% / 14.81% | 每组成功 13/15；后续 sibling/catalog/联合路径可因选择分叉 |
| 固定需求稳定性 | 五城市 seed=42；LLM/随机各 5 次、规则一次 | 14.60% / 16.61% / 15.43% | 每个初始集合已有可行解，最终成功率有天花板；不能代替 15 案例主均值 |
| 需求敏感性 | Albany/Manhattan，seed=42–44，1,200/2,400/3,600 trips | 分强度见正文表 6 | 2,400 复用主实验；高需求 Manhattan 有显著 teleport，属于拥堵压力测试 |

受控和完整搜索均未显示 LLM 优势。不能将不同预算之间的提升归因于拓宽搜索或联合反馈；相应等预算控制仍未运行。实际 SUMO 与逻辑预算、缓存、模型请求和传输错误分别记录。

当前完整搜索的随机策略在 100 组阈值下均有最高的平均 Top-10 改善；成功率排名会变。全文已替换旧的阈值、输出失败和三案例稳定性叙述。附录 A 使用预定顺序首例 Albany seed=42，提供真实 catalog、拒绝原因、选择、全部 15 个候选 KPI、两轮联合链及最终筛选。

## 重建当前论文

以下步骤只读已完成实验，不调用模型或 SUMO。所有命令从仓库根目录开始。

```bash
python3 experiment/build_current_manuscript.py
cd paper/multra_revision
pdflatex -interaction=nonstopmode -halt-on-error main.tex
bibtex main
pdflatex -interaction=nonstopmode -halt-on-error main.tex
pdflatex -interaction=nonstopmode -halt-on-error main.tex
cd ../..
python3 experiment/export_multra_documents.py
```

`build_current_manuscript.py` 重新核验受控阶段的实际 seed、共同初始候选、完整请求/catalog、前缀截止点与最终选择，并从日志生成当前表格、图及匿名证据。正文措辞由 `main.tex` 编辑，不会被此生成器覆盖。不要使用旧 `build_multra_paper.py` 或旧分析目录的数值更新当前论文。

## 本次提交前复核

见 [一致性与匿名审计报告](CONSISTENCY_ANONYMITY_AUDIT_20261004.md)。当前源稿包为 `manuscript_source.zip`；两条自引已匿名，完整书目仅保存在审稿包之外。可运行 `python3 experiment/audit_current_submission.py` 重新检查已导出的文件。

## 版本与剩余事项

改写前的完整论文目录保留在 `../multra_revision_history/pre_current_20261004/`。原 `generated`、`figures` 和 `supplementary_data` 是历史资料，当前正文和匿名包均不引用它们。独立 `new_experiments` 材料是本次正文整合前的补充实验报告。

仍没有观测交通量或校准 OD；尚未验证多模式、中央单代理对照、等预算宽度/深度和联合/独立探索的增量价值。作者声明、数据发布许可与投稿平台要求见 `AUTHOR_ACTIONS.md`。官方指南曾返回 HTTP 403；现已根据作者提供的全文核对，明确要求 Word 源文件。剩余格式与声明问题见 `GUIDE_REQUIREMENTS_20261004.md`。没有提交论文、发送邮件或公开发布资料。
