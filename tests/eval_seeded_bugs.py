"""Seeded-bug evaluation for the reviewer.

Builds a throwaway Git repo from a clean baseline, applies 24 known bugs plus
5 benign edits (refactors, docstrings) to the working tree, reviews batches of
at most 5 bugs, and reports bugs caught, false positives, and failed batches.

Not collected by `unittest discover` because it calls the OpenAI API.

    python tests/eval_seeded_bugs.py --check      # offline: validate the seeds
    python tests/eval_seeded_bugs.py              # live run, needs OPENAI_* config
    python tests/eval_seeded_bugs.py --output eval.json
	python tests/eval_seeded_bugs.py --rescore eval.json --output adjudicated.json

Automatic function matches are provisional. In the saved JSON, adjudicate each
entry in "adjudications" by setting "verdict" to "confirmed_bug" (and "bug_id"
to its seed ID), "false_positive", or "other_issue" for a genuine unseeded issue.
Use --rescore to calculate confirmed metrics without making any API calls.
"""

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from reviewed.git_diff import filter_diff, get_git_diff, split_diff_sections  # noqa: E402
from reviewed.validation import validate_review, validate_review_structure

BASELINE = {
	"inventory.py": '''\
"""Inventory tracking."""

MAX_ITEMS = 100


def add_item(items, name, qty):
    """Add qty of name to items and return the new total for that item."""
    if qty <= 0:
        raise ValueError("qty must be positive")
    items[name] = items.get(name, 0) + qty
    return items[name]


def remove_item(items, name, qty):
    """Remove qty of name; raise if stock is insufficient."""
    current = items.get(name, 0)
    if qty > current:
        raise ValueError("insufficient stock")
    items[name] = current - qty
    if items[name] == 0:
        del items[name]
    return items.get(name, 0)


def low_stock(items, threshold=5):
    """Return names with fewer than threshold units, sorted."""
    return sorted(name for name, qty in items.items() if qty < threshold)


def paginate(seq, page, per_page=10):
    """Return the 1-indexed page of seq."""
    start = (page - 1) * per_page
    return seq[start:start + per_page]


def total_units(items):
    """Sum of all unit counts."""
    total = 0
    for qty in items.values():
        total += qty
    return total
''',
	"billing.py": '''\
"""Billing calculations."""
from decimal import Decimal, ROUND_HALF_UP

TAX_RATE = Decimal("0.08")


def apply_discount(price, percent):
    """Return price reduced by percent (0-100)."""
    if not 0 <= percent <= 100:
        raise ValueError("percent out of range")
    return price * (Decimal(100) - Decimal(percent)) / Decimal(100)


def add_tax(price):
    """Return price including tax, rounded to cents."""
    return (price * (1 + TAX_RATE)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def split_bill(total, people):
    """Split total evenly; the first person absorbs leftover cents."""
    if people <= 0:
        raise ValueError("people must be positive")
    share = (total / people).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    shares = [share] * people
    shares[0] += total - share * people
    return shares


def average_order(orders):
    """Mean order total, or zero for no orders."""
    if not orders:
        return Decimal("0")
    return sum(orders) / len(orders)


def invoice_total(lines):
    """lines: iterable of (unit_price, quantity)."""
    subtotal = sum(price * qty for price, qty in lines)
    return add_tax(subtotal)


def format_money(amount):
    return f"${amount:.2f}"
''',
	"auth.py": '''\
"""Authentication helpers."""
import hashlib
import hmac
import secrets
import time

SESSION_TTL = 3600


def hash_password(password, salt):
    """Derive a password hash with PBKDF2."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)


def verify_password(password, salt, expected):
    """Constant-time password check."""
    return hmac.compare_digest(hash_password(password, salt), expected)


def new_salt():
    """Random per-user salt."""
    return secrets.token_bytes(16)


def new_token():
    """Unguessable session token."""
    return secrets.token_urlsafe(32)


def is_expired(created_at, now=None):
    """True once a session is older than SESSION_TTL seconds."""
    now = time.time() if now is None else now
    return now - created_at > SESSION_TTL


def build_query(username):
    """Parameterised user lookup."""
    return "SELECT id, hash FROM users WHERE name = ?", (username,)


def is_admin(user):
    """Only active admins count."""
    return user.get("role") == "admin" and user.get("active", False)
''',
	"storage.py": '''\
"""File-backed storage."""
import json
import os
import tempfile


def save_json(path, data):
    """Atomically write data as JSON."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def load_json(path, default=None):
    """Load JSON, returning default only when the file is missing."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def read_upload(base_dir, name):
    """Read a file below base_dir; reject paths that escape it."""
    full = os.path.realpath(os.path.join(base_dir, name))
    if not full.startswith(os.path.realpath(base_dir) + os.sep):
        raise ValueError("path escapes base directory")
    with open(full, "rb") as handle:
        return handle.read()


def read_lines(path):
    """Return the file's lines without trailing newlines."""
    with open(path, encoding="utf-8") as handle:
        return [line.rstrip("\\n") for line in handle]
''',
	"utils.py": '''\
"""General utilities."""
import time


def retry(func, attempts=3, delay=0.5):
    """Call func, retrying on OSError with exponential backoff."""
    last = None
    for attempt in range(attempts):
        try:
            return func()
        except OSError as exc:
            last = exc
            time.sleep(delay * (2 ** attempt))
    raise last


def chunked(seq, size):
    """Split seq into lists of at most size items."""
    if size <= 0:
        raise ValueError("size must be positive")
    return [seq[i:i + size] for i in range(0, len(seq), size)]


class LRUCache:
    """Least-recently-used cache."""

    def __init__(self, capacity):
        self.capacity = capacity
        self.data = {}

    def get(self, key):
        if key not in self.data:
            return None
        value = self.data.pop(key)
        self.data[key] = value
        return value

    def put(self, key, value):
        self.data.pop(key, None)
        self.data[key] = value
        if len(self.data) > self.capacity:
            self.data.pop(next(iter(self.data)))


def merge(a, b):
    """Return a new dict with b's entries layered over a's."""
    result = dict(a)
    result.update(b)
    return result


def parse_bool(text):
    """Interpret common truthy strings."""
    return text.strip().lower() in {"1", "true", "yes", "on"}


def clamp(value, low, high):
    return max(low, min(value, high))
''',
}


