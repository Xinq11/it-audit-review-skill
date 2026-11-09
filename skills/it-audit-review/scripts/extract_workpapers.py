#!/usr/bin/env python3
"""Read-only workpaper extraction. No Office application, macros, or network calls."""
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import posixpath
import re
import shutil
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "p": "http://schemas.openxmlformats.org/package/2006/relationships",
      "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
MAX_BYTES = 100 * 1024 * 1024
MAX_CELLS = 200000
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".emf", ".wmf"}


def xml(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("DTD/entity declarations are not supported")
    return ET.fromstring(data)


def texts(element):
    return "".join(node.text or "" for node in element.iter() if node.tag.rsplit("}", 1)[-1] == "t")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def column_name(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


class Xlsx:
    def __init__(self, path, record, output):
        self.zip = zipfile.ZipFile(path)
        self.record, self.output = record, output
        infos = self.zip.infolist()
        if sum(item.file_size for item in infos) > MAX_BYTES:
            self.zip.close()
            raise ValueError("OOXML expanded package exceeds 100 MiB limit; nothing extracted")
        if len({item.filename for item in infos}) != len(infos):
            self.zip.close()
            raise ValueError("Duplicate OOXML package members are ambiguous")
        self.names = set(self.zip.namelist())
        self.image_index = 0

    def read(self, name):
        return xml(self.zip.read(name))

    def warn(self, message):
        if message not in self.record["warnings"]:
            self.record["warnings"].append(message)
        self.record["status"] = "partial"

    def rels(self, name):
        relname = posixpath.join(posixpath.dirname(name), "_rels", posixpath.basename(name) + ".rels")
        result = {}
        if relname not in self.names:
            return result
        for node in self.read(relname):
            if node.get("TargetMode") == "External":
                self.warn("External relationship not followed: " + name)
                continue
            target = node.get("Target", "")
            target = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(name), target))
            if target.startswith("../") or target == ".." or "\\" in target:
                self.warn("Unsafe relationship target ignored: " + name)
                continue
            result[node.get("Id")] = (target, node.get("Type", "").rsplit("/", 1)[-1])
        return result

    def dates(self):
        if "xl/styles.xml" not in self.names:
            return set()
        root = self.read("xl/styles.xml")
        formats = {int(n.get("numFmtId")): n.get("formatCode", "") for n in root.findall("s:numFmts/s:numFmt", NS)}
        result = set()
        for index, node in enumerate(root.findall("s:cellXfs/s:xf", NS)):
            number = int(node.get("numFmtId", "0"))
            code = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', "", formats.get(number, "")).lower()
            # Locale-dependent/time-only built-ins stay raw rather than inventing dates.
            if number in set(range(14, 18)) | {22} or re.search("[yd]", code):
                result.add(index)
        return result

    def image(self, target, anchor):
        self.image_index += 1
        image_id = "image_%04d" % self.image_index
        suffix = Path(target).suffix.lower()
        filename = self.record["file_id"] + "_" + image_id + (suffix if suffix in IMAGE_TYPES else ".bin")
        destination = self.output / "media" / filename
        destination.write_bytes(self.zip.read(target))
        return {"image_id": image_id, "anchor": anchor, "media_path": "media/" + filename,
                "package_path": target, "status": "requires_visual_review"}

    def images(self, sheet, sheet_name, relationships):
        result = []
        for node in sheet.findall("s:drawing", NS):
            rid = node.get("{" + NS["r"] + "}id")
            drawing = relationships.get(rid, (None,))[0]
            if not drawing or drawing not in self.names:
                self.warn("Drawing relationship missing: " + sheet_name)
                continue
            drawing_rels = self.rels(drawing)
            for anchor in self.read(drawing):
                if (any(n.tag.rsplit("}", 1)[-1] in {"sp", "grpSp", "cxnSp", "graphicFrame", "contentPart"} for n in anchor.iter())
                        or any(n.tag == "{" + NS["a"] + "}t" for n in anchor.iter())):
                    self.warn("Drawing contains unextracted shape/textbox/chart content; visual review required: " + sheet_name + " / " + drawing)
                start = anchor.find("xdr:from", NS)
                location = {"sheet": sheet_name, "kind": anchor.tag.rsplit("}", 1)[-1]}
                if start is not None:
                    row = int(start.findtext("xdr:row", "0", NS)) + 1
                    col = int(start.findtext("xdr:col", "0", NS)) + 1
                    location.update({"cell": column_name(col) + str(row), "row": row, "column": col})
                for blip in anchor.findall(".//a:blip", NS):
                    rid = blip.get("{" + NS["r"] + "}embed")
                    target = drawing_rels.get(rid, (None,))[0]
                    if target and target in self.names:
                        result.append(self.image(target, location))
                    else:
                        self.warn("Image relationship missing or linked externally: " + sheet_name)
        return result

    def extract(self):
        try:
            root = self.read("xl/workbook.xml")
            if root.tag != "{" + NS["s"] + "}workbook":
                raise ValueError("Unsupported OOXML namespace or workbook type")
            workbook_rels = self.rels("xl/workbook.xml")
            shared = [texts(n) for n in self.read("xl/sharedStrings.xml")] if "xl/sharedStrings.xml" in self.names else []
            date_styles = self.dates()
            props = root.find("s:workbookPr", NS)
            date1904 = props is not None and props.get("date1904") in {"1", "true"}
            for node in root.findall("s:sheets/s:sheet", NS):
                name = node.get("name", "")
                entry = {"name": name, "state": node.get("state", "visible"), "cells": [], "merged_ranges": [], "images": []}
                self.record["sheets"].append(entry)
                target, kind = workbook_rels.get(node.get("{" + NS["r"] + "}id"), (None, ""))
                if kind != "worksheet" or target not in self.names:
                    self.warn("Unsupported or missing worksheet: " + name)
                    continue
                sheet, relationships = self.read(target), self.rels(target)
                comments = {}
                for related, relkind in relationships.values():
                    if relkind == "comments" and related in self.names:
                        comments.update({n.get("ref"): texts(n) for n in self.read(related).findall("s:commentList/s:comment", NS)})
                for cell in sheet.findall("s:sheetData/s:row/s:c", NS):
                    if len(entry["cells"]) >= MAX_CELLS:
                        self.warn("Cell extraction stopped at 200000 cells in sheet: " + name)
                        break
                    ref, typ = cell.get("r"), cell.get("t", "n")
                    value = cell.findtext("s:v", None, NS)
                    formula = cell.find("s:f", NS)
                    if typ == "s" and value is not None:
                        value = shared[int(value)]
                    elif typ == "inlineStr":
                        value = texts(cell)
                    elif typ == "b" and value is not None:
                        value = value == "1"
                    elif typ == "n" and value is not None:
                        number = float(value)
                        if not math.isfinite(number):
                            raise ValueError("Non-finite numeric value in " + name + "!" + str(ref))
                        if int(cell.get("s", "0")) in date_styles:
                            if not date1904 and 60 <= number < 61:
                                self.warn("Excel fictitious 1900-02-29 kept as raw serial: " + name + "!" + ref)
                            else:
                                base = dt.datetime(1904, 1, 1) if date1904 else dt.datetime(1899, 12, 30 if number > 60 else 31)
                                stamp = base + dt.timedelta(days=number)
                                value = stamp.date().isoformat() if stamp.time() == dt.time() else stamp.isoformat()
                        else:
                            value = int(number) if number.is_integer() else number
                    data = {"ref": ref, "value": value}
                    if formula is not None:
                        data.update({"formula": "=" + (formula.text or ""), "cached_value": value})
                        if formula.get("t") in {"shared", "array", "dataTable"}:
                            data["formula_attributes"] = formula.attrib
                            self.warn("Special formula retained without expansion/recalculation: " + name + "!" + ref)
                        if value is None:
                            self.warn("Formula has no cached value; not recalculated: " + name + "!" + ref)
                    if ref in comments:
                        data["comment"] = comments.pop(ref)
                    if value is not None or formula is not None or "comment" in data:
                        entry["cells"].append(data)
                entry["cells"].extend({"ref": ref, "value": None, "comment": comment} for ref, comment in comments.items())
                entry["merged_ranges"] = [n.get("ref") for n in sheet.findall("s:mergeCells/s:mergeCell", NS)]
                entry["images"] = self.images(sheet, name, relationships)
                if sheet.find("s:oleObjects", NS) is not None:
                    self.warn("Embedded OLE objects not opened: " + name)
            handled_media = {image["package_path"] for sheet in self.record["sheets"] for image in sheet["images"]}
            for member in sorted(self.names):
                if member.startswith("xl/media/") and member not in handled_media and not member.endswith("/"):
                    self.record["images"].append(self.image(member, {"kind": "unresolved"}))
                    self.warn("Media extracted without a resolvable sheet anchor: " + member)
            if "xl/vbaProject.bin" in self.names:
                self.warn("VBA project present; macros not executed or reviewed")
            if any("threadedComments" in member for member in self.names):
                self.warn("Threaded comments not supported; legacy cell notes are extracted")
            if any(member.startswith("xl/externalLinks/") and not member.endswith("/") for member in self.names):
                self.warn("External workbook links present; external link parts not resolved or refreshed")
        finally:
            self.zip.close()


