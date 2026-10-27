import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import eval_seeded_bugs as evaluation
from reviewed import prompts, reviewer, validation


def finding_for(change, line=None):
	return {
		"file": change.file, "line": line, "title": change.func,
		"evidence": change.desc, "explanation": change.desc,
		"recommendation": "Restore the original behavior.",
		"severity": "medium", "confidence": "high",
	}


class SeededEvaluationTests(unittest.TestCase):
	def test_batches_cover_each_change_once_and_respect_finding_limit(self):
		batches = evaluation.review_batches()
		changes = [change for batch in batches for change in batch]
		self.assertCountEqual(changes, evaluation.BUGS + evaluation.BENIGN)
		for batch in batches:
			self.assertEqual(len({change.file for change in batch}), 1)
			self.assertLessEqual(sum(not change.benign for change in batch), 5)

	def test_scoring_uses_only_active_changes_and_batch_line_numbers(self):
		for changes in evaluation.review_batches():
			modified = evaluation.apply_changes(changes)
			findings = []
			for change in changes:
				if not change.benign:
					start, end = evaluation.function_span(modified[change.file], change.func)
					findings.append(finding_for(change, end))
			result = evaluation.classify(findings, modified, changes)
			self.assertCountEqual(result["caught"], [c.id for c in changes if not c.benign])
			self.assertEqual(result["false_positives"], [])
		changes = evaluation.review_batches()[0]
		inactive = next(c for c in evaluation.BUGS if c.file == changes[0].file and c not in changes)
		result = evaluation.classify([finding_for(inactive)], evaluation.apply_changes(changes), changes)
		self.assertEqual(result["caught"], {})
		self.assertEqual(len(result["false_positives"]), 1)

	def test_failed_reviews_continue_and_do_not_count_as_misses(self):
		batches = evaluation.review_batches()
		responses = [reviewer.ReviewValidationError("invalid added line"), RuntimeError("provider failed")]
		responses.extend(
			{"findings": [finding_for(next(c for c in changes if not c.benign))]}
			for changes in batches[2:]
		)
		with (
			patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
			patch("openai.OpenAI") as client,
			patch.object(reviewer, "call_openai", side_effect=responses) as call,
			redirect_stderr(io.StringIO()),
		):
			result = evaluation.run_eval("test-model")
		self.assertEqual(call.call_count, len(batches))
		client.return_value.close.assert_called_once()
		summary = evaluation.summarize(result)
		unreviewed = [c.id for batch in batches[:2] for c in batch if not c.benign]
		self.assertCountEqual(summary["unreviewed"], unreviewed)
		self.assertTrue(set(unreviewed).isdisjoint(summary["missed"]))
		self.assertEqual(summary["failed_batches"], 2)
		self.assertEqual(summary["validation_failures"], 1)
		self.assertEqual(summary["reviewed_bugs"], len(evaluation.BUGS) - len(unreviewed))
		self.assertEqual(summary["recall"], round(summary["bugs_caught"] / summary["reviewed_bugs"], 3))
		self.assertEqual(len(result["findings"]), len(batches) - 2)
		output = io.StringIO()
		with redirect_stdout(output):
			evaluation.print_report(result, summary)
		self.assertIn("unreviewed", output.getvalue())
		self.assertIn("invalid added line", output.getvalue())

	def test_all_failed_reviews_have_no_recall_or_precision(self):
		result = {
			"model": "test-model", "caught": {}, "findings": [], "false_positives": [],
			"reviewed_bug_ids": [], "failures": [{"kind": "validation"}],
		}
		summary = evaluation.summarize(result)
		self.assertIsNone(summary["recall"])
		self.assertIsNone(summary["precision"])
		self.assertEqual(summary["missed"], [])
		self.assertEqual(summary["coverage"], 0)

	def test_invalid_line_error_includes_file_line_and_valid_lines(self):
		diff = "diff --git a/example.py b/example.py\n--- a/example.py\n+++ b/example.py\n@@ -1 +1,2 @@\n context\n+added\n"
		finding = {**finding_for(evaluation.BUGS[0], 1), "file": "example.py"}
		review = {"summary": "Review", "risk": "medium", "findings": [finding]}
		with self.assertRaises(ValueError) as error:
			validation.validate_review(review, diff)
		self.assertIn("example.py:1", str(error.exception))
		self.assertIn("Valid added lines: [2]", str(error.exception))
		client = Mock()
		client.responses.create.return_value = SimpleNamespace(
			status="completed", output_text=json.dumps(review), usage=None,
		)
		with patch.object(reviewer, "scan_diff_for_secrets"), redirect_stdout(io.StringIO()):
			with self.assertRaises(reviewer.ReviewValidationError) as rejected:
				reviewer.call_openai(client, "test-model", diff)
		self.assertEqual(rejected.exception.review, review)
		self.assertEqual(rejected.exception.raw_response, json.dumps(review))
		finding["line"] = None
		self.assertEqual(validation.validate_review(review, diff), review)

	def test_request_supplies_exact_allowed_new_file_line_numbers(self):
		diff = (
			"diff --git a/example.py b/example.py\n--- a/example.py\n+++ b/example.py\n"
			"@@ -10,2 +10,3 @@\n context\n-old\n+new\n+extra\n"
			"@@ -30 +31 @@\n-before\n+after\n"
		)
		request = reviewer.build_review_request("test-model", diff)
		index = request["input"].split("<added_line_numbers>\n", 1)[1].split("\n</added_line_numbers>", 1)[0]
		self.assertEqual(json.loads(index), {"example.py": [11, 12, 31]})
		self.assertIn("<diff>\n" + diff + "\n</diff>", request["input"])

	def test_rejected_review_retains_evidence_and_has_separate_detection_metrics(self):
		changes = evaluation.review_batches()[0]
		bugs = [change for change in changes if not change.benign]
		review = {
			"summary": "Two defects", "risk": "high",
			"findings": [finding_for(bugs[0], 999), finding_for(bugs[1])],
		}
		error = reviewer.ReviewValidationError("invalid citation", review=review, raw_response=json.dumps(review))
		with (
			patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
			patch("openai.OpenAI"),
			patch.object(evaluation, "review_batches", return_value=[changes]),
			patch.object(reviewer, "call_openai", side_effect=error),
			redirect_stderr(io.StringIO()),
		):
			result = evaluation.run_eval("test-model")
		self.assertEqual(result["findings"], [])
		self.assertEqual(result["responses"][0]["review"], review)
		self.assertIn("diff --git", result["responses"][0]["diff"])
		self.assertIn("def verify_password", result["responses"][0]["source"])
		self.assertEqual(result["failures"][0]["raw_response"], json.dumps(review))
		summary = evaluation.summarize(result)
		self.assertEqual(summary["reviewed_bugs"], 0)
		self.assertEqual(summary["citation_validity"]["total_findings"], 2)
		self.assertEqual(summary["citation_validity"]["invalid_findings"], 1)
		self.assertEqual(summary["citation_validity"]["valid_rate"], 0.5)
		self.assertEqual(summary["citation_validity"]["numeric_valid_rate"], 0)
		self.assertEqual(summary["detection"]["reviewed_bugs"], len(bugs))
		self.assertEqual(summary["detection"]["provisional_bugs_caught"], 2)
		self.assertIsNone(summary["detection"]["recall"])
		self.assertIsNone(summary["detection"]["precision"])
		self.assertEqual(result["adjudications"][0]["finding"]["line"], 999)

		result["adjudications"][0].update(verdict="confirmed_bug", bug_id=bugs[0].id)
		result["adjudications"][1].update(verdict="false_positive")
		summary = evaluation.summarize(result)
		self.assertTrue(summary["detection"]["manual_complete"])
		self.assertEqual(summary["detection"]["confirmed_bugs_caught"], 1)
		self.assertEqual(summary["detection"]["confirmed_false_positives"], 1)
		self.assertEqual(summary["detection"]["precision"], 0.5)
		self.assertEqual(summary["detection"]["recall"], round(1 / len(bugs), 3))

		with tempfile.TemporaryDirectory() as directory:
			input_path = Path(directory) / "eval.json"
			output_path = Path(directory) / "adjudicated.json"
			input_path.write_text(json.dumps(result), encoding="utf-8")
			with (
				patch("sys.argv", ["eval", "--rescore", str(input_path), "--output", str(output_path)]),
				patch.dict(os.environ, {}, clear=True),
				patch.object(evaluation, "run_eval") as run,
				redirect_stdout(io.StringIO()),
			):
				self.assertEqual(evaluation.main(), 1)
			run.assert_not_called()
			self.assertEqual(json.loads(output_path.read_text(encoding="utf-8"))["summary"], summary)

		result["adjudications"][0]["bug_id"] = "B24"
		with self.assertRaisesRegex(ValueError, "seed from this batch"):
			evaluation.summarize(result)
		result["adjudications"].pop()
		with self.assertRaisesRegex(ValueError, "retain every original finding"):
			evaluation.summarize(result)

	def test_structure_errors_preserve_review_without_creating_detection_scores(self):
		changes = evaluation.review_batches()[0]
		result = {"responses": [], "adjudications": [], "detection_reviewed_bug_ids": []}
		batch = {"id": 1, "file": changes[0].file, "bug_ids": [c.id for c in changes if not c.benign]}
		evaluation.record_response(result, batch, {"findings": []}, "", evaluation.apply_changes(changes), changes)
		self.assertEqual(result["responses"][0]["review"], {"findings": []})
		self.assertIn("structure_error", result["responses"][0])
		self.assertEqual(result["adjudications"], [])
		self.assertEqual(result["detection_reviewed_bug_ids"], [])

	def test_prompt_requires_new_file_added_lines_or_null(self):
		instructions = prompts.get_review_instructions("all")
		self.assertIn("new-file line number", instructions)
		self.assertIn("Never cite unchanged context or deleted lines", instructions)
		self.assertIn("deletion-only or file-level findings, use null", instructions)


if __name__ == "__main__":
	unittest.main()