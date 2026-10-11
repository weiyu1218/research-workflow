# research-workflow

Model-independent research workflow and paper-reading skill, built with the
[Agent Skills format](https://agentskills.io/specification).

面向生命科学与 AI 交叉研究，组织科研项目、文献调研、论文精读、baseline 复现、
实验记录、会话交接，以及文献与调研汇报。生物学问题与计算方法沿同一条主线解释，
不分成互不相干的阅读路线。

技能主体不依赖 Codex 工具名、模型 API、特定 MCP 服务或其他已安装技能。
Claude、DeepSeek、Codex 等模型能执行哪些步骤，取决于其所在客户端提供的
文件、联网、命令执行和图片能力；下载技能本身不会给纯聊天界面增加这些能力。

## 加载与使用

下载完整仓库，保持 `SKILL.md`、`paper-reading/`、`scripts/` 和 `assets/` 的相对结构。
原生支持 Agent Skills 的客户端将目录放入其技能目录；不需要转换多套模型版本。
支持导入压缩包的客户端按其实际要求导入整个技能目录，入口为 `SKILL.md`。

[Claude Code](https://code.claude.com/docs/en/skills) 的个人技能安装方式：

```bash
git clone https://github.com/weiyu1218/research-workflow.git ~/.claude/skills/research-workflow
```

在 Claude Code 中可用 `/research-workflow` 调用。

[Codex](https://developers.openai.com/codex/skills/) 的个人技能安装方式：

```bash
git clone https://github.com/weiyu1218/research-workflow.git ~/.agents/skills/research-workflow
```

在 Codex 中可用 `$research-workflow` 调用。已有其他路径安装时就地更新，
避免同名技能重复安装；`agents/openai.yaml` 只提供可选的 Codex 界面信息。

使用 DeepSeek 或其他模型的客户端时，先确认客户端是否支持技能加载，
使用它文档规定的目录或导入方式，本仓库不假定存在统一的 DeepSeek 安装路径。
只有普通聊天功能时，把 `SKILL.md` 作为指令材料提供；需要精读时再提供
`paper-reading/guide.md`、必要的参考文件及论文原文。附件或文件若未被客户端
实际读取，不能认为仅发送仓库链接就已加载技能。

通用调用示例：

```text
使用 research-workflow，调研蛋白复合物识别中的超图方法，检索 PubMed 与 arXiv，
按问题筛选和归档，不进入精读。

使用 research-workflow 精读这篇 PDF。先核查源码、补充材料与官方代码，
从原文和必要背景开始，用同一样本讲清数据、输入输出、变换与信息流。
```

仅调用技能、下载论文或存在 `related_work/` 均不启动精读。
只有用户明确要求精读、逐节讲解或使用内置精读模块时，才加载该模块。

## 架构与职责

```text
research-workflow/
  SKILL.md                         通用入口、能力判断、任务路由与科研工作流
  agents/openai.yaml               可选 Codex 界面元数据，其他宿主可忽略
  assets/templates/                项目总纲、交接、数据、假设与实验记录模板
  scripts/init-project.sh          新项目骨架；已有文件不覆盖
  scripts/fetch-paper.sh           arXiv PDF 与源码获取、清洗
  paper-reading/
    guide.md                       七站精读、材料验收、讲解与笔记规则
    requirements.txt               Python 材料准备依赖
    scripts/fetch_paper.py          多来源获取、源码展开、文本、大纲、附件与代码
    scripts/extract_figures.py      图表提取与索引
    scripts/crop.py                 裁图、局部放大及公式截图
    references/examples.md         KSHL-PC 的具体七站讲解与数值演算
    references/notes-template.md   持续维护的精读笔记结构
    references/figures/            上述示例引用的原论文图
    profile.example.md             可选用户偏好模板；个人画像不随仓库发布
```

入口按任务加载必要文件。普通调研不加载整套精读指南；精读模块内置于本仓库，
无需另装 kelip-paper-reading。`research-ppt` 等汇报制作技能是可选扩展；
未安装时仍可组织论文证据、逐页内容和讲稿，有实际制作工具时再生成演示文件。

科研项目复用既有目录，典型结构为：

```text
<项目>/
  AGENTS.md                     科研总纲，与宿主已有项目指令共同遵守
  handoff.md                    已验证事实、停点、未完成事项及下一步
  brainstorm.md                 研究问题与可检验假设
  related_work/<论文>/          原文、材料状态与必要派生产物
  复现/                         baseline 代码与复现记录
  experiment/results.md         实验设置、结果与结论
  experiment/evaluation.md      数据、指标、对照与标准化测评
  DataSet/ + dataset.md         数据与处理说明
```

宿主不自动加载 `AGENTS.md` 时主动读取，已有 `CLAUDE.md` 等指令继续生效。
不为切换模型复制一套总纲；后续会话通过同一份 `handoff.md` 接续工作。

## 精读的数据流

完整精读先核对论文身份与实际版本，分别查询 LaTeX 源码、补充材料和官方代码，
按可用能力准备 `paper.pdf`、`source/`、`paper.tex`、`paper.txt`、`outline.md`、
`meta.json`、`supplements/`、`figures/` 和 `code/`。作者未公开的内容不伪造；
源码版本与正式 PDF 不一致时明确区分。补附件可用 `--supplements-only`，
保留已整理的正文、图表、代码和笔记。

七站为：动机与生物学问题、方法架构、训练与目标、数据与样本、信息流、
具体例子、实验与局限及研究空白。一站可分多轮，继续时接续当前未完操作。
每轮先给原文定位或短概括及必要背景，再沿同一样本追踪：

```text
原始记录 -> 加载与预处理 -> 模型实参 -> 中间表示 -> 输出 -> 原研究对象与证据
```

解释字段、形状、单位、对象 ID、标签与参数更新；关键变换用小矩阵或数组
展示操作前后并实际算清一个值。真实样本、代码核对、实际执行与教学数字分开标注。
阅读中更新同一份 `精读笔记.md`，结束后在该文件浓缩，不把七个标题出现视为读完。

图使用宿主支持的图片嵌入或原生附件；本地路径方式在发送前检查当前实际路径，
项目改名后重新定位。笔记保留相对图片路径，便于随论文目录移动。
没有视觉或展示能力时说明限制，给实际图号与页码，不声称已检查或显示图片。

## 执行环境

指令阅读只需能读取 Markdown；自动材料准备需要 Python 3.9+、PyMuPDF、
文件读写和相应网络访问。Git 只在克隆官方代码时需要。
在当前机器的隔离环境安装 `paper-reading/requirements.txt`，不分发或跨机器
复用 `.venv/`；运行前检查同一解释器的 `fitz.Rect(0, 0, 1, 1).get_area()`。

Bash 脚本需要 Bash 和相应 Unix 工具，具体依赖见 `SKILL.md`。
Windows 可在已配置的 WSL 或 Git Bash 中运行这些 shell 脚本，或用可用的
Python 解释器执行 Python 材料脚本；PowerShell 中须使用其自身变量和调用语法。
没有命令执行能力时使用实际已有材料与宿主原生能力，不声称运行过脚本。

无联网时外部材料标为尚未核查或无法访问；无文件写入能力时在会话中交付可复制
笔记正文。缺全文就请求必要原文，不由摘要补造精读内容。只有聊天能力时不能
保证完成材料落盘、代码克隆或图片提取，这些限制不改变证据与真实性要求。

## 来源与许可

基于 [skJack/research-workflow](https://github.com/skJack/research-workflow)
和 [skJack/kelip-paper-reading](https://github.com/skJack/kelip-paper-reading)
改编，加入生命科学与 AI 交叉研究的讲解、材料核查和宿主能力适配。
本仓库是改编版本，不代表原作者发布。

工作流沿用 [Apache-2.0](LICENSE)，内置精读模块沿用其 [MIT 许可](paper-reading/LICENSE)。
示例引用的论文图注明原论文及图号，其版权归原权利人，不由上述软件许可重新授权。