@dataclass(frozen=True)
class Change:
	id: str
	file: str
	func: str  # qualified name of the enclosing function
	old: str
	new: str
	desc: str
	benign: bool = False


BUGS = [
	Change("B01", "inventory.py", "add_item", "if qty <= 0:", "if qty < 0:", "zero quantity accepted"),
	Change("B02", "inventory.py", "low_stock", "if qty < threshold)", "if qty <= threshold)", "threshold boundary off by one"),
	Change("B03", "inventory.py", "paginate", "start = (page - 1) * per_page", "start = page * per_page", "page index off by one"),
	Change("B04", "inventory.py", "total_units", "total += qty", "total = qty", "sum overwritten instead of accumulated"),
	Change(
		"B05", "billing.py", "apply_discount",
		"(Decimal(100) - Decimal(percent))", "(Decimal(100) + Decimal(percent))", "discount increases price",
	),
	Change(
		"B06", "billing.py", "add_tax",
		"(price * (1 + TAX_RATE))", "(price * TAX_RATE)", "returns only the tax amount",
	),
	Change(
		"B07", "billing.py", "split_bill",
		"shares[0] += total - share * people", "shares[0] += share * people - total", "remainder sign flipped",
	),
	Change(
		"B08", "billing.py", "average_order",
		'    if not orders:\n        return Decimal("0")\n', "", "empty list guard removed (ZeroDivisionError)",
	),
	Change(
		"B09", "billing.py", "invoice_total",
		"sum(price * qty for price, qty in lines)", "sum(price for price, qty in lines)", "quantity ignored",
	),
	Change(
		"B10", "auth.py", "verify_password",
		"hmac.compare_digest(hash_password(password, salt), expected)", "hash_password(password, salt) == expected",
		"non-constant-time comparison",
	),
	Change(
		"B11", "auth.py", "new_token",
		"secrets.token_urlsafe(32)", "str(int(time.time() * 1000))", "predictable session token",
	),
	Change("B12", "auth.py", "is_expired", "now - created_at > SESSION_TTL", "now - created_at < SESSION_TTL", "expiry check inverted"),
	Change(
		"B13", "auth.py", "build_query",
		'"SELECT id, hash FROM users WHERE name = ?", (username,)',
		'f"SELECT id, hash FROM users WHERE name = \'{username}\'", ()', "SQL injection",
	),
	Change(
		"B14", "auth.py", "is_admin",
		'== "admin" and user.get("active", False)', '== "admin" or user.get("active", False)', "and/or swapped",
	),
	Change("B15", "auth.py", "hash_password", "salt, 200_000)", "salt, 1)", "PBKDF2 iterations reduced to 1"),
	Change(
		"B16", "storage.py", "read_upload",
		'    if not full.startswith(os.path.realpath(base_dir) + os.sep):\n        raise ValueError("path escapes base directory")\n',
		"", "path traversal check removed",
	),
	Change("B17", "storage.py", "load_json", "except FileNotFoundError:", "except Exception:", "swallows all errors"),
	Change(
		"B18", "storage.py", "read_lines",
		'    with open(path, encoding="utf-8") as handle:\n        return [line.rstrip("\\n") for line in handle]',
		'    handle = open(path, encoding="utf-8")\n    return [line.rstrip("\\n") for line in handle]',
		"file handle never closed",
	),
	Change("B19", "utils.py", "chunked", "seq[i:i + size]", "seq[i:i + size - 1]", "chunks one item short"),
	Change(
		"B20", "utils.py", "LRUCache.get",
		"        value = self.data.pop(key)\n        self.data[key] = value\n        return value",
		"        return self.data[key]", "recency not updated on get",
	),
	Change("B21", "utils.py", "LRUCache.put", "if len(self.data) > self.capacity:", "if len(self.data) >= self.capacity:", "evicts at capacity"),
	Change("B22", "utils.py", "retry", "for attempt in range(attempts):", "for attempt in range(attempts - 1):", "one attempt too few"),
	Change("B23", "utils.py", "merge", "result = dict(a)", "result = a", "mutates first argument"),
	Change("B24", "utils.py", "parse_bool", '{"1", "true", "yes", "on"}', '{"1", "0", "true", "yes", "on"}', '"0" parsed as true'),
]

