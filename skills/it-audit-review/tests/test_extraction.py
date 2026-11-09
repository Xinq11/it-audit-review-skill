"""Behavioral regression tests using synthetic OOXML packages only."""
import base64
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "extract_workpapers.py"
SPEC = importlib.util.spec_from_file_location("extract_workpapers", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
S = MODULE.NS["s"]
R = MODULE.NS["r"]
P = MODULE.NS["p"]
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


def package(path, formula_cache=True):
    cache = "<v>2</v>" if formula_cache else ""
    members = {
        "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Samples" sheetId="1" r:id="rId1" state="hidden"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{P}"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="{R}/worksheet"/></Relationships>',
        "xl/styles.xml": f'<styleSheet xmlns="{S}"><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>',
        "xl/sharedStrings.xml": f'<sst xmlns="{S}"><si><r><t>审批</t></r><r><t>记录</t></r></si></sst>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{S}" xmlns:r="{R}"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><f>1+1</f>{cache}</c><c r="C1" s="1"><v>45292</v></c></row></sheetData><mergeCells><mergeCell ref="A2:C2"/></mergeCells><drawing r:id="rId1"/></worksheet>',
        "xl/worksheets/_rels/sheet1.xml.rels": f'<Relationships xmlns="{P}"><Relationship Id="rId1" Target="../drawings/drawing1.xml" Type="{R}/drawing"/><Relationship Id="rId2" Target="../comments/comment1.xml" Type="{R}/comments"/></Relationships>',
        "xl/comments/comment1.xml": f'<comments xmlns="{S}"><commentList><comment ref="A1"><text><t>核对审批时间</t></text></comment><comment ref="D2"><text><t>空白单元格批注</t></text></comment></commentList></comments>',
        "xl/drawings/drawing1.xml": f'<xdr:wsDr xmlns:xdr="{MODULE.NS["xdr"]}" xmlns:a="{MODULE.NS["a"]}" xmlns:r="{R}"><xdr:twoCellAnchor><xdr:from><xdr:col>2</xdr:col><xdr:row>3</xdr:row></xdr:from><xdr:pic><xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill></xdr:pic></xdr:twoCellAnchor></xdr:wsDr>',
        "xl/drawings/_rels/drawing1.xml.rels": f'<Relationships xmlns="{P}"><Relationship Id="rId1" Target="../media/image1.png" Type="{R}/image"/></Relationships>',
        "xl/media/image1.png": PNG,
    }
    with zipfile.ZipFile(path, "w") as archive:
        for key, value in members.items():
            archive.writestr(key, value)


class ExtractorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "input"
        self.inputs.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def run_extract(self, name="out"):
        return json.loads(MODULE.extract(self.inputs, self.root / name).read_text())

    def test_source_unchanged_hidden_cells_notes_cache_and_image_location(self):
        source = self.inputs / "synthetic.xlsx"
        package(source)
        before = source.read_bytes()
        manifest = self.run_extract()
        self.assertEqual(before, source.read_bytes())
        file = manifest["files"][0]
        self.assertEqual(file["status"], "extracted")
        self.assertEqual(file["sha256"], MODULE.sha256(source))
        sheet = file["sheets"][0]
        self.assertEqual(sheet["state"], "hidden")
        cells = {cell["ref"]: cell for cell in sheet["cells"]}
        self.assertEqual(cells["A1"]["value"], "审批记录")
        self.assertEqual(cells["A1"]["comment"], "核对审批时间")
        self.assertEqual(cells["D2"]["comment"], "空白单元格批注")
        self.assertEqual(cells["B1"]["formula"], "=1+1")
        self.assertEqual(cells["B1"]["cached_value"], 2)
        self.assertEqual(cells["C1"]["value"], "2024-01-01")
        self.assertEqual(sheet["merged_ranges"], ["A2:C2"])
        image = sheet["images"][0]
        self.assertEqual(image["anchor"]["cell"], "C4")
        self.assertEqual(image["status"], "requires_visual_review")
        self.assertEqual((self.root / "out" / image["media_path"]).read_bytes(), PNG)

    def test_missing_formula_cache_does_not_invent_a_value(self):
        package(self.inputs / "uncached.xlsx", formula_cache=False)
        file = self.run_extract()["files"][0]
        self.assertEqual(file["status"], "partial")
        self.assertTrue(any("no cached value" in warning for warning in file["warnings"]))
        formula = next(c for c in file["sheets"][0]["cells"] if c["ref"] == "B1")
        self.assertIsNone(formula["cached_value"])

    def test_corrupt_unsupported_and_empty_inputs_are_visible(self):
        (self.inputs / "broken.xlsx").write_bytes(b"not a spreadsheet")
        (self.inputs / "unsupported.xls").write_bytes(b"old format")
        statuses = {f["relative_path"]: f["status"] for f in self.run_extract()["files"]}
        self.assertEqual(statuses, {"broken.xlsx": "unreadable", "unsupported.xls": "unsupported"})
        for file in self.inputs.iterdir():
            file.unlink()
        self.assertTrue(self.run_extract("empty")["warnings"])

    def test_output_cannot_be_within_input_or_overwrite_results(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            MODULE.extract(self.inputs, self.inputs / "out")
        package(self.inputs / "file.xlsx")
        self.run_extract()
        with self.assertRaisesRegex(ValueError, "not overwritten"):
            self.run_extract()
        source = self.inputs / "file.xlsx"
        with self.assertRaises(ValueError):
            MODULE.extract(source, source)

    def test_standalone_image_and_source_ids_are_stable(self):
        (self.inputs / "screen.png").write_bytes(PNG)
        first, second = self.run_extract(), self.run_extract("other")
        self.assertEqual(first["files"][0]["file_id"], second["files"][0]["file_id"])
        self.assertEqual(first["files"][0]["images"][0]["anchor"]["kind"], "standalone")

    def test_unsafe_relationship_does_not_read_outside_package(self):
        path = self.inputs / "malformed.xlsx"
        package(path)
        with zipfile.ZipFile(path) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members["xl/_rels/workbook.xml.rels"] = f'<Relationships xmlns="{P}"><Relationship Id="rId1" Target="../../escape.xml" Type="{R}/worksheet"/></Relationships>'
        with zipfile.ZipFile(path, "w") as archive:
            for name, value in members.items():
                archive.writestr(name, value)
        file = self.run_extract()["files"][0]
        self.assertEqual(file["status"], "partial")
        self.assertTrue(any("Unsafe relationship" in warning for warning in file["warnings"]))

    def test_textbox_chart_and_external_links_are_not_silently_complete(self):
        path = self.inputs / "drawings.xlsx"
        package(path)
        with zipfile.ZipFile(path) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        drawing = members["xl/drawings/drawing1.xml"].decode()
        drawing = drawing.replace("</xdr:twoCellAnchor>", "<xdr:sp><xdr:txBody><a:p><a:r><a:t>审批结论</a:t></a:r></a:p></xdr:txBody></xdr:sp><xdr:graphicFrame/></xdr:twoCellAnchor>")
        members["xl/drawings/drawing1.xml"] = drawing
        members["xl/externalLinks/externalLink1.xml"] = "<externalLink/>"
        with zipfile.ZipFile(path, "w") as archive:
            for name, value in members.items():
                archive.writestr(name, value)
        file = self.run_extract()["files"][0]
        self.assertEqual(file["status"], "partial")
        self.assertEqual(len(file["sheets"][0]["images"]), 1)
        self.assertTrue(any("shape/textbox/chart" in warning for warning in file["warnings"]))
        self.assertTrue(any("External workbook links" in warning for warning in file["warnings"]))


if __name__ == "__main__":
    unittest.main()
