#!/usr/bin/env python3
"""Validate review structure and source locations, then optionally render Markdown."""
import argparse
import json
import re
from pathlib import Path
from check_facts import load, output_path
from extract_workpapers import sha256

CATEGORIES = {"documentation_issue", "evidence_gap", "potential_control_exception", "professional_judgment"}
STATUSES = {"reviewed", "partial", "not_reviewed", "unsupported"}
LABELS = {"documentation_issue": "底稿记录问题", "evidence_gap": "证据不足",
          "potential_control_exception": "疑似控制例外", "professional_judgment": "需要专业判断",
          "reviewed": "已完成所列检查", "partial": "部分完成", "not_reviewed": "未复核", "unsupported": "格式未支持"}


def image_ids(file):
    return {i["image_id"] for i in file.get("images", [])} | {
        i["image_id"] for s in file.get("sheets", []) for i in s.get("images", [])}


def validate(manifest, review):
    errors = []
    files = {f["file_id"]: f for f in manifest["files"]}

    def text(obj, key, prefix):
        if not isinstance(obj.get(key), str) or not obj[key].strip():
            errors.append(prefix + ": missing/non-text " + key)

    def strings(obj, key, prefix):
        value = obj.get(key)
        if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
            errors.append(prefix + ": expected text array " + key)
            return []
        return value

    if review.get("schema_version") != "1.0":
        errors.append("Unsupported review schema_version")
    text(review, "scope", "review")
    strings(review, "criteria", "review")
    limitations = strings(review, "limitations", "review")
    if not review.get("criteria"):
        errors.append("State criteria or explicitly state that project criteria are unconfirmed")
    coverage = review.get("coverage")
    if not isinstance(coverage, list):
        return errors + ["coverage must be an array"]
    seen = set()
    for cov in coverage:
        if not isinstance(cov, dict):
            errors.append("coverage item must be an object")
            continue
        fid = cov.get("file_id")
        prefix = "coverage " + str(fid)
        if fid not in files or fid in seen:
            errors.append(prefix + ": unknown or duplicate file_id")
            continue
        seen.add(fid)
        file = files[fid]
        status = cov.get("status")
        if status not in STATUSES:
            errors.append(prefix + ": invalid status")
        text(cov, "reason", prefix)
        done = strings(cov, "checks_performed", prefix)
        remaining = strings(cov, "checks_not_performed", prefix)
        reviewed_images = strings(cov, "images_reviewed", prefix)
        unread_images = strings(cov, "images_not_reviewed", prefix)
        if len(reviewed_images) != len(set(reviewed_images)) or len(unread_images) != len(set(unread_images)):
            errors.append(prefix + ": duplicate image coverage")
        if set(reviewed_images) & set(unread_images) or set(reviewed_images) | set(unread_images) != image_ids(file):
            errors.append(prefix + ": image coverage does not exactly match extracted images")
        if status == "reviewed" and (file["status"] != "extracted" or remaining or unread_images or not done):
            errors.append(prefix + ": cannot mark reviewed with extraction/coverage gaps or no completed checks")
        if status in {"not_reviewed", "unsupported"} and (done or reviewed_images):
            errors.append(prefix + ": unreviewed status conflicts with completed checks/images")
        if status != "reviewed" and not limitations:
            errors.append(prefix + ": partial/unreviewed coverage requires stated limitations")
    if seen != set(files):
        errors.append("Missing coverage for: " + ", ".join(sorted(set(files) - seen)))
    if not files and not limitations:
        errors.append("Empty input must state that no review was performed")

    notes = review.get("notes")
    if not isinstance(notes, list):
        return errors + ["notes must be an array"]
    ids = set()
    for note in notes:
        if not isinstance(note, dict):
            errors.append("note must be an object")
            continue
        prefix = "note " + str(note.get("id"))
        for key in ("id", "rule_id", "control_id", "title", "observation", "basis", "implication", "requested_action"):
            text(note, key, prefix)
        nid = note.get("id")
        if isinstance(nid, str):
            if nid in ids:
                errors.append(prefix + ": duplicate note id")
            ids.add(nid)
        if note.get("category") not in CATEGORIES:
            errors.append(prefix + ": invalid category")
        if note.get("priority") not in {"high", "normal", "low"}:
            errors.append(prefix + ": invalid priority (not a deficiency grade)")
        locations = note.get("locations")
        if not isinstance(locations, list) or not locations:
            errors.append(prefix + ": locations must be a nonempty array")
            continue
        for loc in locations:
            if not isinstance(loc, dict) or loc.get("file_id") not in files:
                errors.append(prefix + ": unknown location file")
                continue
            file = files[loc["file_id"]]
            if any(k not in {"file_id", "sheet", "cell", "page", "image_id"} for k in loc):
                errors.append(prefix + ": unsupported location field")
            kinds = int("sheet" in loc or "cell" in loc) + int("page" in loc) + int("image_id" in loc)
            if kinds > 1:
                errors.append(prefix + ": mixed location kinds")
            if "sheet" in loc or "cell" in loc:
                sheets = [s for s in file.get("sheets", []) if s["name"] == loc.get("sheet")]
                if len(sheets) != 1 or loc.get("cell") not in {c["ref"] for c in sheets[0]["cells"]}:
                    errors.append(prefix + ": nonexistent sheet/cell")
            elif "page" in loc:
                if not isinstance(loc["page"], int) or isinstance(loc["page"], bool) or loc["page"] not in {p["page"] for p in file.get("pages", [])}:
                    errors.append(prefix + ": nonexistent page")
            elif "image_id" in loc and loc["image_id"] not in image_ids(file):
                errors.append(prefix + ": nonexistent image")
    return errors