BENIGN = [
	Change(
		"N1", "inventory.py", "remove_item",
		"    current = items.get(name, 0)\n    if qty > current:\n        raise ValueError(\"insufficient stock\")\n    items[name] = current - qty",
		"    on_hand = items.get(name, 0)\n    if qty > on_hand:\n        raise ValueError(\"insufficient stock\")\n    items[name] = on_hand - qty",
		"variable rename", benign=True,
	),
	Change(
		"N2", "billing.py", "format_money",
		"def format_money(amount):\n", 'def format_money(amount):\n    """Format amount as dollars with two decimals."""\n',
		"docstring added", benign=True,
	),
	Change(
		"N3", "auth.py", "new_salt",
		"def new_salt():\n    \"\"\"Random per-user salt.\"\"\"\n    return secrets.token_bytes(16)",
		"def new_salt(size=16):\n    \"\"\"Random per-user salt.\"\"\"\n    return secrets.token_bytes(size)",
		"size parameter with same default", benign=True,
	),
	Change(
		"N4", "storage.py", "save_json",
		"json.dump(data, handle)", "json.dump(data, handle, indent=2, sort_keys=True)", "pretty-printed output", benign=True,
	),
	Change(
		"N5", "utils.py", "clamp",
		"def clamp(value, low, high):\n",
		'def clamp(value: float, low: float, high: float) -> float:\n    """Limit value to the inclusive range [low, high]."""\n',
		"type hints and docstring", benign=True,
	),
]


