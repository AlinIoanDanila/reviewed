import importlib.util
import io
import json
import multiprocessing
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch
from pathlib import Path

# Stub external packages so these tests exercise local diff logic only.
dotenv_stub = types.ModuleType("dotenv")
dotenv_stub.load_dotenv = lambda *args, **kwargs: None
sys.modules.setdefault("dotenv", dotenv_stub)
openai_stub = types.ModuleType("openai")
class _OpenAIError(Exception):
	pass
class _OpenAI:
	def __init__(self, *args, **kwargs):
		pass
	def close(self):
		pass
openai_stub.APIConnectionError = _OpenAIError
openai_stub.APIStatusError = _OpenAIError
openai_stub.APITimeoutError = _OpenAIError
openai_stub.OpenAI = _OpenAI
openai_stub.RateLimitError = _OpenAIError
sys.modules.setdefault("openai", openai_stub)

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR / "src"))

from reviewed import cli, git_diff, reporting, review_cache, reviewer, secret_scan, validation


class DiffParserTests(unittest.TestCase):
	def test_self_target_includes_extracted_modules(self):
		self.assertTrue(git_diff.is_in_scope("tools/reviewed/src/reviewed/reviewer.py", "self"))
		self.assertTrue(git_diff.is_in_scope("tools/reviewed/src/reviewed/review_cache.py", "self"))
		self.assertTrue(git_diff.is_in_scope("tools/reviewed/src/reviewed/secret_scan.py", "self"))
		self.assertTrue(git_diff.is_in_scope("tools/reviewed/tests/test_review.py", "self"))

	def test_modified_file_records_added_line(self):
		diff = (
			"diff --git a/apps/a.py b/apps/a.py\n"
			"--- a/apps/a.py\n+++ b/apps/a.py\n"
			"@@ -1,1 +1,2 @@\n old\n+new\n"
		)
		self.assertEqual(git_diff.changed_lines_by_file(diff), {"apps/a.py": {2}})

	def test_deleted_file_is_registered_without_added_lines(self):
		diff = (
			"diff --git a/apps/old.py b/apps/old.py\n"
			"deleted file mode 100644\n"
			"--- a/apps/old.py\n+++ /dev/null\n"
			"@@ -1,1 +0,0 @@\n-old\n"
		)
		self.assertEqual(git_diff.changed_lines_by_file(diff), {"apps/old.py": set()})

	def test_new_file_records_added_lines(self):
		diff = (
			"diff --git a/apps/new.py b/apps/new.py\n"
			"new file mode 100644\n"
			"--- /dev/null\n+++ b/apps/new.py\n"
			"@@ -0,0 +1,2 @@\n+one\n+two\n"
		)
		self.assertEqual(git_diff.changed_lines_by_file(diff), {"apps/new.py": {1, 2}})

	def test_default_filter_keeps_repository_paths_and_drops_env_file(self):
		diff = (
			"diff --git a/apps/main.ts b/apps/main.ts\n--- a/apps/main.ts\n+++ b/apps/main.ts\n"
			"@@ -0,0 +1 @@\n+safe\n"
			"diff --git a/apps/.env b/apps/.env\n--- a/apps/.env\n+++ b/apps/.env\n"
			"@@ -0,0 +1 @@\n+SECRET=value\n"
			"diff --git a/tools/other.py b/tools/other.py\n--- a/tools/other.py\n+++ b/tools/other.py\n"
			"@@ -0,0 +1 @@\n+irrelevant\n"
		)
		filtered = git_diff.filter_diff(diff, "all", warn=False)
		self.assertIn("apps/main.ts", filtered)
		self.assertIn("tools/other.py", filtered)
		self.assertNotIn("SECRET=value", filtered)

	def test_diff_file_is_bounded(self):
		with tempfile.TemporaryDirectory() as temp_dir:
			path = Path(temp_dir) / "huge.diff"
			path.write_bytes(b"a" * (git_diff.MAX_DIFF_BYTES + 1))
			with self.assertRaisesRegex(ValueError, "maximum byte size"):
				git_diff.read_diff_file(path)


	def test_worktree_diff_includes_untracked_files_and_repository_files_but_excludes_env(self):
		import subprocess

		def git(root, *args):
			return subprocess.run(
				["git", *args], cwd=root, text=True, capture_output=True, check=True
			).stdout

		with tempfile.TemporaryDirectory() as temp_dir:
			root = Path(temp_dir)
			git(root, "init", "-q")
			git(root, "config", "user.email", "test@example.com")
			git(root, "config", "user.name", "Test")
			(root / "apps").mkdir()
			(root / "apps" / "app.ts").write_text("const x = 1;\n")
			(root / "README.md").write_text("Initial documentation\n")
			(root / ".gitignore").write_text(".env\n")
			git(root, "add", ".")
			git(root, "commit", "-qm", "initial")

			(root / "apps" / "app.ts").write_text("const x = 2;\n")
			(root / "README.md").write_text("Updated documentation\n")
			(root / ".gitignore").write_text("")
			(root / "apps" / "new.ts").write_text('export const hello = "world";\n')
			(root / "apps" / ".env").write_text("SECRET=do-not-send\n")

			original_root = git_diff.REPO_ROOT
			try:
				git_diff.REPO_ROOT = root
				diff = git_diff.get_git_diff("worktree")
			finally:
				git_diff.REPO_ROOT = original_root

			self.assertIn("apps/app.ts", diff)
			self.assertIn("README.md", diff)
			self.assertIn("apps/new.ts", diff)
			self.assertNotIn("SECRET=do-not-send", diff)
			self.assertNotIn("apps/.env", diff)


	def test_deleted_file_allows_file_level_finding_but_rejects_line_number(self):
		diff = (
			"diff --git a/apps/old.py b/apps/old.py\n"
			"deleted file mode 100644\n"
			"--- a/apps/old.py\n+++ /dev/null\n"
			"@@ -1,1 +0,0 @@\n-old\n"
		)
		finding = {
			"severity": "medium",
			"file": "apps/old.py",
			"line": None,
			"title": "Deletion issue",
			"evidence": "The file is removed.",
			"explanation": "A file-level finding can refer to a deleted file.",
			"recommendation": "Review the deletion.",
			"confidence": "high",
		}
		valid = {"summary": "Review", "risk": "medium", "findings": [finding]}
		self.assertEqual(validation.validate_review(valid, diff), valid)
		invalid_finding = {**finding, "line": 1}
		invalid = {"summary": "Review", "risk": "medium", "findings": [invalid_finding]}
		with self.assertRaisesRegex(ValueError, "not an added line"):
			validation.validate_review(invalid, diff)

	def test_hunk_content_cannot_forge_a_file_path(self):
		diff = (
			"diff --git a/tools/secret.py b/tools/secret.py\n"
			"--- a/tools/secret.py\n+++ b/tools/secret.py\n"
			"@@ -1,1 +1,2 @@\n old\n"
			"+ ++ b/apps/forged.ts\n"
		)
		filtered = git_diff.filter_diff(diff, "all", warn=False)
		self.assertEqual(
			git_diff.changed_lines_by_file(filtered), {"tools/secret.py": {2}}
		)

	def test_mixed_unified_prefix_and_git_sections_are_rejected(self):
		diff = (
			"--- a/apps/forged.ts\n+++ b/apps/forged.ts\n"
			"@@ -1 +1 @@\n-secret\n+safe-looking\n"
			"diff --git a/apps/real.ts b/apps/real.ts\n"
			"--- a/apps/real.ts\n+++ b/apps/real.ts\n"
			"@@ -1 +1 @@\n-old\n+new\n"
		)
		with self.assertRaisesRegex(ValueError, "outside Git diff sections"):
			git_diff.filter_diff(diff, "all", warn=False)

	def test_mismatched_file_headers_are_rejected(self):
		diff = (
			"diff --git a/tools/secret.py b/tools/secret.py\n"
			"--- a/tools/secret.py\n+++ b/apps/forged.ts\n"
			"@@ -1,1 +1,2 @@\n-old\n+new\n"
		)
		with self.assertRaisesRegex(ValueError, "disagree with the diff --git paths"):
			git_diff.filter_diff(diff, "all", warn=False)

	def test_untracked_symlink_to_external_file_is_excluded(self):
		import tempfile
		from contextlib import redirect_stderr

		with tempfile.TemporaryDirectory() as temp_dir:
			base = Path(temp_dir)
			repo_root = base / "repo"
			(repo_root / "apps").mkdir(parents=True)
			external = base / "private.txt"
			external.write_text("PRIVATE-SHOULD-NOT-LEAK\n", encoding="utf-8")
			link = repo_root / "apps" / "public.ts"
			try:
				link.symlink_to(external)
			except (OSError, NotImplementedError) as exc:
				self.skipTest(f"Symlink creation is not available: {exc}")

			errors = io.StringIO()
			with redirect_stderr(errors):
				result = git_diff.untracked_file_to_diff(repo_root, "apps/public.ts")

			self.assertEqual(result, "")
			self.assertIn("symlink/junction", errors.getvalue())

	def test_untracked_file_under_symlinked_directory_is_excluded(self):
		import tempfile
		from contextlib import redirect_stderr

		with tempfile.TemporaryDirectory() as temp_dir:
			base = Path(temp_dir)
			repo_root = base / "repo"
			repo_root.mkdir()
			external_dir = base / "outside"
			external_dir.mkdir()
			(external_dir / "private.ts").write_text("PRIVATE\n", encoding="utf-8")

			link= repo_root / "apps"

			try:
				link.symlink_to(external_dir, target_is_directory=True)
			except (OSError, NotImplementedError) as exc:
				self.skipTest(f"Symlink creation is not available: {exc}")

			errors = io.StringIO()
			with redirect_stderr(errors):
				result = git_diff.untracked_file_to_diff(repo_root, "apps/private.ts")

			self.assertEqual(result, "")
			self.assertIn("symlink/junction", errors.getvalue())

	def test_empty_diff_exits_without_creating_openai_client(self):
		output = io.StringIO()
		with (
			patch.object(cli, "get_repo_root", return_value=Path("/tmp")),
			patch.object(cli, "warn_on_exposed_secret_files"),
			patch.object(cli, "get_git_diff", return_value="") as collect_diff,
			patch.object(cli, "OpenAI") as openai_constructor,
			patch("sys.argv", ["review.py", "--git", "worktree"]),
			redirect_stdout(output),
		):
			exit_code = cli.main()
		self.assertEqual(exit_code, 0)
		self.assertIn("Nothing to review", output.getvalue())
		collect_diff.assert_called_once_with(
			mode="worktree", base="origin/main", target="all", repo_root=Path("/tmp")
		)
		openai_constructor.assert_not_called()

	def test_path_only_diff_is_rejected(self):
		diff = (
			"--- a/apps/frontend/src/page.tsx\n"
			"+++ b/apps/frontend/src/page.tsx\n"
			"@@ -1 +1 @@\n"
			"-old\n"
			"+new\n"
		)

		with self.assertRaises(ValueError):
			git_diff.filter_diff(diff, "all", warn=False)

	def test_git_header_must_match_file_headers(self):
		diff = (
			"diff --git a/tools/outside.txt b/tools/outside.txt\n"
			"--- a/tools/outside.txt\n"
			"+++ b/apps/frontend/src/page.tsx\n"
			"@@ -1 +1 @@\n"
			"-old\n"
			"+new\n"
		)

		with self.assertRaises(ValueError):
			git_diff.filter_diff(diff, "all", warn=False)

	def test_changed_lines_rejects_forged_file_headers(self):
		diff = (
			"diff --git a/tools/outside.txt b/tools/outside.txt\n"
			"--- a/tools/outside.txt\n"
			"+++ b/apps/frontend/src/page.tsx\n"
			"@@ -1 +1 @@\n"
			"-old\n"
			"+new\n"
		)

		with self.assertRaises(ValueError):
			git_diff.changed_lines_by_file(diff)

