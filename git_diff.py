"""Collect, filter, and inspect Git diffs for review."""

import ast
import difflib
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR
MAX_DIFF_CHARS = 55_000
MAX_DIFF_BYTES = MAX_DIFF_CHARS * 4
TARGET_PATHS = {
	# Empty pathspec means the whole repository. This is the portable default.
	"all": [],
	"self": [
		"__init__.py",
		"cli.py",
		"git_diff.py",
		"prompts.py",
		"reporting.py",
		"review.py",
		"reviewer.py",
		"review_cache.py",
		"secret_scan.py",
		"validation.py",
		"tests/",
		"requirements.txt",
		"tools/reviewed/__init__.py",
		"tools/reviewed/cli.py",
		"tools/reviewed/git_diff.py",
		"tools/reviewed/prompts.py",
		"tools/reviewed/reporting.py",
		"tools/reviewed/review_cache.py",
		"tools/reviewed/review.py",
		"tools/reviewed/reviewer.py",
		"tools/reviewed/secret_scan.py",
		"tools/reviewed/validation.py",
		"tools/reviewed/tests/",
		"tools/reviewed/requirements.txt",
	],
}
SENSITIVE_EXACT_NAMES = {
	".env", ".npmrc", ".pypirc", ".netrc", "id_rsa", "id_ed25519",
	"credentials", "credentials.json", "service-account.json",
}
SENSITIVE_CONFIG_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".ini", ".conf"}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".p7b", ".p7c", ".jks", ".keystore"}


def is_sensitive_path(path: str) -> bool:
	"""Return True for environment files and common private-key/credential files."""
	normalized = path.replace("\\", "/")
	name = PurePosixPath(normalized).name.lower()
	if name in SENSITIVE_EXACT_NAMES or name.startswith(".env"):
		return True
	suffix = PurePosixPath(name).suffix.lower()
	if suffix in SENSITIVE_SUFFIXES:
		return True
	return suffix in SENSITIVE_CONFIG_SUFFIXES and any(
		marker in name for marker in ("secret", "credential", "token", "service-account")
	)


def is_in_scope(path: str, target: str) -> bool:
	"""Check whether a repository-relative path belongs to a review target."""
	if target == "all":
		return True
	normalized = path.replace("\\", "/").removeprefix("./")
	for prefix in TARGET_PATHS[target]:
		if (prefix.endswith("/") and normalized.startswith(prefix)) or normalized == prefix:
			return True
	return False