def apply_changes(changes: list[Change]) -> dict[str, str]:
	files = dict(BASELINE)
	for change in changes:
		if files[change.file].count(change.old) != 1:
			raise ValueError(f"{change.id}: snippet must occur exactly once in {change.file}")
		files[change.file] = files[change.file].replace(change.old, change.new)
	return files


def function_span(source: str, qualname: str) -> tuple[int, int]:
	"""1-based inclusive line range of a function or method."""
	tree = ast.parse(source)
	parts = qualname.split(".")
	scope = tree.body
	node = None
	for part in parts:
		node = next(n for n in scope if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == part)
		scope = node.body
	return node.lineno, node.end_lineno


def git(repo: Path, *args: str) -> None:
	subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def build_repo(directory: Path, changes: list[Change]) -> dict[str, str]:
	for name, content in BASELINE.items():
		(directory / name).write_text(content, encoding="utf-8", newline="\n")
	git(directory, "init", "-q")
	git(directory, "config", "user.email", "eval@example.invalid")
	git(directory, "config", "user.name", "eval")
	git(directory, "config", "commit.gpgsign", "false")
	git(directory, "add", "-A")
	git(directory, "commit", "-q", "-m", "baseline")
	modified = apply_changes(changes)
	for name, content in modified.items():
		(directory / name).write_text(content, encoding="utf-8", newline="\n")
	return modified


def file_diffs(directory: Path) -> dict[str, str]:
	diff = filter_diff(get_git_diff(mode="worktree", target="all", repo_root=directory), "all")
	result = {}
	for section in split_diff_sections(diff):
		first = section.splitlines()[0]  # "diff --git a/x b/x"
		result[first.split(" b/")[-1]] = section
	return result


def review_batches() -> list[list[Change]]:
	batches = []
	for name in sorted(BASELINE):
		bugs = [change for change in BUGS if change.file == name]
		benign = [change for change in BENIGN if change.file == name]
		for offset in range(0, len(bugs), 5):
			batches.append(bugs[offset:offset + 5] + (benign if offset == 0 else []))
	return batches


def classify(findings: list[dict], modified: dict[str, str], changes: list[Change] | None = None) -> dict:
	"""Match findings to seeded changes by file and enclosing-function line range."""
	changes = BUGS + BENIGN if changes is None else changes
	bugs = [change for change in changes if not change.benign]
	benign = [change for change in changes if change.benign]
	spans = {c.id: function_span(modified[c.file], c.func) for c in changes}
	caught: dict[str, list[dict]] = {}
	false_positives = []
	for finding in findings:
		target = None
		for change in bugs:
			if change.file != finding["file"]:
				continue
			lo, hi = spans[change.id]
			line = finding["line"]
			text = " ".join(finding[k] for k in ("title", "evidence", "explanation")).lower()
			if (line is not None and lo <= line <= hi) or (line is None and change.func.split(".")[-1].lower() in text):
				target = change
				break
		if target is None:
			hit_benign = next(
				(c.id for c in benign if c.file == finding["file"] and finding["line"] is not None
				 and spans[c.id][0] <= finding["line"] <= spans[c.id][1]),
				None,
			)
			false_positives.append({**finding, "on_benign_change": hit_benign})
		else:
			caught.setdefault(target.id, []).append(finding)
	return {"caught": caught, "false_positives": false_positives}