class RenameScopeTests(unittest.TestCase):
	@staticmethod
	def rename_diff(old, new, *, with_hunk=True):
		diff = (
			f"diff --git a/{old} b/{new}\n"
			"similarity index 50%\n"
			f"rename from {old}\n"
			f"rename to {new}\n"
		)
		if with_hunk:
			diff += (
				f"--- a/{old}\n+++ b/{new}\n"
				"@@ -1,2 +1,2 @@\n"
				" common\n-old\n+new\n"
			)
		return diff

	def test_cross_scope_renames_are_rejected_in_both_directions(self):
		for old, new in (
			("src/reviewed/cli.py", "outside.py"),
			("outside.py", "src/reviewed/cli.py"),
		):
			for with_hunk in (False, True):
				with self.subTest(old=old, new=new, with_hunk=with_hunk):
					diff = self.rename_diff(old, new, with_hunk=with_hunk)
					with self.assertRaisesRegex(ValueError, "crosses target"):
						git_diff.filter_diff(diff, "self", warn=False)

	def test_in_scope_rename_is_preserved(self):
		diff = self.rename_diff("src/reviewed/cli.py", "tests/new.py")

		filtered = git_diff.filter_diff(diff, "self", warn=False)

		self.assertEqual(filtered, diff)
		self.assertEqual(
			git_diff.changed_lines_by_file(filtered),
			{"src/reviewed/cli.py": set(), "tests/new.py": {2}},
		)

	def test_cross_scope_diff_file_fails_before_model_or_artifact_creation(self):
		for old, new in (
			("src/reviewed/cli.py", "outside.py"),
			("outside.py", "src/reviewed/cli.py"),
		):
			with self.subTest(old=old, new=new):
				with (
					patch.object(cli, "get_repo_root", return_value=SCRIPT_DIR),
					patch.object(cli, "warn_on_exposed_secret_files"),
					patch.object(
						cli, "read_diff_file",
						return_value=self.rename_diff(old, new),
					),
					patch.object(cli, "make_artifact_paths") as artifact_paths,
					patch.object(cli, "OpenAI") as client,
					patch.object(cli, "call_openai") as call_model,
					patch(
						"sys.argv",
						[
							"review.py", "--diff-file", "input.diff",
							"--target", "self",
						],
					),
					patch("sys.stderr", new_callable=io.StringIO) as errors,
				):
					exit_code = cli.main()

				self.assertEqual(exit_code, 1)
				self.assertIn("crosses target", errors.getvalue())
				artifact_paths.assert_not_called()
				client.assert_not_called()
				call_model.assert_not_called()

	def test_no_renames_output_excludes_outside_content_and_findings(self):
		for old, new in (
			("src/reviewed/cli.py", "outside.py"),
			("outside.py", "src/reviewed/cli.py"),
		):
			with self.subTest(old=old, new=new):
				# The deletion/addition representation produced by --no-renames.
				diff = (
					f"diff --git a/{old} b/{old}\n"
					"deleted file mode 100644\n"
					f"--- a/{old}\n+++ /dev/null\n"
					"@@ -1 +0,0 @@\n-OLD_SIDE_CONTENT\n"
					f"diff --git a/{new} b/{new}\n"
					"new file mode 100644\n"
					f"--- /dev/null\n+++ b/{new}\n"
					"@@ -0,0 +1 @@\n+NEW_SIDE_CONTENT\n"
				)
				outside = old if old == "outside.py" else new
				outside_content = (
					"OLD_SIDE_CONTENT" if outside == old else "NEW_SIDE_CONTENT"
				)

				filtered = git_diff.filter_diff(diff, "self", warn=False)

				self.assertNotIn(outside, filtered)
				self.assertNotIn(outside_content, filtered)
				self.assertNotIn(
					outside, git_diff.changed_lines_by_file(filtered)
				)

				for line in (None, 1):
					with self.subTest(line=line):
						review = {
							"summary": "Review",
							"risk": "medium",
							"findings": [{
								"severity": "medium",
								"file": outside,
								"line": line,
								"title": "Outside finding",
								"evidence": "Outside content.",
								"explanation": "Must not be accepted.",
								"recommendation": "Reject this finding.",
								"confidence": "high",
							}],
						}
						with self.assertRaisesRegex(
							ValueError, "file absent from the diff"
						):
							validation.validate_review(review, filtered)

	def test_every_git_mode_disables_rename_detection(self):
		root = SCRIPT_DIR
		commands = {
			"worktree": ["diff", "--no-renames", "HEAD", "--"],
			"staged": ["diff", "--no-renames", "--cached", "--"],
			"branch": ["diff", "--no-renames", "origin/main...HEAD", "--"],
		}

		for mode, command in commands.items():
			with self.subTest(mode=mode):
				with (
					patch.object(git_diff, "get_repo_root", return_value=root),
					patch.object(
						git_diff, "git_run",
						return_value=types.SimpleNamespace(stdout=""),
					) as run,
					patch.object(git_diff, "untracked_files", return_value=[]),
				):
					self.assertEqual(
						git_diff.get_git_diff(mode), ""
					)

				run.assert_called_once_with(
					command + git_diff.TARGET_PATHS["all"], cwd=root
				)

