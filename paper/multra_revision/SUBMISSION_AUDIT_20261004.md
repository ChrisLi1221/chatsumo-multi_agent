# Multimodal Transportation 投稿要求复核（历史记录）

> 当前状态以 [CONSISTENCY_ANONYMITY_AUDIT_20261004.md](CONSISTENCY_ANONYMITY_AUDIT_20261004.md)、[PDF_SUBMISSION_FILES.md](PDF_SUBMISSION_FILES.md) 为准。本文件保留早期检查过程；其中自引未匿名、未准备 cover letter、作者待确认和旧页数等记录已经被后续修改解决。

> **图片恢复更新：** 原稿四张图对应内容已恢复或用现行流程/数据更新；正文现有六张编号图。原始机制图完整保留，旧结果图未混入新数值。对应关系和重建命令见 `FIGURE_RESTORATION.md`；此前图数和页数记录已由本次更新取代。

> **2026-10-04 作者确认更新：** Ruimin Ke 为通讯作者；IBM–RPI FCRC 资助；无利益冲突；贡献不变；全体作者已审阅同意；代码尚未公开；仅采用 Codex 辅助。标题页、正文声明、Cover letter 和独立利益冲突文件已据此更新。下方早期审计中这些“待确认”记录已由本更新取代；期刊格式与自引匿名规则仍待核实。详见 `AUTHOR_ACTIONS.md`。

核查日期：2026-10-04。对象：当前 `main.tex/main.pdf`、Word 导出、标题页、Highlights 和匿名归档。此次为投稿准备审计，没有提交稿件，也没有替作者完成任何事实声明。

## 证据范围