def record_response(result: dict, batch: dict, review: object, diff: str, modified: dict[str, str], changes: list[Change]) -> None:
	response = {
		"batch_id": batch["id"], "file": batch["file"], "bug_ids": batch["bug_ids"],
		"review": review, "diff": diff, "source": modified[batch["file"]],
	}
	result["responses"].append(response)
	try:
		validate_review_structure(review)
	except ValueError as exc:
		response["structure_error"] = str(exc)
		return
	response["structure_valid"] = True
	result["detection_reviewed_bug_ids"].extend(batch["bug_ids"])
	for index, finding in enumerate(review["findings"], start=1):
		citation_error = None
		try:
			validate_review({**review, "findings": [finding]}, diff)
		except ValueError as exc:
			citation_error = str(exc)
		candidate = finding if citation_error is None else {**finding, "line": None}
		provisional = classify([candidate], modified, changes)
		result["adjudications"].append({
			"id": f"{batch['id']}:{index}", "batch_id": batch["id"], "finding": finding,
			"citation_status": "invalid" if citation_error else ("file_level" if finding["line"] is None else "valid"),
			"citation_error": citation_error,
			"suggested_bug_id": next(iter(provisional["caught"]), None),
			"verdict": "pending", "bug_id": None,
		})


def detection_summary(result: dict) -> dict:
	rows = result.get("adjudications", [])
	allowed = {batch["id"]: set(batch["bug_ids"]) for batch in result.get("batches", [])}
	expected = {
		f"{response['batch_id']}:{index}": (response["batch_id"], finding)
		for response in result.get("responses", []) if response.get("structure_valid")
		for index, finding in enumerate(response["review"]["findings"], start=1)
	}
	if {row["id"] for row in rows} != set(expected):
		raise ValueError("Adjudications must retain every original finding ID; edit only verdict and bug_id.")
	seen = set()
	for row in rows:
		if row["id"] in seen:
			raise ValueError(f"Duplicate adjudication ID: {row['id']}")
		seen.add(row["id"])
		if (row["batch_id"], row["finding"]) != expected[row["id"]]:
			raise ValueError(f"{row['id']}: original finding evidence must not be changed.")
		if row["verdict"] not in {"pending", "confirmed_bug", "false_positive", "other_issue"}:
			raise ValueError(f"Invalid verdict for {row['id']}: {row['verdict']}")
		if row["verdict"] == "confirmed_bug":
			if row["bug_id"] not in allowed.get(row["batch_id"], set()):
				raise ValueError(f"{row['id']}: bug_id must be a seed from this batch.")
		elif row["bug_id"] is not None:
			raise ValueError(f"{row['id']}: only confirmed_bug verdicts may specify bug_id.")
	reviewed = set(result.get("detection_reviewed_bug_ids", []))
	suggested = {row["suggested_bug_id"] for row in rows if row["suggested_bug_id"] is not None}
	confirmed = {row["bug_id"] for row in rows if row["verdict"] == "confirmed_bug"}
	pending = sum(row["verdict"] == "pending" for row in rows)
	false_positives = sum(row["verdict"] == "false_positive" for row in rows)
	other_issues = sum(row["verdict"] == "other_issue" for row in rows)
	true_findings = sum(row["verdict"] == "confirmed_bug" for row in rows) + other_issues
	complete = bool(reviewed) and pending == 0
	return {
		"scoring": "Function matches are provisional; confirmed metrics require manual adjudication.",
		"reviewed_bugs": len(reviewed), "coverage": round(len(reviewed) / len(BUGS), 3),
		"unreviewed": [bug.id for bug in BUGS if bug.id not in reviewed],
		"provisional_bugs_caught": len(suggested),
		"provisional_recall": round(len(suggested) / len(reviewed), 3) if reviewed else None,
		"unmatched_findings": sum(row["suggested_bug_id"] is None for row in rows),
		"pending_findings": pending, "manual_complete": complete,
		"confirmed_bugs_caught": len(confirmed), "confirmed_false_positives": false_positives,
		"confirmed_other_issues": other_issues,
		"recall": round(len(confirmed) / len(reviewed), 3) if complete else None,
		"precision": round(true_findings / len(rows), 3) if complete and rows else None,
		"missed": [bug.id for bug in BUGS if bug.id in reviewed and bug.id not in confirmed] if complete else [],
	}