SAMPLE_DIFF = (
	"diff --git a/apps/main.py b/apps/main.py\n"
	"--- a/apps/main.py\n+++ b/apps/main.py\n"
	"@@ -1 +1 @@\n-old\n+new\n"
)


def review_for(file="apps/main.py", line=1):
	return {
		"summary": "Review",
		"risk": "medium",
		"findings": [{
			"severity": "medium",
			"file": file,
			"line": line,
			"title": "Example finding",
			"evidence": "The supplied change.",
			"explanation": "An example for validation.",
			"recommendation": "Review the change.",
			"confidence": "high",
		}],
	}


def _hold_cache_lock(lock_path, acquired, release):
	with review_cache.review_cache_lock(Path(lock_path)):
		acquired.set()
		release.wait(10)


def _acquire_cache_lock(lock_path, acquired):
	with review_cache.review_cache_lock(Path(lock_path)):
		acquired.set()


class CanonicalPathTests(unittest.TestCase):
	def test_git_octal_utf8_paths_are_decoded_consistently(self):
		diff = (
			'diff --git "a/apps/caf\\303\\251.py" "b/apps/caf\\303\\251.py"\n'
			'--- "a/apps/caf\\303\\251.py"\n+++ "b/apps/caf\\303\\251.py"\n'
			"@@ -1 +1 @@\n-old\n+new\n"
		)
		path = "apps/caf" + chr(233) + ".py"
		self.assertEqual(git_diff.paths_in_diff_section(diff), {path})
		self.assertEqual(git_diff.changed_lines_by_file(diff), {path: {1}})
		review = review_for(path)
		self.assertEqual(validation.validate_review(review, diff), review)

	def test_quoted_sensitive_paths_are_excluded(self):
		diff = (
			'diff --git "a/apps/\\056env" "b/apps/\\056env"\n'
			'--- "a/apps/\\056env"\n+++ "b/apps/\\056env"\n'
			"@@ -1 +1 @@\n-old\n+new\n"
		)
		self.assertEqual(git_diff.filter_diff(diff, "all", warn=False), "")

	def test_quoted_path_escapes_round_trip(self):
		for path in (
			"apps/a b.py", 'apps/a"b.py', "apps/a\tb.py",
			"apps/a\\b.py", "apps/caf" + chr(233) + ".py",
		):
			with self.subTest(path=path):
				token = git_diff._quote_git_path("a/" + path)
				self.assertEqual(
					git_diff._decode_git_path_token(token), ("a/" + path, "")
				)

	def test_mixed_quoted_and_unquoted_rename_headers(self):
		for header in (
			'diff --git a/apps/old name.py "b/apps/new\\tname.py"\n',
			'diff --git "a/apps/old\\tname.py" b/apps/new name.py\n',
		):
			with self.subTest(header=header):
				paths = git_diff.paths_in_diff_section(header)
				self.assertEqual(len(paths), 2)
				self.assertEqual(
					set(git_diff.changed_lines_by_file(header)), paths
				)

	def test_metadata_only_files_allow_only_file_level_findings(self):
		sections = (
			"diff --git a/apps/run.py b/apps/run.py\n"
			"old mode 100644\nnew mode 100755\n",
			"diff --git a/apps/empty.py b/apps/empty.py\n"
			"new file mode 100644\n",
			"diff --git a/apps/old.py b/apps/new.py\n"
			"similarity index 100%\nrename from apps/old.py\nrename to apps/new.py\n",
			"diff --git a/apps/image.png b/apps/image.png\n"
			"Binary files a/apps/image.png and b/apps/image.png differ\n",
		)
		for diff in sections:
			for path in git_diff.paths_in_diff_section(diff):
				with self.subTest(path=path):
					self.assertEqual(git_diff.changed_lines_by_file(diff)[path], set())
					review = review_for(path, None)
					self.assertEqual(validation.validate_review(review, diff), review)
					with self.assertRaisesRegex(ValueError, "not an added line"):
						validation.validate_review(review_for(path, 1), diff)

	def test_encoded_binary_payloads_are_rejected(self):
		diff = (
			"diff --git a/apps/image.png b/apps/image.png\n"
			"GIT binary patch\nliteral 4\nencoded-content\n"
		)
		with self.assertRaisesRegex(ValueError, "without --binary"):
			git_diff.filter_diff(diff, "all", warn=False)

	def test_untracked_unicode_and_space_paths_generate_valid_diffs(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "apps").mkdir()
			path = "apps/caf" + chr(233) + " example.py"
			(root / path).write_text("example\n", encoding="utf-8")
			diff = git_diff.untracked_file_to_diff(root, path)
			self.assertEqual(git_diff.filter_diff(diff, "all", warn=False), diff)
			self.assertEqual(git_diff.changed_lines_by_file(diff), {path: {1}})

	def test_real_git_quoted_unicode_paths_and_utf8_content(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "apps").mkdir()
			path = "apps/caf" + chr(233) + ".py"
			content = "example = '" + chr(233) + "'\n"
			(root / path).write_text(content, encoding="utf-8")
			for command in (["init", "-q"], ["add", "."]):
				subprocess.run(
					["git", *command], cwd=root, capture_output=True, check=True,
				)
			with patch.object(git_diff, "get_repo_root", return_value=root):
				diff = git_diff.get_git_diff("staged")
			self.assertIn("+" + content, diff)
			self.assertEqual(git_diff.changed_lines_by_file(diff), {path: {1}})

	def test_unsupported_path_encoding_fails_explicitly(self):
		for token in ('"a/apps/\\777.py"', '"a/apps/\\377.py"', '"a/apps/\\q.py"'):
			with self.subTest(token=token):
				with self.assertRaises(ValueError):
					git_diff._decode_git_path_token(token)