def git_run(args: list[str], *, cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess:
	"""Run Git with consistent error handling."""
	try:
		return subprocess.run(["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", check=True, timeout=timeout)
	except subprocess.CalledProcessError as exc:
		detail = (exc.stderr or "").strip() or "Git command failed."
		raise RuntimeError(f"Could not collect Git diff: {detail}") from None
	except subprocess.TimeoutExpired:
		raise RuntimeError("Git command timed out.") from None
	except FileNotFoundError:
		raise RuntimeError("Git executable was not found.") from None


def get_repo_root(start_path: Path | None = None) -> Path:
	"""Find the Git repository containing start_path (or the current directory)."""
	cwd = (start_path or REPO_ROOT).expanduser().resolve()
	result = git_run(["rev-parse", "--show-toplevel"], cwd=cwd, timeout=10)
	return Path(result.stdout.strip()).resolve()


def warn_on_exposed_secret_files(repo_root: Path) -> None:
	"""Warn about tracked or non-ignored secret-like files without reading them."""
	result = git_run(["ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=repo_root)
	exposed = sorted({path for path in result.stdout.split("\0") if path and is_sensitive_path(path)})
	if exposed:
		print(
			"Warning: secret-like files are tracked or not ignored by Git and will be "
			"excluded from review payloads: " + ", ".join(exposed), file=sys.stderr,
		)


def split_diff_sections(diff: str) -> list[str]:
	"""Split a Git diff into per-file sections, preserving each section verbatim."""
	if not diff:
		return []
	matches = list(re.finditer(r"(?m)^diff --git ", diff))
	if not matches:
		return [diff]
	sections: list[str] = []
	if matches[0].start() > 0:
		prefix = diff[:matches[0].start()]
		if prefix.strip():
			sections.append(prefix)
	for index, match in enumerate(matches):
		end = matches[index + 1].start() if index + 1 < len(matches) else len(diff)
		sections.append(diff[match.start():end])
	return sections


def _decode_git_path_token(value: str) -> tuple[str, str]:
	"""Decode one Git path token and return (path, remaining text)."""
	value = value.lstrip()
	if not value:
		raise ValueError("Missing path in Git diff header.")
	if value.startswith('"'):
		escaped = False
		end = None
		for index in range(1, len(value)):
			char = value[index]
			if escaped:
				escaped = False
			elif char == "\\":
				escaped = True
			elif char == '"':
				end = index + 1
				break
		if end is None:
			raise ValueError("Malformed quoted path in Git diff header.")
		raw = value[1:end - 1]
		decoded_bytes = bytearray()
		escapes = {
			"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13,
			"\\": 92, '"': 34,
		}
		index = 0
		while index < len(raw):
			if raw[index] != "\\":
				decoded_bytes.extend(raw[index].encode("utf-8"))
				index += 1
				continue
			index += 1
			octal = re.match(r"[0-7]{1,3}", raw[index:])
			if octal:
				byte = int(octal.group(), 8)
				if byte > 255:
					raise ValueError("Invalid octal byte in Git diff path.")
				decoded_bytes.append(byte)
				index += len(octal.group())
			elif index < len(raw) and raw[index] in escapes:
				decoded_bytes.append(escapes[raw[index]])
				index += 1
			else:
				raise ValueError("Unsupported escape in Git diff path.")
		try:
			decoded = decoded_bytes.decode("utf-8")
		except UnicodeDecodeError:
			raise ValueError("Git diff paths must be valid UTF-8.") from None
		return decoded, value[end:].lstrip()
	match = re.match(r"\S+", value)
	if not match:
		raise ValueError("Malformed path in Git diff header.")
	return match.group(0), value[match.end():].lstrip()


def _strip_git_prefix(path: str) -> str:
	if path == "/dev/null":
		return path
	return path[2:] if path.startswith(("a/", "b/")) else path


def _quote_git_path(path: str) -> str:
	"""Encode synthetic diff paths using Git's C-style quoting."""
	if all(33 <= ord(char) < 127 and char not in {'"', "\\"} for char in path):
		return path
	quoted: list[str] = []
	for byte in path.encode("utf-8"):
		if byte in {34, 92}:
			quoted.append("\\" + chr(byte))
		elif 32 <= byte < 127:
			quoted.append(chr(byte))
		else:
			quoted.append(f"\\{byte:03o}")
	return '"' + "".join(quoted) + '"'


def _paths_from_git_header(line: str) -> tuple[str, str]:
	"""Parse the file identity from a `diff --git` header."""
	if not line.startswith("diff --git "):
		raise ValueError("Diff section is missing its Git file header.")
	payload = line[len("diff --git "):].rstrip("\r\n")
	if payload.startswith('"'):
		old_token, remainder = _decode_git_path_token(payload)
		if remainder.startswith('"'):
			new_token, remainder = _decode_git_path_token(remainder)
		else:
			new_token, remainder = remainder, ""
		if remainder:
			raise ValueError("Unexpected text after paths in Git diff header.")
	else:
		match = re.fullmatch(r'(a/.+) ("b/.*")', payload)
		if match:
			old_token = match.group(1)
			new_token, remainder = _decode_git_path_token(match.group(2))
			if remainder:
				raise ValueError("Unexpected text after paths in Git diff header.")
		else:
			match = re.fullmatch(r"a/(.+) b/(.+)", payload)
			if not match:
				raise ValueError("Could not safely parse paths in Git diff header.")
			old_token, new_token = "a/" + match.group(1), "b/" + match.group(2)
	if not old_token.startswith("a/") or not new_token.startswith("b/"):
		raise ValueError("Git diff header paths must use a/ and b/ prefixes.")
	old_path, new_path = _strip_git_prefix(old_token), _strip_git_prefix(new_token)
	if not old_path or not new_path:
		raise ValueError("Empty path in Git diff header.")
	return old_path, new_path


def _path_from_file_header(line: str, marker: str) -> str:
	"""Parse a ---/+++ path header, preserving /dev/null as a special value."""
	if not line.startswith(marker):
		raise ValueError(f"Expected a {marker.strip()} file header.")
	raw = line[len(marker):].split("\t", 1)[0]
	if raw.startswith('"'):
		parsed, remainder = _decode_git_path_token(raw)
		if remainder:
			raise ValueError("Unexpected text after a quoted file-header path.")
		raw = parsed
	if raw == "/dev/null":
		return raw
	if not raw:
		raise ValueError("Empty path in file header.")
	return _strip_git_prefix(raw)


def _header_lines_before_hunk(section: str) -> tuple[str | None, str | None]:
	"""Return ---/+++ metadata only before the first hunk or binary payload."""
	old_header: str | None = None
	new_header: str | None = None
	for line in section.splitlines():
		if line.startswith("@@ ") or line == "GIT binary patch" or line.startswith("Binary files "):
			break
		if line.startswith("--- ") and old_header is None:
			old_header = line
		elif line.startswith("+++ ") and new_header is None:
			new_header = line
	return old_header, new_header



def paths_in_diff_section(section: str) -> set[str]:
	"""Extract paths from a structurally validated Git diff section."""
	lines = section.splitlines()

	# A section must begin with its own Git file-identity header.
	if not lines or not lines[0].startswith("diff --git "):
		raise ValueError(
			"Every diff section must start with a valid 'diff --git' header."
		)

	git_header = lines[0]
	old_header, new_header = _header_lines_before_hunk(section)

	old_path, new_path = _paths_from_git_header(git_header)

	if (old_header is None) != (new_header is None):
		raise ValueError(
			"Incomplete ---/+++ metadata in supplied Git diff section."
		)

	if old_header is not None and new_header is not None:
		header_old = _path_from_file_header(old_header, "--- ")
		header_new = _path_from_file_header(new_header, "+++ ")

		if header_old == "/dev/null":
			consistent = old_path == new_path and header_new == new_path
		elif header_new == "/dev/null":
			consistent = old_path == new_path and header_old == old_path
		else:
			consistent = (
				header_old == old_path and header_new == new_path
			)

		if not consistent:
			raise ValueError(
				"File headers disagree with the diff --git paths."
			)

	return {old_path, new_path}



def filter_diff(diff: str, target: str, *, warn: bool = True) -> str:
	"""Keep only in-scope diff sections and exclude secret-bearing files."""
	kept: list[str] = []
	excluded_sensitive: set[str] = set()
	excluded_scope: set[str] = set()
	has_git_sections = bool(re.search(r"(?m)^diff --git ", diff))
	for section in split_diff_sections(diff):
		if has_git_sections and section.strip() and not section.startswith("diff --git "):
			raise ValueError("Unexpected content outside Git diff sections; refusing scope filtering.")
		
		paths = paths_in_diff_section(section)
		if not has_git_sections and len(paths) > 1:
			raise ValueError(
				"A multi-file --diff-file input must use Git diff format with "
				"'diff --git' headers so target and secret filtering stays reliable."
			)
		if not paths:
			if section.strip():
				raise ValueError("Could not determine file paths in supplied diff.")
			continue

		safe_paths = {path for path in paths if not is_sensitive_path(path)}
		excluded_sensitive.update(path for path in paths if is_sensitive_path(path))
		if not safe_paths:
			continue
		in_scope_paths = {path for path in safe_paths if is_in_scope(path, target)}
		if not in_scope_paths:
			excluded_scope.update(safe_paths)
			continue

		if in_scope_paths != safe_paths:
			raise ValueError(
				f"Diff section crosses target '{target}' scope: "
				+ ", ".join(sorted(safe_paths))
				+ ". Regenerate the diff with 'git diff --no-renames'."
			)

		if any(is_sensitive_path(path) for path in paths):
			continue
		if "GIT binary patch" in section.splitlines():
			raise ValueError(
				"Encoded binary patch payloads cannot be reviewed safely. "
				"Regenerate the diff without --binary."
			)
		kept.append(section)

	if warn and excluded_sensitive:
		print(
			"Warning: excluded sensitive file(s) from the review diff: " + ", ".join(sorted(excluded_sensitive)),
			file=sys.stderr,
		)
	if warn and excluded_scope:
		print(f"Ignored {len(excluded_scope)} out-of-scope file path(s) for target '{target}'.", file=sys.stderr)
	return "".join(kept).strip() + ("\n" if kept else "")


def untracked_files(repo_root: Path, target: str) -> list[str]:
	"""Return in-scope untracked, non-ignored files without modifying the index."""
	result = git_run(
		["ls-files", "--others", "--exclude-standard", "-z", "--", *TARGET_PATHS[target]], cwd=repo_root,
	)
	paths = [path for path in result.stdout.split("\0") if path]
	return sorted(path for path in paths if is_in_scope(path, target))


def _is_symlink_or_junction(path: Path) -> bool:
	"""Check link-like filesystem entries without following the final entry."""
	if path.is_symlink():
		return True
	is_junction = getattr(path, "is_junction", None)
	return bool(is_junction and is_junction())


def _contains_symlink_component(repo_root: Path, relative_path: str) -> bool:
	"""Return True if any path component is a symlink or Windows junction."""
	normalized = relative_path.replace("\\", "/")
	parts = PurePosixPath(normalized).parts
	if not normalized or normalized.startswith("/") or any(part in {".", ".."} for part in normalized.split("/")) or (parts and parts[0].endswith(":")):
		raise ValueError("Untracked path must be a safe repository-relative path.")
	current = repo_root.resolve()
	for part in parts:
		current = current / part
		if _is_symlink_or_junction(current):
			return True
	return False


def untracked_file_to_diff(repo_root: Path, relative_path: str) -> str:
	"""Create a bounded unified diff for one untracked regular text file."""
	if is_sensitive_path(relative_path):
		print(f"Warning: excluded sensitive untracked file: {relative_path}", file=sys.stderr)
		return ""
	if _contains_symlink_component(repo_root, relative_path):
		print(f"Warning: excluded symlink/junction untracked path: {relative_path}", file=sys.stderr)
		return ""
	path = repo_root.resolve() / Path(*PurePosixPath(relative_path.replace("\\", "/")).parts)
	if not path.is_file():
		return ""
	flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
	try:
		descriptor = os.open(path, flags)
	except OSError as exc:
		print(f"Warning: could not safely open untracked file {relative_path}: {exc}", file=sys.stderr)
		return ""
	try:
		with os.fdopen(descriptor, "rb") as source:
			if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
				print(f"Warning: skipped non-regular untracked file: {relative_path}", file=sys.stderr)
				return ""
			raw = source.read(MAX_DIFF_BYTES + 1)
	except OSError as exc:
		raise RuntimeError(f"Could not read untracked file {relative_path}: {exc}") from None
	if len(raw) > MAX_DIFF_BYTES:
		raise ValueError(f"Untracked file exceeds the size limit and was not read: {relative_path}")
	try:
		content = raw.decode("utf-8")
	except UnicodeDecodeError:
		print(f"Warning: skipped non-UTF-8 untracked file: {relative_path}", file=sys.stderr)
		return ""
	if "\x00" in content:
		print(f"Warning: skipped binary untracked file: {relative_path}", file=sys.stderr)
		return ""
	lines = content.splitlines()
	old_token = _quote_git_path(f"a/{relative_path}")
	new_token = _quote_git_path(f"b/{relative_path}")
	if not lines:
		body = f"--- /dev/null\n+++ {new_token}\n"
	else:
		unified = list(difflib.unified_diff([], lines, fromfile="/dev/null", tofile=new_token, lineterm=""))
		body = "\n".join(unified) + "\n"
	return f"diff --git {old_token} {new_token}\nnew file mode 100644\n{body}"


def get_git_diff(
	mode: str,
	base: str = "origin/main",
	target: str = "all",
	repo_root: Path | None = None,
) -> str:
	"""Collect scoped tracked changes plus scoped untracked text files."""
	if mode not in {"worktree", "staged", "branch"}:
		raise ValueError(f"Unsupported Git diff mode: {mode}")
	if target not in TARGET_PATHS:
		raise ValueError(f"Unsupported review target: {target}")
	repo_root = get_repo_root(repo_root) if repo_root is not None else get_repo_root()
	commands = {
		"worktree": ["diff", "--no-renames", "HEAD", "--"],
		"staged": ["diff", "--no-renames", "--cached", "--"],
		"branch": ["diff", "--no-renames", f"{base}...HEAD", "--"],
	}
	raw_diff = git_run(commands[mode] + TARGET_PATHS[target], cwd=repo_root).stdout
	filtered_diff = filter_diff(raw_diff, target)
	if mode == "worktree":
		new_sections = [
			section for path in untracked_files(repo_root, target)
			if (section := untracked_file_to_diff(repo_root, path))
		]
		if new_sections:
			filtered_diff = (filtered_diff.rstrip() + "\n" if filtered_diff else "") + "\n".join(new_sections)
			filtered_diff = filter_diff(filtered_diff, target)
	return filtered_diff


def read_diff_file(path: Path) -> str:
	"""Read a diff with a bounded allocation before UTF-8 decoding."""
	if not path.is_file():
		raise ValueError("The specified diff file does not exist.")
	with path.open("rb") as file:
		raw = file.read(MAX_DIFF_BYTES + 1)
	if len(raw) > MAX_DIFF_BYTES:
		raise ValueError("Diff file exceeds the maximum byte size.")
	try:
		diff = raw.decode("utf-8")
	except UnicodeDecodeError:
		raise ValueError("Diff file must be valid UTF-8 text.") from None
	if len(diff) > MAX_DIFF_CHARS:
		raise ValueError(f"Diff exceeds the {MAX_DIFF_CHARS}-character limit.")
	return diff


def changed_lines_by_file(diff: str) -> dict[str, set[int]]:
	"""Collect changed file paths and added-line numbers from a unified diff."""
	result: dict[str, set[int]] = {}
	for section in split_diff_sections(diff):
		if not section.strip():
			continue
		for path in paths_in_diff_section(section):
			result.setdefault(path, set())
		_, new_header = _header_lines_before_hunk(section)
		current_file = _path_from_file_header(new_header, "+++ ") if new_header is not None else None
		new_line = 0
		in_hunk = False
		for text in section.splitlines():
			if text.startswith("@@ "):
				match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", text)
				if match:
					new_line = int(match.group(1))
					in_hunk = True
				else:
					in_hunk = False
				continue
			if not in_hunk or current_file is None:
				continue
			if text.startswith("\\ No newline"):
				continue
			if text.startswith("+"):
				if current_file != "/dev/null":
					result[current_file].add(new_line)
				new_line += 1
			elif text.startswith("-"):
				continue
			elif text.startswith(" "):
				new_line += 1
			else:
				in_hunk = False
	return result
