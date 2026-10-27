"""Reuse only successfully persisted and validated reviews."""

import contextlib
import errno
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, Callable, Generator

from .validation import validate_review


def _file_lock_operations(
	platform: str, lock_file: BinaryIO,
) -> tuple[Callable[[], None], Callable[[], None]]:
	"""Return non-blocking lock and unlock operations for the current platform."""
	if platform == "nt":
		import msvcrt

		lock_file.seek(0, os.SEEK_END)
		if lock_file.tell() == 0:
			lock_file.write(b"\0")
			lock_file.flush()
		lock_file.seek(0)

		def acquire() -> None:
			msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)

		def release() -> None:
			lock_file.seek(0)
			msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)

		return acquire, release

	import fcntl

	def acquire() -> None:
		fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

	def release() -> None:
		fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

	return acquire, release


@contextlib.contextmanager
def review_cache_lock(path: Path, *, timeout: float = 180) -> Generator[None, None, None]:
	"""Serialize cache lookup, review creation, and publication across processes."""
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("a+b") as lock_file:
		acquire, release = _file_lock_operations(os.name, lock_file)
		deadline = time.monotonic() + timeout
		while True:
			try:
				acquire()
				break
			except OSError as exc:
				contention_errors = {errno.EACCES, errno.EAGAIN}
				if exc.errno not in contention_errors:
					raise
				if time.monotonic() >= deadline:
					raise TimeoutError("Timed out waiting for the review cache lock.") from None
				time.sleep(0.1)
		try:
			yield
		finally:
			release()


def review_cache_key(
	repo_root: Path, request: Mapping[str, object], *, target: str = "all",
) -> str:
	"""Identify the repository, exact request, and configured provider context."""
	identity = {
		"version": 1,
		"repository": str(repo_root.resolve()),
		"target": target,
		"request": request,
		"provider": {
			name: os.getenv(name, "")
			for name in ("OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID")
		},
	}
	encoded = json.dumps(identity, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
	return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_cached_review(path: Path, diff: str) -> dict | None:
	"""Validate cached content again before returning it to the user."""
	try:
		with path.open(encoding="utf-8") as source:
			result = json.load(source)
	except FileNotFoundError:
		return None
	except (json.JSONDecodeError, UnicodeDecodeError):
		raise ValueError("Invalid review cache entry; use --refresh to replace it.") from None
	try:
		return validate_review(result, diff)
	except ValueError:
		raise ValueError("Invalid review cache entry; use --refresh to replace it.") from None


def save_cached_review(path: Path, review: dict) -> None:
	"""Atomically publish a cache entry after its report has been saved."""
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary_path: Path | None = None
	try:
		with tempfile.NamedTemporaryFile(
			mode="w", encoding="utf-8", newline="\n",
			dir=path.parent, prefix=".review-", suffix=".tmp", delete=False,
		) as destination:
			temporary_path = Path(destination.name)
			json.dump(review, destination, indent=2, ensure_ascii=False)
			destination.write("\n")
		os.replace(temporary_path, path)
	finally:
		if temporary_path is not None:
			temporary_path.unlink(missing_ok=True)