class SecretScannerTests(unittest.TestCase):
	def test_empty_diff_does_not_start_scanner(self):
		with patch.object(secret_scan.subprocess, "run") as run:
			secret_scan.scan_diff_for_secrets(" \n")
		run.assert_not_called()

	def test_scanner_receives_exact_text_and_isolated_default_configuration(self):
		def run(command, **kwargs):
			directory = Path(kwargs["cwd"])
			self.assertNotEqual(directory, SCRIPT_DIR)
			self.assertEqual(kwargs["input"], SAMPLE_DIFF)
			self.assertEqual(kwargs["encoding"], "utf-8")
			self.assertTrue(kwargs["capture_output"])
			self.assertIn("--ignore-gitleaks-allow", command)
			self.assertIn("--redact=100", command)
			config = Path(command[command.index("--config") + 1])
			self.assertEqual(config.read_text(), "[extend]\nuseDefault = true\n")
			self.assertEqual(list(directory.iterdir()), [config])
			return subprocess.CompletedProcess(command, 0)

		with patch.object(secret_scan.subprocess, "run", side_effect=run):
			secret_scan.scan_diff_for_secrets(SAMPLE_DIFF)

	def test_secret_detection_does_not_echo_scanner_output(self):
		result = subprocess.CompletedProcess(
			[], secret_scan.SECRET_FOUND_EXIT_CODE,
			stdout="DO-NOT-ECHO-SCANNER-OUTPUT", stderr="DO-NOT-ECHO-SCANNER-ERROR",
		)
		with patch.object(secret_scan.subprocess, "run", return_value=result):
			with self.assertRaisesRegex(ValueError, "Potential secret") as error:
				secret_scan.scan_diff_for_secrets(SAMPLE_DIFF)
		self.assertNotIn("DO-NOT-ECHO", str(error.exception))

	def test_missing_scanner_timeout_and_operating_system_errors_fail_closed(self):
		for failure in (
			FileNotFoundError("DO-NOT-ECHO"),
			subprocess.TimeoutExpired("DO-NOT-ECHO", 30, output="DO-NOT-ECHO"),
			OSError("DO-NOT-ECHO"),
		):
			with self.subTest(failure=type(failure).__name__):
				with patch.object(secret_scan.subprocess, "run", side_effect=failure):
					with self.assertRaisesRegex(RuntimeError, "no review was sent") as error:
						secret_scan.scan_diff_for_secrets(SAMPLE_DIFF)
				self.assertNotIn("DO-NOT-ECHO", str(error.exception))

	def test_scanner_failure_is_not_treated_as_clean(self):
		result = subprocess.CompletedProcess([], 2, stderr="DO-NOT-ECHO")
		with patch.object(secret_scan.subprocess, "run", return_value=result):
			with self.assertRaisesRegex(RuntimeError, "exit code 2") as error:
				secret_scan.scan_diff_for_secrets(SAMPLE_DIFF)
		self.assertNotIn("DO-NOT-ECHO", str(error.exception))


