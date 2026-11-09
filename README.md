# IT 审计底稿复核 skill 原型

第一版选择 Excel 底稿、内嵌截图及独立证据附件，面向具备文件读取、Python 执行及图片查看能力的 Codex 或同类 agent。包含通用、ITGC、ITAC 共21条复核规则，以及6项确定性候选检查。输出独立的中文 review notes、结构化 JSON 和未完成检查说明。

## 获取与使用

下载并解压 [it-audit-review.zip](it-audit-review.zip) 得到 `it-audit-review` 文件夹。仓库中的完整源代码位于 [skills/it-audit-review/](skills/it-audit-review/)，入口为 [SKILL.md](skills/it-audit-review/SKILL.md)。

Codex 本地试用可将该文件夹放入工作区 `.agents/skills/it-audit-review/`；个人作用域可放入 `~/.agents/skills/it-audit-review/`。加载位置及 `$` 调用方式参见 [OpenAI 官方文档](https://learn.chatgpt.com/docs/build-skills#where-to-save-skills)。将完整 skill 目录放入相应加载位置后调用。

在 agent 中使用：

```text
使用 $it-audit-review，只读复核 /绝对路径/底稿及附件，
把复核结果写入 /绝对路径/本次复核输出。
先识别 ITGC / ITAC，逐条定位证据；项目方法论未提供的要求列为待确认。
```

其他 agent 可直接读取本包 `SKILL.md` 作为入口指令；加载与调用方式按其宿主配置。三个 Python 脚本不调用模型API，不需要模型API密钥。

## 跑合成演示

输入文件在 [合成演示底稿](skills/it-audit-review/assets/demo/input/synthetic-it-audit.xlsx)，两张截图均明确标注 SYNTHETIC，不含客户数据。请让 agent 读取此输入目录，采用 [project.example.json](skills/it-audit-review/assets/project.example.json) 的演示配置，输出到输入目录之外的新工作目录。不要把整个 skill 目录当成审计输入。

演示已形成7条复核意见，其中2条疑似控制例外、5条证据缺口；详见 [合成案例复核示例](examples/review-notes.md) 和 [结构化输出](examples/review.json)。例如普通变更上线后批准、金额1000的边界场景与规则不一致，以及只有一张截图却被多个样本引用。紧急变更未直接套用普通变更规则。覆盖状态为部分完成，原因和未完成事项已保留。

## 第一版实际实现

| 部分 | 能力 |
|---|---|
| Skill流程 | 控制与证据对应、领域规则选择、语义复核、图片查看、意见及覆盖说明 |
| 文件抽取 | XLSX/XLSM单元格、隐藏表、公式与保存缓存、传统批注、合并区、内嵌图片及锚点；独立图片与UTF-8文本/CSV |
| 自动事实核对 | 按明确列映射及样本范围，检查普通变更时间顺序、字段、重复编号、独立证据引用，以及配置的金额阈值结果 |
| 输出校验 | 检查意见结构、原始位置、图片及文件覆盖；复核结束重新核对输入文件哈希 |

Python 3.10+；核心流程使用标准库。PDF文本抽取需要现有环境可用的可选 `pypdf`；无依赖或扫描页将明确列出读取限制。脚本不会执行宏、外链或Excel公式，不会自动进行OCR。图片语义判断依赖 agent 的视觉能力。

对于未支持的图形、文本框、图表内容、特殊公式及外部工作簿链接，清单会报告限制；不能因为抽出了图片就把文件视为已完成审计复核。Word与旧版`.xls`尚未实现解析。

## 验证与使用边界

30项回归测试通过，覆盖只读、隐藏表及图片定位、公式缺缓存、普通/紧急规则、日期精度和时区、阈值边界、脚注范围、证据及覆盖定位、源文件变化和输出保护。skill结构校验通过。另由独立 agent 在不知道预设答案的条件下完整执行合成复核，核对过两张图片及所有意见定位。

这不是基于真实客户底稿测得的准确率。尚需用经授权脱敏且由审计经理标注的案例，评价重要问题漏报、误报、引用正确性和节省时间，再决定推广范围。

第一版规则来自公开原则及复核实现建议，不包含特定机构的内部方法论。正式项目需要绑定适用准则、内部方法论版本、控制政策、抽样依据、系统和期间，以及模板映射。未知样本量、工作日时限和缺陷等级不由模型自行设定。最终审计判断及正式复核签署由审计人员完成。

每次用新输出目录；迭代时换用新报告名或先归档本次生成的旧结果。脚本不会覆盖输入或已有结果。


## 本地运行与测试

以下命令在仓库根目录运行。输出目录需尚未使用，且不能放入底稿输入目录。

```bash
python3 skills/it-audit-review/scripts/extract_workpapers.py skills/it-audit-review/assets/demo/input --output-dir work/demo/extracted
python3 skills/it-audit-review/scripts/check_facts.py work/demo/extracted/manifest.json --config skills/it-audit-review/assets/project.example.json --out work/demo/facts.json
python3 -m unittest discover -s skills/it-audit-review/tests
```

前两条命令完成抽取及确定性核对。完整复核仍需 agent 按 `SKILL.md` 查看证据、形成 `review.json`，再运行 `validate_review.py` 校验并生成报告。