def run_eval(model: str) -> dict:
	from openai import OpenAI
	from reviewed.reviewer import ReviewValidationError, call_openai

	client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=60, max_retries=1)
	result = {
		"model": model, "findings": [], "caught": {}, "false_positives": [],
		"reviewed_bug_ids": [], "batches": [], "failures": [],
		"responses": [], "adjudications": [], "detection_reviewed_bug_ids": [],
		"seeds": [asdict(change) for change in BUGS + BENIGN],
	}
	try:
		for index, changes in enumerate(review_batches(), start=1):
			with tempfile.TemporaryDirectory() as tmp:
				directory = Path(tmp)
				modified = build_repo(directory, changes)
				name = changes[0].file
				section = file_diffs(directory)[name]
				batch = {"id": index, "file": name, "bug_ids": [c.id for c in changes if not c.benign]}
				print(f"Reviewing batch {index}: {name} ({', '.join(batch['bug_ids'])}) ...", file=sys.stderr)
				try:
					review = call_openai(client, model, section)
				except Exception as exc:
					failure = {
						**batch, "kind": "validation" if isinstance(exc, ReviewValidationError) else "review",
						"error_type": type(exc).__name__, "error": str(exc),
					}
					if isinstance(exc, ReviewValidationError):
						failure["raw_response"] = exc.raw_response
						record_response(result, batch, exc.review, section, modified, changes)
					result["failures"].append(failure)
					result["batches"].append({**batch, "status": "failed"})
					print(f"Batch {index} failed ({failure['kind']}): {exc}", file=sys.stderr)
					continue
				record_response(result, batch, review, section, modified, changes)
				findings = review["findings"]
				scored = classify(findings, modified, changes)
				result["findings"].extend(findings)
				result["caught"].update(scored["caught"])
				result["false_positives"].extend(scored["false_positives"])
				result["reviewed_bug_ids"].extend(batch["bug_ids"])
				result["batches"].append({**batch, "status": "completed"})
	finally:
		client.close()
	return result


def summarize(result: dict) -> dict:
	caught_ids = set(result["caught"])
	total = len(BUGS)
	reviewed_ids = set(result["reviewed_bug_ids"])
	false_positives = result["false_positives"]
	duplicates = sum(len(v) - 1 for v in result["caught"].values())
	true_findings = sum(len(v) for v in result["caught"].values())
	rows = result.get("adjudications", [])
	invalid = sum(row["citation_status"] == "invalid" for row in rows)
	file_level = sum(row["citation_status"] == "file_level" for row in rows)
	numeric = sum(row["finding"]["line"] is not None for row in rows)
	valid_numeric = sum(row["citation_status"] == "valid" for row in rows)
	return {
		"model": result["model"],
		"scoring": "Strict validated reviews only; location-based matches are provisional.",
		"citation_validity": {
			"total_findings": len(rows), "valid_findings": len(rows) - invalid,
			"invalid_findings": invalid, "file_level_findings": file_level,
			"valid_rate": round((len(rows) - invalid) / len(rows), 3) if rows else None,
			"numeric_valid_rate": round(valid_numeric / numeric, 3) if numeric else None,
		},
		"detection": detection_summary(result),
		"seeded_bugs": total,
		"reviewed_bugs": len(reviewed_ids),
		"unreviewed": [b.id for b in BUGS if b.id not in reviewed_ids],
		"coverage": round(len(reviewed_ids) / total, 3),
		"failed_batches": len(result["failures"]),
		"validation_failures": sum(f["kind"] == "validation" for f in result["failures"]),
		"bugs_caught": len(caught_ids),
		"recall": round(len(caught_ids) / len(reviewed_ids), 3) if reviewed_ids else None,
		"missed": [b.id for b in BUGS if b.id in reviewed_ids and b.id not in caught_ids],
		"total_findings": len(result["findings"]),
		"false_positives": len(false_positives),
		"false_positives_on_benign_edits": sum(1 for f in false_positives if f["on_benign_change"]),
		"duplicate_findings_on_same_bug": duplicates,
		"precision": round(true_findings / len(result["findings"]), 3) if result["findings"] else None,
	}