@unittest.skipUnless(shutil.which("gitleaks"), "Gitleaks is not available on PATH")
class GitleaksIntegrationTests(unittest.TestCase):
	def test_clean_diff_passes_real_scanner(self):
		secret_scan.scan_diff_for_secrets(SAMPLE_DIFF)

	def test_added_deleted_and_context_secrets_are_blocked_even_with_allow_comments(self):
		token = "ghp_" + "".join(random.Random(2026).choices(string.ascii_letters + string.digits, k=36))
		for prefix in ("+", "-", " "):
			with self.subTest(prefix=prefix):
				diff = (
					"diff --git a/apps/config.py b/apps/config.py\n"
					"--- a/apps/config.py\n+++ b/apps/config.py\n"
					"@@ -1 +1 @@\n"
					+ prefix + 'credential = "' + token + '" # gitleaks:allow\n'
				)
				with self.assertRaisesRegex(ValueError, "Potential secret") as error:
					secret_scan.scan_diff_for_secrets(diff)
				self.assertNotIn(token, str(error.exception))

	def test_environment_config_cannot_disable_default_rules(self):
		token = "ghp_" + "".join(random.Random(2026).choices(string.ascii_letters + string.digits, k=36))
		with patch.dict(
			os.environ, {"GITLEAKS_CONFIG_TOML": '[allowlist]\nregexes = [".*"]\n'}
		):
			with self.assertRaisesRegex(ValueError, "Potential secret"):
				secret_scan.scan_diff_for_secrets(SAMPLE_DIFF + "+" + token + "\n")

	def test_real_preflight_blocks_cli_artifacts_and_client(self):
		token = "ghp_" + "".join(random.Random(2026).choices(string.ascii_letters + string.digits, k=36))
		diff = SAMPLE_DIFF.replace("+new", '+credential = "' + token + '"')
		with tempfile.TemporaryDirectory() as directory:
			reports = Path(directory) / "reports"
			output, errors = io.StringIO(), io.StringIO()
			with (
				patch.object(cli, "get_repo_root", return_value=Path(directory)),
				patch.object(cli, "warn_on_exposed_secret_files"),
				patch.object(cli, "get_git_diff", return_value=diff),
				patch.object(cli, "REPORTS_DIR", reports),
				patch.object(reporting, "REPORTS_DIR", reports),
				patch.object(cli, "OpenAI") as client,
				patch("sys.argv", ["review.py"]),
				redirect_stdout(output), redirect_stderr(errors),
			):
				code = cli.main()
			self.assertEqual(code, 1)
			self.assertIn("Potential secret", errors.getvalue())
			self.assertNotIn(token, output.getvalue() + errors.getvalue())
			self.assertFalse(reports.exists())
			client.assert_not_called()


