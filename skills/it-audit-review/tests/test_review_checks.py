"""Independent behavioral checks for fact comparison and review validation."""
import copy
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


FACTS = module("check_facts")
REVIEW = module("validate_review")


def case(kind="change_samples", rows=None):
    change = {"sample_id": "S1", "control_id": "C1", "change_type": "normal", "approved_at": "2026-09-01T09:00:00Z",
              "tested_at": "2026-09-01T10:00:00Z", "deployed_at": "2026-09-01T11:00:00Z", "evidence_ref": "embedded screenshot"}
    threshold = {"sample_id": "T1", "control_id": "C2", "amount": "1000", "expected_result": "pass", "actual_result": "pass", "evidence_ref": "embedded screenshot"}
    default = change if kind == "change_samples" else threshold
    rows = rows if rows is not None else [default]
    columns = {field: chr(65 + i) for i, field in enumerate(default)}
    cells = [{"ref": col + "1", "value": field} for field, col in columns.items()]
    for number, row in enumerate(rows, 2):
        cells.extend({"ref": columns[field] + str(number), "value": value} for field, value in row.items() if field in columns and value is not None)
    file = {"file_id": "F1", "relative_path": "book.xlsx", "absolute_path": "/synthetic-input/book.xlsx", "sha256": "a" * 64,
            "kind": "xlsx", "status": "extracted", "warnings": [], "pages": [], "images": [],
            "sheets": [{"name": "Samples", "state": "visible", "cells": cells, "images": [], "merged_ranges": []}]}
    policy = ({"source": "Synthetic policy", "standard_values": ["normal"], "emergency_values": ["emergency"],
               "require_preapproval": True, "require_predeployment_test": True} if kind == "change_samples" else
              {"source": "Synthetic policy", "operator": ">", "threshold": "1000", "when_true": "block", "when_false": "pass"})
    config = {"schema_version": "1.0", "tables": [{"file": "book.xlsx", "sheet": "Samples", "kind": kind, "header_row": 1,
              "data_start_row": 2, "data_end_row": len(rows) + 1, "columns": columns, "expected_headers": {field: field for field in columns}, "policy": policy}]}
    return {"schema_version": "1.0", "input_root": "/synthetic-input", "files": [file], "warnings": []}, config


def set_cell(manifest, config, field, value, row=2, **extra):
    ref = config["tables"][0]["columns"][field] + str(row)
    cells = manifest["files"][0]["sheets"][0]["cells"]
    target = next((cell for cell in cells if cell["ref"] == ref), None)
    if target is None:
        target = {"ref": ref}
        cells.append(target)
    target.update({"value": value}, **extra)


def valid_review(manifest):
    file = manifest["files"][0]
    return {"schema_version": "1.0", "scope": "Synthetic workpaper", "criteria": ["Synthetic policy; project methodology unconfirmed"], "limitations": [],
            "coverage": [{"file_id": file["file_id"], "status": "reviewed", "reason": "Completed stated checks",
                          "checks_performed": ["Mapped sample records read"], "checks_not_performed": [],
                          "images_reviewed": sorted(REVIEW.image_ids(file)), "images_not_reviewed": []}],
            "notes": [{"id": "N1", "rule_id": "TEST-1", "control_id": "C1", "category": "documentation_issue", "priority": "normal",
                       "title": "Synthetic issue", "observation": "Record requires clarification", "basis": "Synthetic policy",
                       "implication": "Conclusion requires verification", "requested_action": "Verify original evidence",
                       "locations": [{"file_id": "F1", "sheet": "Samples", "cell": "A2"}]}]}