def verify_sources(manifest):
    errors = []
    for file in manifest["files"]:
        if not file.get("sha256"):
            continue  # Unreadable sources without a hash remain coverage limitations.
        try:
            if sha256(Path(file["absolute_path"])) != file["sha256"]:
                errors.append("Source changed since extraction: " + file["relative_path"])
        except OSError:
            errors.append("Source cannot be rechecked: " + file["relative_path"])
    return errors


def plain(value):
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


def render(manifest, review):
    files = {f["file_id"]: f for f in manifest["files"]}

    def where(loc):
        name = files[loc["file_id"]]["relative_path"]
        if "sheet" in loc:
            name += " / " + loc["sheet"] + "!" + loc["cell"]
        elif "page" in loc:
            name += " / 第 %s 页" % loc["page"]
        elif "image_id" in loc:
            name += " / " + loc["image_id"]
        return plain(name)

    lines = ["# IT 审计底稿复核意见", "", "本报告为辅助复核意见；处理优先级不等于缺陷等级。", "",
             "## 范围", "", review["scope"], "", "## 依据", ""]
    lines += ["- " + plain(s) for s in review["criteria"]]
    lines += ["", "## 限制与未完成事项", ""]
    lines += ["- " + plain(s) for s in review["limitations"]] or ["未另列限制；范围以覆盖记录为准。"]
    lines += ["", "## 覆盖情况", "", "| 文件 | 状态 | 已完成检查数 | 未完成检查数 | 图片已看/未看 |", "|---|---|---|---|---|"]
    for cov in review["coverage"]:
        lines.append("| %s | %s | %s | %s | %d / %d |" % (
            plain(files[cov["file_id"]]["relative_path"]), LABELS[cov["status"]],
            len(cov["checks_performed"]), len(cov["checks_not_performed"]),
            len(cov["images_reviewed"]), len(cov["images_not_reviewed"])))
    lines += ["", "检查数表示覆盖记录条目，不是样本数或通过率。", ""]
    for cov in review["coverage"]:
        lines += ["### " + plain(files[cov["file_id"]]["relative_path"]), "", plain(cov["reason"]), "", "已完成：", ""]
        lines += ["- " + plain(s) for s in cov["checks_performed"]] or ["- 无"]
        lines += ["", "未完成：", ""]
        lines += ["- " + plain(s) for s in cov["checks_not_performed"]] or ["- 无"]
    lines += ["", "## 复核意见", ""]
    if not review["notes"]:
        lines.append("在所列已完成检查中未形成复核意见；这不表示控制有效。")
    for note in review["notes"]:
        lines += ["### %s %s" % (plain(note["id"]), plain(note["title"])), "",
                  "- 控制 / 规则：%s / %s" % (plain(note["control_id"]), plain(note["rule_id"])),
                  "- 类型：%s；处理优先级：%s" % (LABELS[note["category"]], plain(note["priority"])),
                  "- 位置：" + "；".join(where(loc) for loc in note["locations"]),
                  "- 观察：" + plain(note["observation"]), "- 依据：" + plain(note["basis"]),
                  "- 影响：" + plain(note["implication"]), "- 建议：" + plain(note["requested_action"]), ""]
    lines += ["## 输入校验信息", "", "| 文件 | SHA256 | 抽取状态 |", "|---|---|---|"]
    for file in files.values():
        lines.append("| %s | %s | %s |" % (plain(file["relative_path"]), file.get("sha256") or "未取得", file["status"]))
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("review")
    parser.add_argument("--report")
    args = parser.parse_args()
    try:
        manifest, review = load(args.manifest), load(args.review)
        errors = validate(manifest, review) + verify_sources(manifest)
        if errors:
            parser.exit(2, "Validation failed:\n" + "\n".join("- " + e for e in errors) + "\n")
        if args.report:
            dest = output_path(args.report, manifest, (args.manifest, args.review))
            dest.write_text(render(manifest, review), encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, "Validation refused: %s\n" % exc)
    print("Structure, coverage, and location validation passed; semantic audit review remains required.")


if __name__ == "__main__":
    main()
