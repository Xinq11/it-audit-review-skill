#!/usr/bin/env python3
"""Produce candidate notes from explicitly mapped workpaper facts; no audit opinion."""
import argparse
import datetime as dt
import json
import operator
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def output_path(path, manifest, protected=()):
    dest = Path(path).expanduser().resolve()
    root = Path(manifest["input_root"]).resolve()
    if dest == root or root in dest.parents:
        raise ValueError("Output must be outside the source input directory")
    sources = {Path(f["absolute_path"]).resolve() for f in manifest["files"]}
    sources.update(Path(p).resolve() for p in protected)
    if dest in sources:
        raise ValueError("Output must not overwrite any input")
    if dest.exists():
        raise ValueError("Output already exists; choose a new filename or archive the previously generated result")
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def time_value(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?", value
    ):
        raise ValueError("需要 ISO 日期/时间，不能推测日月顺序")
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")), len(value) > 10


def compare_time(first, second):
    left, lp = time_value(first)
    right, rp = time_value(second)
    if (left.tzinfo is None) != (right.tzinfo is None):
        return None, "时间的时区口径不一致"
    if (not lp or not rp) and left.date() == right.date():
        return None, "同日记录缺少时刻，无法判断先后"
    if left == right:
        return None, "时间相同，无法证明执行顺序"
    return (1 if left > right else -1), None


def number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("金额/阈值不是有效数字")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("金额/阈值不是有效数字") from None
    if not result.is_finite():
        raise ValueError("金额/阈值不是有限数字")
    return result