用户提供的 [本刊 Guide for Authors](https://www.sciencedirect.com/journal/multimodal-transportation/publish/guide-for-authors) 经网页读取和直接 HTTP 请求均返回 403；Elsevier 旧版指南地址同样不可读。未获得当前完整指南，不能声称逐条通过本刊全部要求。

以下区分：

- **出版方通用规则**：本次直接读取的 Elsevier 政策/支持页面；本刊明确的特殊要求优先。
- **本刊旁证**：[2026 世界交通运输大会期刊介绍](https://www.wtc-conference.com/journals/a4005.html)，2026-03-27 发布；说明双匿名、Word、摘要不超过 250 词及作者声明等，但不能代替当前完整指南。
- **本地实测**：直接检查文件、XML、PDF 字体和 Word 转换结果。
- **待核实**：无法从本刊当前指南或登录后的投稿页面确认的条款。

## 文件与格式对照

| 项目 | 当前状态 | 处理意见 |
|---|---|---|
| 稿件类型 | 原创方法及实验论文 | 沿用用户界面中的 Full Length Article；无特刊邀请或明确归属时，不选 VSI |
| 匿名正文 | 作者栏和 PDF/Word 作者元数据为空；文件名不含作者姓名 | 基础匿名检查通过，但自引处理尚未关闭，见下文 |
| 独立标题页 | `title_page.tex/.pdf/.docx` 已有作者、单位、邮箱 | 通讯作者、完整通讯地址、贡献及声明仍有草稿提示，不能原样提交 |
| 摘要 | 212 个空白分隔词；此前更严格拆词计数 224；无引用 | 低于 250 词；Word 首页缺显式 Abstract 标题，建议补上以利元数据提取 |
| 关键词 | 6 个 | 已有；本刊当前数量上限未直接核实 |
| 可编辑正文 | LaTeX 和 DOCX 均有 | WTC 本刊介绍写明 Word；优先准备 DOCX。不可仅凭通用 LaTeX 支持推断本刊一定接受源文件 ZIP |
| 篇幅 | LaTeX PDF 18 页；LibreOffice 转换的 Word PDF 22 页 | 两种排版页数不同不等于内容缺失；本刊总字数、页数上限未核实，不能认定无上限 |
| 行距与行号 | LaTeX 为单栏、11 pt、1.15 倍行距；未设连续行号 | 本刊是否要求双倍行距/行号尚未核实；可读性改进不应冒充硬性规定 |
| 表格 | 8 张主文表，另有附录表；Word 含 11 个可编辑表格 | 表格不是截图；Word 存在跨页拆分，需要正式排版时处理续表和重复表头 |
| 图 | 主文 2 图，均有独立矢量 PDF 和 PNG | 上传矢量 PDF 原图；不要将预览 PNG 当作唯一出版原图 |
| Highlights | 5 条，分别 82/75/76/75/73 字符（含空格，不含项目符号） | 满足 Elsevier 通用长度标准；本次新增 `Highlights.docx`，与 TXT 内容相同 |
| 参考文献 | 当前正文实际引用 17 条；编号式、交叉引用可解析 | 本刊指定引文样式未核实；本次不宣称完成全部书目真实性核验 |
| 补充材料 | 有匿名 CSV/JSON、完整案例、统计与来源记录 | 按系统 Supplementary Material 分类并提供说明；当前大 ZIP 同时含源稿和证据，不宜直接视为单个补充材料上传 |
| Cover letter | 当前目录未准备 | 建议短于一页，说明问题、贡献、结果边界和期刊适配性；本刊强制状态待核实 |
| 图形摘要 | 未准备 | WTC 介绍为鼓励项目；没有证据认定本刊当前强制要求，暂不因此阻塞提交 |
| 利益冲突独立文件 | 未准备，标题页也未定稿 | 先由全体作者确认；按系统 Declaration of Competing Interest 栏目/工具形成文件 |
| CRediT、资助及致谢 | 有草稿或占位 | 需作者确认；双匿名审稿材料中避免带出身份 |

Elsevier 的 [Highlights 规则](https://www.elsevier.support/publishing/answer/how-do-i-include-highlights-with-my-manuscript) 为 3–5 条、每条最多 85 字符，独立可编辑文件；其强制与否依各刊指南。新增 DOCX 不改变原稿或旧 ZIP。

## 此次发现的实质性未完成项

### 1. 自引的双匿名处理需要重新核对

Elsevier [双匿名说明](https://www.elsevier.support/publishing/answer/what-are-the-requirements-for-doubleanonymized-peer-review) 当前页面更新日期为 2026-08-28，明确提出自引匿名占位处理。当前相关工作采用第三人称，但 `li2024chatsumo` 和 `li2026chatsumoagent` 两条文献仍显示作者姓名、论文标题和 DOI。因此，此前的文件/元数据匿名检查并不等于已满足这一内容匿名规则。

若本刊没有另行规定，匿名审稿版本应按该说明处理两条自引及其可识别书目信息，保留完整书目用于恢复。不能只隐藏 PDF 姓名，却把原始 `references.bib` 放入审稿人可见 ZIP。必要的研究背景仍应保留。此次只记录差异，没有在未读到本刊具体条款时批量删改引文。

### 2. AI 声明仍是草稿

`main.tex` 当前仅写 “An AI coding assistant”，未列工具名称，并写明作者尚需核验；声明位于附录之前，并非紧邻参考文献。

根据 [Elsevier AI 政策](https://www.elsevier.com/about/policies-and-standards/generative-ai-policies-for-journals)，最终稿应列明实际工具及用途、真实的人类复核和责任承担情况，并在参考文献前放置独立声明。实验中的 LLM 选择器和辅助写作/编码工具是两类使用，应分别报告。方法部分还需明确辅助编码、数据分析和可复现绘图的 AI 使用；只描述受测 LLM 不足以覆盖这部分。

当前会话能确认使用了 Codex；用户此前提到 Claude，但需作者确认哪些 Claude 产出进入最终研究或论文。不能凭推测填写版本，也不能替作者宣称已完成复核。现有统计图来自数据和绘图代码，不能误写为生成式图像结果。

### 3. 作者信息和声明未完成

`title_page.tex` 明确保留以下待确认内容：通讯作者、完整通讯地址、CRediT、资助和利益冲突。通用双匿名标题页说明还列出电话与邮箱。独立标题页是交给编辑的，双匿名不意味着投稿系统也不填写作者。

利益冲突需要明确有无，不能因没有提供信息便填写“无”。参见 [Elsevier 声明说明](https://www.elsevier.support/publishing/answer/what-are-conflict-of-interest-statements-funding-source-declarations-author-agreementsdeclarations-and-permission-notes)。

### 4. 数据可获得性尚未形成最终承诺

当前声明如实说公共发布、许可和永久标识待作者确认；适合作为内部草稿，但最终提交时应明确审稿人能获取什么、代码与原始结果如何获取。不能将“保存在本地项目”写成“已公开”。本刊采用哪一级数据共享政策尚未核实。匿名数据链接本身也需要检查是否暴露账号、机构或作者。

### 5. 图片可交付，但有质量改进点

两幅主文 PDF 均嵌入字体，目前为 Type 3；Elsevier 通用图稿说明建议尽可能使用 Type 1/TrueType。建议后续绘图导出设置嵌入 TrueType，避免字体替换风险。当前 PNG 为 180/160 dpi 元数据；实际有效分辨率还取决于排版尺寸，不能只看元数据判定不合格。

通用像素图参考标准为照片 300 dpi、组合图 500 dpi、线图 1000 dpi；矢量 PDF 不按上述像素阈值判断。来源：[图稿格式与字体](https://www.elsevier.com/en-in/about/policies-and-standards/author/artwork-and-media-instructions/artwork-overview)、[图稿尺寸](https://www.prod.webpresence.elsevier.com/about/policies-and-standards/author/artwork-and-media-instructions/artwork-sizing)。

### 6. TRB 转投的状态需确认

需要确认旧稿已经结束审理、是否仅作会议展示/摘要，还是进入正式论文集或其他出版物。不能将“收到过 TRB 审稿意见”直接等同于已发表，也不能直接等同于未发表。新投本刊通常不把旧刊审稿回复作为默认必传文件；如果是正式转稿或系统要求，再按要求提供。

## Word 检查的实际范围

- LibreOffice 已成功将当前 `manuscript_anonymous.docx` 渲染为 22 页 PDF，保存在 `/tmp/multra_requirement_audit/manuscript_anonymous.pdf`。
- 检查了全部页的文字提取，视觉抽查第 1、11 页；没有声称逐页完成视觉检查。
- 首页面标题及正文可读，第 11 页图表可读；第一页无显式 Abstract 标题，表格跨页排版需优化。
- DOCX 作者元数据为空；无跟踪插入/删除；`comments.xml` 存在但为空，未发现批注内容。
- 本次未改动论文结果、正文、引用或已有投稿 ZIP；因此旧 `CURRENT_VALIDATION.json` 的文件哈希未被本次审计改写。

## 建议的上传分类

1. **Manuscript / anonymized manuscript**：最终匿名 DOCX；如本刊系统明确允许 LaTeX，再按其格式提交 PDF 和源文件。
2. **Title page**：单独的作者信息页，完成通讯作者和声明后上传。
3. **Highlights**：新增的 `Highlights.docx`。
4. **Figure 1 / Figure 2**：两幅独立矢量原图，编号、图注和正文对应。
5. **Declaration of competing interest**：经作者确认的文件或系统生成声明。
6. **Supplementary material**：整理后的匿名证据包与内容说明。
7. **Cover letter**：简要介绍贡献及期刊适配性，不夸大 LLM 优势。

最后核对系统生成的审稿 PDF；检查标题页是否被错误并入审稿版本、符号/表格是否损坏、作者元数据与填写字段是否一致。以实际上传页面分类及可见范围为准。

参见 [Elsevier 文件准备说明](https://www.elsevier.support/publishing/answer/how-do-i-prepare-my-files-for-submission-in-editorial-manager) 和 [Cover letter 说明](https://www.elsevier.support/publishing/answer/what-should-be-included-in-a-cover-letter)。Cover letter 不宜混入另有填写位置的资助、声明或推荐审稿人信息。

## 仍须从本刊实时指南/页面确认

- Word 是否唯一接受的编辑格式，LaTeX 能否用于本刊当前投稿类型。
- 总字数、页数、关键词数量限制；行距、连续行号和引用样式。
- Highlights、图形摘要、Cover letter、推荐审稿人及各声明附件是否必填。
- 自引匿名规则是否有本刊特别说明；补充材料文件限制和数据共享政策。
- 当前 APC、减免及适用日期。没有根据旧版免费通知或 WTC 合作说明承诺普通稿件免费，也没有根据历史价目表报出当前收费。

目前可判断：科学结果和主要稿件已准备，但投稿文件尚未全部定稿；主要阻塞是作者声明、内容匿名规则和当前指南尚未完整取得。没有发现“必须证明 LLM 显著优于基线”这一形式提交要求，也不能把它推断为本刊录用条件。