class ReviewCacheTests(unittest.TestCase):
	def test_cache_lock_serializes_independent_processes(self):
		context = multiprocessing.get_context("spawn")
		with tempfile.TemporaryDirectory() as directory:
			lock_path = str(Path(directory) / "cache.lock")
			first_acquired = context.Event()
			release_first = context.Event()
			second_acquired = context.Event()
			first = context.Process(
				target=_hold_cache_lock,
				args=(lock_path, first_acquired, release_first),
			)
			second = context.Process(
				target=_acquire_cache_lock,
				args=(lock_path, second_acquired),
			)
			first.start()
			try:
				self.assertTrue(first_acquired.wait(10), "first process did not acquire lock")
				second.start()
				self.assertFalse(
					second_acquired.wait(0.25),
					"second process acquired the lock while the first held it",
				)
				release_first.set()
				self.assertTrue(
					second_acquired.wait(10),
					"second process did not acquire the released lock",
				)
			finally:
				release_first.set()
				for process in (first, second):
					if process.pid is not None:
						process.join(10)
						if process.is_alive():
							process.terminate()
							process.join(5)
			self.assertEqual(first.exitcode, 0)
			self.assertEqual(second.exitcode, 0)

	def test_key_changes_with_every_request_context(self):
		request = reviewer.build_review_request("test-model", SAMPLE_DIFF)
		root = SCRIPT_DIR
		original = review_cache.review_cache_key(root, request)
		for name, value in (
			("model", "another-model"),
			("input", "another diff"),
			("instructions", "new instructions"),
			("max_output_tokens", 100),
			("text", {"format": {"schema": {"description": "new schema"}}}),
			("store", True),
		):
			with self.subTest(field=name):
				changed = {**request, name: value}
				self.assertNotEqual(
					review_cache.review_cache_key(root, changed), original
				)
		self.assertNotEqual(review_cache.review_cache_key(root / "other", request), original)
		self.assertNotEqual(
			review_cache.review_cache_key(root, request, target="self"), original
		)
		with patch.dict(os.environ, {"OPENAI_BASE_URL": "https://example.invalid"}):
			self.assertNotEqual(review_cache.review_cache_key(root, request), original)

	def test_cache_key_is_deterministic_and_contains_only_a_digest(self):
		request = reviewer.build_review_request("test-model", SAMPLE_DIFF)
		root = SCRIPT_DIR
		key = review_cache.review_cache_key(root, request)
		self.assertRegex(key, r"^[a-f0-9]{64}$")
		self.assertEqual(review_cache.review_cache_key(root, dict(reversed(list(request.items())))), key)

	def test_cache_round_trip_revalidates_findings_against_current_diff(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / ".cache" / "review.json"
			self.assertIsNone(review_cache.load_cached_review(path, SAMPLE_DIFF))
			review_cache.save_cached_review(path, review_for())
			self.assertEqual(review_cache.load_cached_review(path, SAMPLE_DIFF), review_for())
			other_diff = SAMPLE_DIFF.replace("apps/main.py", "apps/other.py")
			with self.assertRaisesRegex(ValueError, "--refresh"):
				review_cache.load_cached_review(path, other_diff)

	def test_corrupt_or_invalid_cache_fails_explicitly(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "review.json"
			for content in (
				"not json", "{}", '{"summary":"Review","risk":[],"findings":[]}',
				'{"summary":"Review","risk":"low","findings":[],"unexpected":true}',
			):
				with self.subTest(content=content):
					path.write_text(content, encoding="utf-8")
					with self.assertRaisesRegex(ValueError, "--refresh"):
						review_cache.load_cached_review(path, SAMPLE_DIFF)

	def test_failed_atomic_publish_preserves_existing_cache_and_removes_temporary_file(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "review.json"
			original = review_for()
			review_cache.save_cached_review(path, original)
			with patch.object(review_cache.os, "replace", side_effect=OSError("disk error")):
				with self.assertRaises(OSError):
					review_cache.save_cached_review(path, {**original, "summary": "New"})
			self.assertEqual(review_cache.load_cached_review(path, SAMPLE_DIFF), original)
			self.assertEqual(list(Path(directory).iterdir()), [path])


class ReviewRequestTests(unittest.TestCase):
	def test_request_disables_storage_and_matches_the_cache_request(self):
		client = Mock()
		client.responses.create.return_value = types.SimpleNamespace(
			status="completed", output_text=json.dumps(review_for()), usage=None,
		)
		with (
			patch.object(reviewer, "scan_diff_for_secrets") as scan,
			redirect_stdout(io.StringIO()),
		):
			result = reviewer.call_openai(client, "test-model", SAMPLE_DIFF)
		self.assertEqual(result, review_for())
		scan.assert_called_once_with(SAMPLE_DIFF)
		client.responses.create.assert_called_once_with(
			**reviewer.build_review_request("test-model", SAMPLE_DIFF)
		)
		self.assertIs(client.responses.create.call_args.kwargs["store"], False)
		self.assertNotIn("tools", client.responses.create.call_args.kwargs)

	def test_direct_call_blocks_empty_and_sensitive_only_diffs(self):
		client = Mock()
		for diff in (
			"", SAMPLE_DIFF.replace("apps/main.py", "apps/.env"),
		):
			with self.subTest(diff=diff):
				with (
					patch.object(reviewer, "scan_diff_for_secrets") as scan,
					redirect_stderr(io.StringIO()),
				):
					with self.assertRaisesRegex(ValueError, "no API request"):
						reviewer.call_openai(client, "test-model", diff)
				scan.assert_not_called()
		client.responses.create.assert_not_called()

	def test_direct_call_reviews_arbitrary_paths_by_default(self):
		diff = SAMPLE_DIFF.replace("apps/main.py", "docs/guide.md")
		client = Mock()
		client.responses.create.return_value = types.SimpleNamespace(
			status="completed", output_text=json.dumps(review_for("docs/guide.md")), usage=None,
		)
		with (
			patch.object(reviewer, "scan_diff_for_secrets"),
			redirect_stdout(io.StringIO()),
		):
			result = reviewer.call_openai(client, "test-model", diff)
		self.assertEqual(result, review_for("docs/guide.md"))
		client.responses.create.assert_called_once_with(
			**reviewer.build_review_request("test-model", diff)
		)

	def test_direct_call_blocks_secret_before_request(self):
		client = Mock()
		with patch.object(reviewer, "scan_diff_for_secrets", side_effect=ValueError("blocked")):
			with self.assertRaisesRegex(ValueError, "blocked"):
				reviewer.call_openai(client, "test-model", SAMPLE_DIFF)
		client.responses.create.assert_not_called()

	def test_environment_values_are_not_added_to_model_input(self):
		with patch.dict(os.environ, {
			"OPENAI_API_KEY": "test-authentication-value",
			"DATABASE_URL": "test-private-database-value",
		}):
			request = reviewer.build_review_request("test-model", SAMPLE_DIFF)
		encoded = json.dumps(request)
		self.assertNotIn("test-authentication-value", encoded)
		self.assertNotIn("test-private-database-value", encoded)

	def test_malformed_incomplete_and_invalid_reviews_fail(self):
		for status, text, error_type in (
			("completed", "{", RuntimeError),
			("completed", "", RuntimeError),
			("incomplete", "{}", RuntimeError),
			("completed", json.dumps(review_for("apps/outside.py")), ValueError),
		):
			with self.subTest(status=status, text=text):
				client = Mock()
				client.responses.create.return_value = types.SimpleNamespace(
					status=status, output_text=text, usage=None, incomplete_details=None,
				)
				with (
					patch.object(reviewer, "scan_diff_for_secrets"),
					redirect_stdout(io.StringIO()),
				):
					with self.assertRaises(error_type):
						reviewer.call_openai(client, "test-model", SAMPLE_DIFF)


class HardenedCliTests(unittest.TestCase):
	def setUp(self):
		temporary = self.enterContext(tempfile.TemporaryDirectory())
		self.root = Path(temporary)
		self.reports = self.root / "reports"
		self.enterContext(patch.dict(
			os.environ,
			{"OPENAI_MODEL": "test-model", "OPENAI_API_KEY": "test-api-key"},
			clear=True,
		))
		self.enterContext(patch.object(cli, "get_repo_root", return_value=self.root))
		self.enterContext(patch.object(cli, "warn_on_exposed_secret_files"))
		self.diff = self.enterContext(patch.object(cli, "get_git_diff", return_value=SAMPLE_DIFF))
		self.scan = self.enterContext(patch.object(cli, "scan_diff_for_secrets"))
		self.client = self.enterContext(patch.object(cli, "OpenAI"))
		self.call_model = self.enterContext(patch.object(cli, "call_openai", return_value=review_for()))
		self.enterContext(patch.object(cli, "REPORTS_DIR", self.reports))
		self.enterContext(patch.object(reporting, "REPORTS_DIR", self.reports))

	def run_cli(self, *args):
		output, errors = io.StringIO(), io.StringIO()
		with (
			patch("sys.argv", ["review.py", *args]),
			redirect_stdout(output), redirect_stderr(errors),
		):
			code = cli.main()
		return code, output.getvalue(), errors.getvalue()

	def test_identical_successful_diff_reuses_review_without_api_key(self):
		self.assertEqual(self.run_cli()[0], 0)
		del os.environ["OPENAI_API_KEY"]
		code, output, errors = self.run_cli()
		self.assertEqual(code, 0)
		self.assertEqual(json.loads(output), review_for())
		self.assertIn("no API request made", errors)
		self.call_model.assert_called_once()
		self.client.assert_called_once()
		self.assertEqual(self.scan.call_count, 2)
		self.assertEqual(len(list(self.reports.glob("*.json"))), 2)
		self.assertEqual(len(list((self.reports / ".cache").glob("*.json"))), 1)

	def test_changed_diff_model_prompt_schema_and_settings_do_not_reuse_cache(self):
		self.assertEqual(self.run_cli()[0], 0)
		self.diff.return_value = SAMPLE_DIFF.replace("+new", "+changed")
		self.assertEqual(self.run_cli()[0], 0)
		os.environ["OPENAI_MODEL"] = "another-model"
		self.assertEqual(self.run_cli()[0], 0)
		with patch.object(reviewer, "get_review_instructions", return_value="New instructions"):
			self.assertEqual(self.run_cli()[0], 0)
		with patch.dict(validation.REVIEW_SCHEMA, {"description": "New schema"}):
			self.assertEqual(self.run_cli()[0], 0)
		with patch.object(reviewer, "MAX_OUTPUT_TOKENS", 100):
			self.assertEqual(self.run_cli()[0], 0)
		self.assertEqual(self.call_model.call_count, 6)

	def test_refresh_requests_new_review_and_updates_cache(self):
		self.assertEqual(self.run_cli()[0], 0)
		refreshed = {**review_for(), "summary": "Fresh review"}
		self.call_model.return_value = refreshed
		self.assertEqual(self.run_cli("--refresh")[0], 0)
		code, output, _ = self.run_cli()
		self.assertEqual(code, 0)
		self.assertEqual(json.loads(output), refreshed)
		self.assertEqual(self.call_model.call_count, 2)

	def test_force_overwrites_explicit_artifacts_without_forcing_api_call(self):
		output = self.root / "chosen.json"
		self.assertEqual(self.run_cli("--output", str(output))[0], 0)
		self.assertEqual(self.run_cli("--output", str(output), "--force")[0], 0)
		self.call_model.assert_called_once()
		self.assertEqual(json.loads(output.read_text()), review_for())
		self.assertEqual(output.with_suffix(".diff").read_text(), SAMPLE_DIFF)

	def test_existing_artifact_is_not_overwritten_without_force(self):
		output = self.root / "chosen.json"
		output.write_text("existing")
		code, _, errors = self.run_cli("--output", str(output))
		self.assertEqual(code, 1)
		self.assertIn("already exists", errors)
		self.assertEqual(output.read_text(), "existing")
		self.client.assert_not_called()
		self.call_model.assert_not_called()

	def test_scanner_blocks_artifacts_and_api_client(self):
		for failure in (ValueError("Potential secret"), RuntimeError("Scanner missing")):
			with self.subTest(failure=type(failure).__name__):
				self.scan.side_effect = failure
				self.assertEqual(self.run_cli()[0], 1)
				self.assertFalse(self.reports.exists())
		self.client.assert_not_called()
		self.call_model.assert_not_called()

	def test_empty_or_oversized_diff_needs_no_scanner_client_or_artifacts(self):
		os.environ.clear()
		for diff, expected_code in (("", 0), (" " * (git_diff.MAX_DIFF_CHARS + 1) + "x", 1)):
			with self.subTest(expected_code=expected_code):
				self.diff.return_value = diff
				self.assertEqual(self.run_cli()[0], expected_code)
		self.scan.assert_not_called()
		self.client.assert_not_called()
		self.assertFalse(self.reports.exists())

	def test_missing_configuration_does_not_persist_diff(self):
		for missing in ("OPENAI_MODEL", "OPENAI_API_KEY"):
			with self.subTest(missing=missing):
				with patch.dict(os.environ, {missing: ""}):
					code, _, errors = self.run_cli()
				self.assertEqual(code, 1)
				self.assertIn(missing, errors)
				self.assertEqual(list(self.reports.glob("*.json")), [])
				self.assertEqual(list(self.reports.glob("*.diff")), [])
		self.client.assert_not_called()

	def test_failed_or_invalid_review_is_not_cached(self):
		for failure in (RuntimeError("API failed"), ValueError("Invalid model review")):
			with self.subTest(failure=type(failure).__name__):
				self.call_model.side_effect = failure
				self.assertEqual(self.run_cli()[0], 1)
				self.assertEqual(list((self.reports / ".cache").glob("*.json")), [])
		self.call_model.side_effect = None
		self.assertEqual(self.run_cli()[0], 0)
		self.assertEqual(self.call_model.call_count, 3)

	def test_failed_report_persistence_is_not_cached(self):
		with patch.object(cli, "write_artifact", side_effect=[None, OSError("disk full")]):
			self.assertEqual(self.run_cli()[0], 1)
		self.assertEqual(list((self.reports / ".cache").glob("*.json")), [])
		self.assertEqual(self.run_cli()[0], 0)
		self.assertEqual(self.call_model.call_count, 2)

	def test_corrupt_cache_requires_explicit_refresh(self):
		self.assertEqual(self.run_cli()[0], 0)
		path = next((self.reports / ".cache").glob("*.json"))
		path.write_text("invalid")
		code, _, errors = self.run_cli()
		self.assertEqual(code, 1)
		self.assertIn("--refresh", errors)
		self.call_model.assert_called_once()
		self.assertEqual(self.run_cli("--refresh")[0], 0)
		self.assertEqual(self.call_model.call_count, 2)

	def test_diff_file_uses_the_same_scanning_and_cache_flow(self):
		with patch.object(cli, "read_diff_file", return_value=SAMPLE_DIFF):
			self.assertEqual(self.run_cli("--diff-file", "input.diff")[0], 0)
			self.assertEqual(self.run_cli("--diff-file", "input.diff")[0], 0)
		self.diff.assert_not_called()
		self.call_model.assert_called_once()
		self.assertEqual(self.scan.call_count, 2)


if __name__ == "__main__":
	unittest.main()


class StandaloneRepositoryTests(unittest.TestCase):
	def test_default_target_includes_arbitrary_repository_paths(self):
		with tempfile.TemporaryDirectory() as temporary:
			repo = Path(temporary)
			subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
			subprocess.run(["git", "config", "user.email", "reviewer-test@example.invalid"], cwd=repo, check=True)
			subprocess.run(["git", "config", "user.name", "AI Reviewer Tests"], cwd=repo, check=True)

			(repo / "apps").mkdir()
			(repo / "docs").mkdir()
			(repo / "apps" / "app.ts").write_text("export const value = 1\n", encoding="utf-8")
			(repo / "docs" / "guide.md").write_text("Version one\n", encoding="utf-8")
			subprocess.run(["git", "add", "."], cwd=repo, check=True)
			subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)

			(repo / "docs" / "guide.md").write_text("Version two\n", encoding="utf-8")

			default_diff = git_diff.get_git_diff("worktree", repo_root=repo)
			self.assertIn("docs/guide.md", default_diff)

	def test_repository_root_is_discovered_from_another_directory(self):
		with tempfile.TemporaryDirectory() as temporary:
			repo = Path(temporary)
			nested = repo / "nested"
			nested.mkdir(parents=True)
			subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
			self.assertEqual(git_diff.get_repo_root(nested), repo.resolve())

