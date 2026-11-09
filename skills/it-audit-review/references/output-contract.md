# 输出约定

生成 UTF-8 `review.json`，结构为：

```json
{
  "schema_version": "1.0",
  "scope": "本次实际复核的期间、系统、控制和测试范围",
  "criteria": ["适用项目准则/方法论及版本；未确认项明确注明"],
  "limitations": ["缺少哪些材料或能力，以及具体受影响的检查"],
  "coverage": [
    {
      "file_id": "清单中的file_id",
      "status": "reviewed",
      "checks_performed": ["已完成的具体规则或程序"],
      "checks_not_performed": ["未完成的程序及原因"],
      "images_reviewed": ["已查看的image_id"],
      "images_not_reviewed": ["未查看的image_id"],
      "reason": "覆盖范围说明"
    }
  ],
  "notes": [
    {
      "id": "RN-001",
      "rule_id": "ITGC-001",
      "control_id": "底稿控制编号；无法识别时写unknown",
      "category": "evidence_gap",
      "priority": "normal",
      "title": "具体问题",
      "observation": "可核实的事实，避免把推断当事实",
      "basis": "具体控制政策、项目方法论或规则依据",
      "implication": "对当前测试或结论的影响，区分事实与推断",
      "requested_action": "建议补充的证据、程序或判断",
      "locations": [{"file_id": "清单中的file_id", "sheet": "工作表", "cell": "D3"}]
    }
  ]
}
```

覆盖状态只能是 `reviewed`、`partial`、`not_reviewed`、`unsupported`；有抽取失败、未完成的相关检查或未看的图片时不得标 `reviewed`。每个输入文件必须有且仅有一条覆盖记录；零文件清单必须列出限制。图片覆盖应逐一列出，是否查看不意味着该图已证明客户控制执行。

定位支持：`{file_id,sheet,cell}`、`{file_id,page}`、`{file_id,image_id}`、或 `{file_id}`（仅用于整个文件问题或文本文件；尽可能精确）。每条意见至少一个位置，可同时列底稿和证据位置。仅靠图片位置无法表达具体文字时，在 observation 说明对象/时间和可识别区域，不编造单元格。

`category`：`documentation_issue`、`evidence_gap`、`potential_control_exception`、`professional_judgment`。`priority`：`high`、`normal`、`low`，仅用于处理顺序，不等于重大缺陷/重要缺陷。

`validate_review.py` 验证结构、枚举、引用位置及覆盖遗漏，拒绝不存在的单元格/页/图片、重复文件覆盖、未说明的不可读文件。覆盖中的完整图片ID集合必须对应清单。命令行还会重新计算可取得哈希的原文件，原文件自抽取后发生变化则拒绝交付，需在稳定副本上重做受影响检查。它无法验证文字真实性、引用是否真正支持论点、规则适用性或审计证据的充分性；这些必须逐条人工/agent复核。

中文 Markdown 报告由校验脚本生成，包括范围、依据、限制、覆盖情况和完整 review notes。无需把 JSON 当作最终审计结论。客户原始底稿保持不变。