class FactComparisonTests(unittest.TestCase):
    def test_normal_changes_only_flag_late_recorded_events(self):
        manifest, config = case()
        clean = FACTS.run(manifest, config)
        self.assertEqual(len(clean["checks_performed"]), 2)
        self.assertFalse(clean["candidate_notes"])
        set_cell(manifest, config, "approved_at", "2026-09-01T12:00:00Z")
        late = FACTS.run(manifest, config)
        self.assertEqual([n["rule_id"] for n in late["candidate_notes"]], ["FACT-001"])
        self.assertEqual(late["candidate_notes"][0]["category"], "potential_control_exception")

    def test_emergency_and_unknown_types_do_not_inherit_normal_policy(self):
        for kind in ("emergency", "unclassified"):
            with self.subTest(kind=kind):
                manifest, config = case()
                set_cell(manifest, config, "change_type", kind)
                set_cell(manifest, config, "approved_at", "2026-09-03T12:00:00Z")
                result = FACTS.run(manifest, config)
                self.assertFalse(result["checks_performed"])
                self.assertTrue(result["checks_skipped"])
                self.assertFalse([n for n in result["candidate_notes"] if n["category"] == "potential_control_exception"])

    def test_same_day_date_only_equal_times_and_mixed_zones_are_not_ordered(self):
        for approval in ("2026-09-01", "2026-09-01T11:00:00Z", "2026-09-01T12:00:00"):
            with self.subTest(approval=approval):
                manifest, config = case()
                set_cell(manifest, config, "approved_at", approval)
                result = FACTS.run(manifest, config)
                self.assertFalse([c for c in result["checks_performed"] if c["rule_id"] == "FACT-001"])
                self.assertTrue(result["checks_skipped"])
                self.assertFalse([n for n in result["candidate_notes"] if n["rule_id"] == "FACT-001"])

    def test_known_timezone_offsets_are_compared_as_instants(self):
        self.assertEqual(FACTS.compare_time("2026-09-01T17:00:00+08:00", "2026-09-01T10:00:00Z")[0], -1)

    def test_threshold_1000_boundary_uses_configured_strict_operator(self):
        rows = [{"sample_id": "T" + str(i), "control_id": "C2", "amount": value, "expected_result": expected,
                 "actual_result": expected, "evidence_ref": "screenshot"}
                for i, (value, expected) in enumerate((("999.99", "pass"), ("1000", "pass"), ("1000.01", "block")))]
        manifest, config = case("threshold_cases", rows)
        result = FACTS.run(manifest, config)
        self.assertEqual(len(result["checks_performed"]), 3)
        self.assertFalse(result["candidate_notes"])
        set_cell(manifest, config, "actual_result", "block", row=3)
        notes = FACTS.run(manifest, config)["candidate_notes"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["sample_id"], "T1")
        self.assertEqual(notes[0]["category"], "potential_control_exception")

    def test_duplicate_and_missing_ids_are_record_issues(self):
        manifest, config = case()
        cells = manifest["files"][0]["sheets"][0]["cells"]
        cells.extend({**cell, "ref": cell["ref"][:-1] + "3"} for cell in list(cells) if cell["ref"].endswith("2"))
        config["tables"][0]["data_end_row"] = 3
        set_cell(manifest, config, "control_id", None, row=3)
        rules = {n["rule_id"] for n in FACTS.run(manifest, config)["candidate_notes"]}
        self.assertTrue({"FACT-003", "FACT-004"}.issubset(rules))

    def test_header_mismatch_and_invalid_ranges_cannot_claim_checks(self):
        manifest, config = case()
        manifest["files"][0]["sheets"][0]["cells"][0]["value"] = "Wrong header"
        result = FACTS.run(manifest, config)
        self.assertFalse(result["checks_performed"])
        self.assertTrue(result["checks_skipped"])
        for start, end in ((None, 3), (1, 3), (4, 2), (True, 3), (2, 1048577), (2.0, 3)):
            with self.subTest(start=start, end=end):
                manifest, config = case()
                config["tables"][0].update(data_start_row=start, data_end_row=end)
                result = FACTS.run(manifest, config)
                self.assertFalse(result["checks_performed"])
                self.assertTrue(result["checks_skipped"])

    def test_outside_range_footnote_is_not_treated_as_a_sample(self):
        manifest, config = case()
        manifest["files"][0]["sheets"][0]["cells"].append({"ref": "A6", "value": "Screenshot explanation only"})
        result = FACTS.run(manifest, config)
        self.assertEqual(len(result["checks_performed"]), 2)
        self.assertFalse(result["candidate_notes"])
        self.assertFalse(any(c.get("row") == 6 for c in result["manual_checks"]))

    def test_formula_fields_and_incomplete_extraction_are_skipped(self):
        manifest, config = case()
        set_cell(manifest, config, "approved_at", None, formula="=NOW()", cached_value=None)
        result = FACTS.run(manifest, config)
        self.assertFalse(result["checks_performed"])
        self.assertTrue(result["checks_skipped"])
        manifest, config = case()
        manifest["files"][0]["status"] = "partial"
        result = FACTS.run(manifest, config)
        self.assertFalse(result["checks_performed"])
        self.assertTrue(result["checks_skipped"])

    def test_missing_policy_and_invalid_amount_do_not_create_exceptions(self):
        manifest, config = case()
        config["tables"][0]["policy"].pop("source")
        result = FACTS.run(manifest, config)
        self.assertFalse(result["checks_performed"])
        self.assertTrue(result["checks_skipped"])
        for value in (True, "NaN", "Infinity", "1,000", None):
            with self.subTest(value=value):
                manifest, config = case("threshold_cases")
                set_cell(manifest, config, "amount", value)
                result = FACTS.run(manifest, config)
                self.assertFalse(result["checks_performed"])
                self.assertTrue(result["candidate_notes"])
                self.assertFalse([n for n in result["candidate_notes"] if n["category"] == "potential_control_exception"])

    def test_attachment_name_match_does_not_claim_content_review(self):
        manifest, config = case()
        set_cell(manifest, config, "evidence_ref", "missing.pdf")
        result = FACTS.run(manifest, config)
        self.assertTrue(any(n["rule_id"] == "FACT-005" for n in result["candidate_notes"]))
        attached = copy.deepcopy(manifest["files"][0])
        attached.update(file_id="F2", relative_path="evidence/missing.pdf", kind="pdf", status="unreadable", sheets=[])
        manifest["files"].append(attached)
        result = FACTS.run(manifest, config)
        self.assertTrue(any(m.get("file_id") == "F2" for m in result["manual_checks"]))