def run(manifest, config):
    if config.get("schema_version") != "1.0" or not isinstance(config.get("tables"), list):
        raise ValueError("Unsupported or missing configuration schema/tables")
    result = {"schema_version": "1.0", "candidate_notes": [], "checks_performed": [],
              "checks_skipped": [], "manual_checks": [],
              "limitation": "候选意见只基于底稿记录；须确认政策、原始证据和规则适用性。"}
    files = manifest["files"]

    def skip(table, reason, row=None):
        result["checks_skipped"].append({"table": table, "row": row, "reason": reason})

    for table in config["tables"]:
        label = str(table.get("file")) + "!" + str(table.get("sheet"))
        matches = [f for f in files if f["relative_path"] == table.get("file")]
        if len(matches) != 1:
            skip(label, "文件不存在或相对路径不唯一")
            continue
        file = matches[0]
        if file["status"] != "extracted":
            skip(label, "文件未完整抽取，须先解决警告或逐项核实后重新配置")
            continue
        sheets = [s for s in file["sheets"] if s["name"] == table.get("sheet")]
        if len(sheets) != 1:
            skip(label, "工作表不存在或不唯一")
            continue
        columns, headers = table.get("columns", {}), table.get("expected_headers", {})
        required = {"sample_id", "control_id", "evidence_ref"}
        if table.get("kind") == "change_samples":
            required |= {"change_type", "approved_at", "tested_at", "deployed_at"}
        elif table.get("kind") == "threshold_cases":
            required |= {"amount", "expected_result", "actual_result"}
        else:
            skip(label, "未支持的表格类型")
            continue
        row_header = table.get("header_row", 1)
        row_start, row_end = table.get("data_start_row"), table.get("data_end_row")
        if (not required.issubset(columns) or set(columns) != set(headers)
                or len(set(columns.values())) != len(columns)
                or any(not isinstance(c, str) or not re.fullmatch("[A-Z]{1,3}", c) for c in columns.values())
                or not isinstance(row_header, int) or isinstance(row_header, bool) or row_header < 1
                or not isinstance(row_start, int) or isinstance(row_start, bool)
                or not isinstance(row_end, int) or isinstance(row_end, bool)
                or not row_header < row_start <= row_end <= 1048576):
            skip(label, "字段、列映射、预期表头或样本起止行配置无效")
            continue
        cells = {c["ref"]: c for c in sheets[0]["cells"]}
        if any(cells.get(col + str(row_header), {}).get("value") != headers[field]
               for field, col in columns.items()):
            skip(label, "表头不匹配；未执行该表检查")
            continue
        rows = sorted({int(m.group(2)) for ref in cells
                       if (m := re.fullmatch(r"([A-Z]+)([1-9]\d*)", ref))
                       and m.group(1) in columns.values() and row_start <= int(m.group(2)) <= row_end})
        if not rows:
            skip(label, "映射表无样本行；不代表零笔发生或已完成总体核验")
            continue
        policy = table.get("policy", {})
        seen = {}

        for row in rows:
            values = {field: cells.get(col + str(row), {}).get("value") for field, col in columns.items()}
            if all(v is None or v == "" for v in values.values()):
                continue
            sample = values["sample_id"]
            control = str(values["control_id"] or "unknown")

            def location(field):
                ref = columns[field] + str(row)
                # A missing cell can only be cited at the existing row identifier.
                if ref not in cells:
                    ref = next((c + str(row) for c in columns.values() if c + str(row) in cells), None)
                return {"file_id": file["file_id"], "sheet": sheets[0]["name"], "cell": ref}

            def note(rule, category, title, observation, fields, action, basis=None):
                locations = []
                for field in fields:
                    loc = location(field)
                    if loc not in locations:
                        locations.append(loc)
                result["candidate_notes"].append({
                    "id": "CAND-%03d" % (len(result["candidate_notes"]) + 1), "rule_id": rule,
                    "control_id": control, "sample_id": sample, "category": category,
                    "priority": "normal", "title": title, "observation": observation,
                    "basis": basis or policy.get("source") or "映射样本表的记录完整性要求，须经项目确认",
                    "implication": "当前记录需要核实或补充，尚未据此判定控制有效性或缺陷等级。",
                    "requested_action": action, "locations": locations})

            if sample in (None, "") or values["control_id"] in (None, ""):
                note("FACT-003", "documentation_issue", "样本标识或控制编号缺失",
                     "映射表第 %d 行缺少样本标识或控制编号。" % row, ["sample_id", "control_id"],
                     "补充标识并核对该行是否属于本次测试。")
            if sample not in (None, ""):
                sample_key = str(sample)
                if sample_key in seen:
                    note("FACT-004", "documentation_issue", "样本编号重复",
                         "样本 %s 在第 %d 行与第 %d 行重复。" % (sample, row, seen[sample_key]),
                         ["sample_id"], "确认是重复记录、多属性测试，还是应使用不同标识。")
                else:
                    seen[sample_key] = row

            evidence = values["evidence_ref"]
            if isinstance(evidence, str) and re.search(r"\.(png|jpe?g|pdf|xlsx?|docx?|gif|webp|csv|txt)$", evidence.strip(), re.I):
                ref = evidence.strip().replace("\\", "/")
                exact = [f for f in files if f["relative_path"] == ref]
                named = [f for f in files if Path(f["relative_path"]).name == Path(ref).name]
                candidates = exact if exact else named
                if len(candidates) != 1:
                    note("FACT-005", "evidence_gap", "证据引用未能唯一匹配",
                         "样本 %s 引用 %s，在本次输入中%s。" % (sample, evidence, "未匹配文件" if not candidates else "存在同名歧义"),
                         ["evidence_ref"], "补充或明确对应附件；核对对象、期间及内容，不能仅靠文件名。")
                else:
                    result["manual_checks"].append({"table": label, "row": row, "sample_id": sample,
                                                   "reason": "附件名称已匹配，仍须检查内容", "file_id": candidates[0]["file_id"]})
            else:
                result["manual_checks"].append({"table": label, "row": row, "sample_id": sample,
                                               "reason": "证据引用须与具体图片或附件人工匹配，尚未验证证据覆盖"})

            critical = required - {"evidence_ref"}
            if any("formula" in cells.get(columns[field] + str(row), {}) for field in critical):
                skip(label, "关键字段包含公式；缓存未重算，需核实后再比较", row)
                continue
            if not isinstance(policy.get("source"), str) or not policy["source"].strip():
                skip(label, "缺少政策依据，不执行政策符合性比较", row)
                continue
            if table["kind"] == "change_samples":
                standard, emergency = policy.get("standard_values", []), policy.get("emergency_values", [])
                if not isinstance(standard, list) or not isinstance(emergency, list) or set(standard) & set(emergency):
                    skip(label, "普通与紧急变更类型配置无效或重叠", row)
                    continue
                if values["change_type"] not in standard:
                    skip(label, "紧急变更需专门政策；未套用普通顺序规则" if values["change_type"] in emergency else "变更类型不在已确认普通类型中", row)
                    continue
                for field, enabled, rule, title in (
                    ("approved_at", "require_preapproval", "FACT-001", "批准时间晚于上线"),
                    ("tested_at", "require_predeployment_test", "FACT-002", "测试时间晚于上线")):
                    if policy.get(enabled) is not True:
                        skip(label, enabled + " 未明确启用", row)
                        continue
                    try:
                        comparison, reason = compare_time(values[field], values["deployed_at"])
                    except (TypeError, ValueError) as exc:
                        note("FACT-003", "documentation_issue", "时间字段缺失或不可解析",
                             "样本 %s 的 %s / deployed_at 无法比较：%s。" % (sample, field, exc),
                             [field, "deployed_at"], "核实日期原值、精度及时区口径。")
                        continue
                    if comparison is None:
                        skip(label, field + ": " + reason, row)
                    else:
                        result["checks_performed"].append({"table": label, "row": row, "rule_id": rule})
                        if comparison > 0:
                            note(rule, "potential_control_exception", title,
                                 "底稿记录样本 %s：%s=%s，deployed_at=%s。" % (sample, field, values[field], values["deployed_at"]),
                                 [field, "deployed_at", "sample_id"], "对照原始时间证据与适用政策，确认例外及对当前结论的影响。")
            else:
                ops = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq}
                if policy.get("operator") not in ops or not all(isinstance(policy.get(k), str) and policy[k] for k in ("when_true", "when_false")):
                    skip(label, "阈值政策配置不完整", row)
                    continue
                try:
                    threshold = number(policy.get("threshold"))
                except ValueError:
                    skip(label, "阈值政策配置不是有效数字", row)
                    continue
                try:
                    amount = number(values["amount"])
                except ValueError:
                    note("FACT-003", "documentation_issue", "测试金额缺失或不可解析",
                         "样本 %s 的金额不可解析。" % sample, ["amount"], "核对金额原值及单位。")
                    continue
                expected = policy["when_true"] if ops[policy["operator"]](amount, threshold) else policy["when_false"]
                result["checks_performed"].append({"table": label, "row": row, "rule_id": "FACT-006"})
                for field, category in (("expected_result", "documentation_issue"), ("actual_result", "potential_control_exception")):
                    if values[field] in (None, ""):
                        note("FACT-003", "documentation_issue", "测试结果记录缺失",
                             "样本 %s 的 %s 未记录。" % (sample, field), [field, "sample_id"], "补充结果及其原始证据。")
                    elif values[field] != expected:
                        note("FACT-006", category, "阈值规则与记录结果不一致",
                             "样本 %s 金额 %s，按配置应为“%s”，%s 记录为“%s”。" % (sample, amount, expected, field, values[field]),
                             ["amount", field, "sample_id"], "核对规则、参数、生效版本和原始测试输出，评价当前结论。")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        manifest = load(args.manifest)
        result = run(manifest, load(args.config))
        dest = output_path(args.out, manifest, (args.manifest, args.config))
        dest.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, "Fact check refused: %s\n" % exc)
    print(dest)


if __name__ == "__main__":
    main()