def extract_file(path, root, output):
    relative = path.relative_to(root).as_posix()
    file_id = "file_" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
    suffix = path.suffix.lower()
    record = {"file_id": file_id, "relative_path": relative, "absolute_path": str(path), "sha256": None,
              "kind": suffix.lstrip(".") or "unknown", "status": "extracted", "warnings": [], "sheets": [], "pages": [], "images": []}
    try:
        if path.is_symlink():
            raise ValueError("Symbolic link not followed")
        record["sha256"] = sha256(path)
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Source file exceeds 100 MiB limit; nothing extracted")
        if suffix in {".xlsx", ".xlsm"}:
            Xlsx(path, record, output).extract()
        elif suffix in IMAGE_TYPES:
            filename = file_id + "_image_0001" + suffix
            shutil.copyfile(path, output / "media" / filename)
            record["images"].append({"image_id": "image_0001", "anchor": {"kind": "standalone"}, "media_path": "media/" + filename, "status": "requires_visual_review"})
        elif suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                raise ValueError("Optional dependency pypdf is unavailable; PDF not read")
            reader = PdfReader(str(path))
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("Encrypted PDF needs a password; not read")
            for page, item in enumerate(reader.pages, 1):
                try:
                    text = item.extract_text() or ""
                    record["pages"].append({"page": page, "text": text})
                    if not text.strip():
                        record["status"] = "partial"
                        record["warnings"].append("Page %d has no extracted text; visual review/OCR required" % page)
                except Exception as exc:
                    record["status"] = "partial"
                    record["warnings"].append("Page %d extraction failed: %s" % (page, exc))
            record["warnings"].append("PDF text extraction does not review page layout, screenshots, or signatures; visual review may be required")
        elif suffix in {".txt", ".md", ".csv", ".json"}:
            text = path.read_text(encoding="utf-8-sig")
            record["text"] = text
            if suffix == ".csv":
                record["sheets"] = [{"name": "CSV", "state": "visible", "merged_ranges": [], "images": [], "cells": [
                    {"ref": column_name(col) + str(row), "value": value}
                    for row, values in enumerate(csv.reader(io.StringIO(text)), 1) for col, value in enumerate(values, 1) if value != ""]}]
        else:
            record["status"] = "unsupported"
            record["warnings"].append("Unsupported file type; not reviewed")
    except Exception as exc:
        record["status"] = "partial" if record["sheets"] or record["pages"] or record["images"] else "unreadable"
        record["warnings"].append(str(exc))
    if record["sha256"] is not None:
        try:
            if sha256(path) != record["sha256"]:
                record["status"] = "partial"
                record["warnings"].append("Source changed during extraction; discard extracted facts and rerun on a stable copy")
        except OSError:
            record["status"] = "partial"
            record["warnings"].append("Source could not be rechecked after extraction")
    return record


def extract(input_path, output_dir):
    source = Path(input_path).expanduser().absolute()
    if source.is_symlink():
        raise ValueError("Input must not be a symbolic link")
    source = source.resolve(strict=True)
    output = Path(output_dir).expanduser().resolve()
    if output == source or (source.is_dir() and source in output.parents):
        raise ValueError("Output must be outside the input directory and must not overwrite input")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Output directory must be new or empty; existing results are not overwritten")
    root = source if source.is_dir() else source.parent
    paths = sorted(source.rglob("*")) if source.is_dir() else [source]
    output.mkdir(parents=True, exist_ok=True)
    (output / "media").mkdir()
    manifest = {"schema_version": "1.0", "input_root": str(root), "files": [], "warnings": []}
    for path in paths:
        if path.is_file() or path.is_symlink():
            manifest["files"].append(extract_file(path, root, output))
    if not manifest["files"]:
        manifest["warnings"].append("No input files found; no review can be completed")
    destination = output / "manifest.json"
    destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    try:
        destination = extract(args.input, args.output_dir)
    except (OSError, ValueError) as exc:
        parser.exit(2, "Extraction refused: %s\n" % exc)
    print(destination)


if __name__ == "__main__":
    main()