class ReviewValidationTests(unittest.TestCase):
    def test_valid_review_accepts_real_cell_page_and_image_locations(self):
        manifest, _ = case()
        manifest["files"][0]["pages"] = [{"page": 1, "text": "test"}]
        manifest["files"][0]["images"] = [{"image_id": "IMG1"}]
        review = valid_review(manifest)
        review["notes"][0]["locations"].extend([{"file_id": "F1", "page": 1}, {"file_id": "F1", "image_id": "IMG1"}])
        self.assertEqual(REVIEW.validate(manifest, review), [])

    def test_page_number_must_be_an_integer_not_a_bool_or_float(self):
        manifest, _ = case()
        manifest["files"][0]["pages"] = [{"page": 1, "text": "test"}]
        for number in (True, 1.0, "1"):
            with self.subTest(number=number):
                review = valid_review(manifest)
                review["notes"][0]["locations"] = [{"file_id": "F1", "page": number}]
                self.assertTrue(REVIEW.validate(manifest, review))

    def test_missing_locations_and_nonexistent_references_are_rejected(self):
        manifest, _ = case()
        for location in (None, {"file_id": "unknown"}, {"file_id": "F1", "sheet": "Samples", "cell": "ZZ999"},
                         {"file_id": "F1", "page": 9}, {"file_id": "F1", "page": True}, {"file_id": "F1", "image_id": "fake"}):
            with self.subTest(location=location):
                review = valid_review(manifest)
                review["notes"][0]["locations"] = [] if location is None else [location]
                self.assertTrue(REVIEW.validate(manifest, review))

    def test_mixed_location_kinds_and_duplicate_note_ids_are_rejected(self):
        manifest, _ = case()
        review = valid_review(manifest)
        review["notes"][0]["locations"][0]["page"] = 1
        self.assertTrue(REVIEW.validate(manifest, review))
        review = valid_review(manifest)
        review["notes"].append(copy.deepcopy(review["notes"][0]))
        self.assertTrue(REVIEW.validate(manifest, review))

    def test_missing_duplicate_coverage_and_unsupported_enum_are_rejected(self):
        manifest, _ = case()
        for change in ("missing", "duplicate", "invalid"):
            with self.subTest(change=change):
                review = valid_review(manifest)
                if change == "missing":
                    review["coverage"] = []
                elif change == "duplicate":
                    review["coverage"] *= 2
                else:
                    review["coverage"][0]["status"] = "pass"
                self.assertTrue(REVIEW.validate(manifest, review))

    def test_image_coverage_must_be_exact_and_unseen_images_prevent_reviewed(self):
        manifest, _ = case()
        manifest["files"][0]["images"] = [{"image_id": "IMG1"}, {"image_id": "IMG2"}]
        review = valid_review(manifest)
        review["coverage"][0]["images_reviewed"] = ["IMG1"]
        self.assertTrue(REVIEW.validate(manifest, review))
        review["coverage"][0]["images_not_reviewed"] = ["IMG2"]
        self.assertTrue(REVIEW.validate(manifest, review))
        review["coverage"][0].update(status="partial", checks_not_performed=["IMG2 not visually inspected"])
        review["limitations"] = ["IMG2 not reviewed"]
        self.assertEqual(REVIEW.validate(manifest, review), [])
        review["coverage"][0]["images_reviewed"] = ["IMG1", "IMG1"]
        self.assertTrue(REVIEW.validate(manifest, review))

    def test_unreadable_input_incomplete_checks_and_no_checks_prevent_reviewed(self):
        for status in ("unreadable", "partial", "unsupported"):
            with self.subTest(status=status):
                manifest, _ = case()
                manifest["files"][0]["status"] = status
                self.assertTrue(REVIEW.validate(manifest, valid_review(manifest)))
        manifest, _ = case()
        for field, value in (("checks_performed", []), ("checks_not_performed", ["Evidence not inspected"])):
            review = valid_review(manifest)
            review["coverage"][0][field] = value
            self.assertTrue(REVIEW.validate(manifest, review))

    def test_unreviewed_coverage_requires_limitations_and_conflicts_with_done(self):
        manifest, _ = case()
        review = valid_review(manifest)
        review["coverage"][0].update(status="not_reviewed", checks_performed=[])
        self.assertTrue(REVIEW.validate(manifest, review))
        review["limitations"] = ["Input unavailable"]
        self.assertEqual(REVIEW.validate(manifest, review), [])
        review["coverage"][0]["checks_performed"] = ["Read content"]
        self.assertTrue(REVIEW.validate(manifest, review))

    def test_empty_input_requires_explicit_limitation(self):
        manifest, _ = case()
        review = valid_review(manifest)
        manifest["files"], review["coverage"], review["notes"] = [], [], []
        self.assertTrue(REVIEW.validate(manifest, review))
        review["limitations"] = ["No input files; no review performed"]
        self.assertEqual(REVIEW.validate(manifest, review), [])

    def test_render_nonempty_coverage_and_notes_preserves_evidence_location(self):
        manifest, _ = case()
        review = valid_review(manifest)
        self.assertEqual(REVIEW.validate(manifest, review), [])
        report = REVIEW.render(manifest, review)
        self.assertIn("book.xlsx / Samples!A2", report)
        self.assertIn("Record requires clarification", report)
        self.assertIn("Mapped sample records read", report)
        self.assertIn("a" * 64, report)

    def test_source_hash_recheck_rejects_changed_or_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.xlsx"
            source.write_bytes(b"original immutable source")
            manifest, _ = case()
            manifest["files"][0].update(absolute_path=str(source), sha256=REVIEW.sha256(source))
            self.assertEqual(REVIEW.verify_sources(manifest), [])
            self.assertEqual(source.read_bytes(), b"original immutable source")
            source.write_bytes(b"changed by another process")
            self.assertTrue(any("changed" in e for e in REVIEW.verify_sources(manifest)))
            source.unlink()
            self.assertTrue(any("cannot be rechecked" in e for e in REVIEW.verify_sources(manifest)))

    def test_output_protection_rejects_sources_inputs_and_existing_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            source.mkdir()
            book = source / "book.xlsx"
            book.write_text("immutable source")
            manifest, _ = case()
            manifest["input_root"] = str(source)
            manifest["files"][0]["absolute_path"] = str(book)
            protected = root / "review.json"
            protected.write_text("immutable review")
            for destination in (book, source / "report.md", protected):
                with self.subTest(destination=destination):
                    with self.assertRaises(ValueError):
                        FACTS.output_path(destination, manifest, (protected,))
            allowed = root / "new-output" / "report.md"
            self.assertEqual(FACTS.output_path(allowed, manifest), allowed.resolve())
            self.assertFalse(allowed.exists())
            self.assertEqual(book.read_text(), "immutable source")
            self.assertEqual(protected.read_text(), "immutable review")


if __name__ == "__main__":
    unittest.main()