def print_report(result: dict, summary: dict) -> None:
	print("\nStrict validated results (provisional function matching)")
	for bug in BUGS:
		status = "unreviewed" if bug.id not in result["reviewed_bug_ids"] else (
			"CAUGHT" if bug.id in result["caught"] else "missed"
		)
		print(f"  {bug.id} {status:6} {bug.file}:{bug.func}  ({bug.desc})")
	print("\nFalse positives")
	for fp in result["false_positives"] or [None]:
		if fp is None:
			print("  none")
		else:
			where = f" [benign edit {fp['on_benign_change']}]" if fp["on_benign_change"] else ""
			print(f"  {fp['file']}:{fp['line']} {fp['severity']}: {fp['title']}{where}")
	print("\nFailed batches")
	for failure in result["failures"]:
		print(f"  Batch {failure['id']} ({failure['file']}), {failure['kind']}: {failure['error']}")
	if not result["failures"]:
		print("  none")
	print("\nDetection candidates (independent of citation validity)")
	for row in result.get("adjudications", []):
		finding = row["finding"]
		print(f"  {row['id']} {finding['title']} | citation={row['citation_status']} | suggested={row['suggested_bug_id']} | verdict={row['verdict']}")
	print("\nSummary (strict recall excludes rejected batches; detection is reported separately)")
	print(json.dumps(summary, indent=2))


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	mode = parser.add_mutually_exclusive_group()
	mode.add_argument("--check", action="store_true", help="Validate seeds offline; no API calls.")
	mode.add_argument("--rescore", type=Path, help="Score a saved result after editing adjudications; no API calls.")
	parser.add_argument("--output", type=Path, help="Write full results as JSON.")
	args = parser.parse_args()

	if args.check:
		with tempfile.TemporaryDirectory() as tmp:
			modified = build_repo(Path(tmp), BUGS + BENIGN)
			diffs = file_diffs(Path(tmp))
		for change in BUGS + BENIGN:
			function_span(modified[change.file], change.func)
		for name in BASELINE:
			ast.parse(modified[name])
		batches = review_batches()
		for changes in batches:
			batch_source = apply_changes(changes)
			for change in changes:
				function_span(batch_source[change.file], change.func)
		print(f"OK: {len(BUGS)} bugs, {len(BENIGN)} benign edits, {len(diffs)} changed files, {len(batches)} review batches.")
		return 0

	if args.rescore:
		result = json.loads(args.rescore.read_text(encoding="utf-8"))
		if not {"responses", "adjudications", "detection_reviewed_bug_ids"} <= result.keys():
			parser.error("This result predates response preservation; rerun the live eval first.")
	else:
		from dotenv import find_dotenv, load_dotenv
		from reviewed.reporting import DATA_DIR

		load_dotenv(DATA_DIR / ".env")
		load_dotenv(find_dotenv(usecwd=True))
		model = os.getenv("OPENAI_MODEL", "").strip()
		if not model or not os.getenv("OPENAI_API_KEY", "").strip():
			print("OPENAI_MODEL and OPENAI_API_KEY must be configured.", file=sys.stderr)
			return 1
		result = run_eval(model)
	try:
		summary = summarize(result)
	except ValueError as exc:
		parser.error(str(exc))
	print_report(result, summary)
	if args.output:
		args.output.write_text(json.dumps({"summary": summary, **result}, indent=2), encoding="utf-8")
	return 1 if result["failures"] else 0


if __name__ == "__main__":
	raise SystemExit(main())
