"""Command-line entrypoint for scoped AI code reviews."""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from .git_diff import MAX_DIFF_CHARS, filter_diff, get_git_diff, get_repo_root, read_diff_file, warn_on_exposed_secret_files
from .reporting import DATA_DIR, REPORTS_DIR, make_artifact_paths, write_artifact
from .review_cache import load_cached_review, review_cache_key, review_cache_lock, save_cached_review
from .reviewer import build_review_request, call_openai
from .secret_scan import scan_diff_for_secrets

load_dotenv(DATA_DIR / ".env")
load_dotenv(find_dotenv(usecwd=True))


def main() -> int:
	parser = argparse.ArgumentParser(description="Review scoped Git diffs using an OpenAI model.")
	source = parser.add_mutually_exclusive_group()
	source.add_argument("--diff-file", type=Path, help="Review a previously saved diff file.")
	source.add_argument(
		"--git", choices=["worktree", "staged", "branch"], default="worktree",
		help="Collect a diff from Git (default: worktree, including untracked files).",
	)
	parser.add_argument("--base", default="origin/main", help="Base branch for --git branch.")
	parser.add_argument(
		"--repo", type=Path, default=Path.cwd(),
		help="Git repository to review (default: current directory or a directory inside it).",
	)
	parser.add_argument("--output", type=Path, help="Explicit path for the JSON report; default is timestamped.")
	parser.add_argument("--force", action="store_true", help="Allow overwriting an explicit report and its .diff file.")
	parser.add_argument(
		"--refresh", action="store_true",
		help="Request a fresh review even when the exact request has a cached successful review.",
	)
	parser.add_argument(
		"--target", choices=("all", "self"), default="all",
		help="Paths to review: all repository paths by default, or the self-review profile.",
	)
	args = parser.parse_args()
	client: OpenAI | None = None
	try:
		if args.force and args.output is None:
			raise ValueError("--force is only valid when you provide an explicit --output path.")
		repo_root = get_repo_root(args.repo)
		warn_on_exposed_secret_files(repo_root)
		if args.diff_file is not None:
			diff = filter_diff(read_diff_file(args.diff_file), args.target)
		else:
			diff = get_git_diff(
				mode=args.git, base=args.base, target=args.target, repo_root=repo_root,
			)
		if not diff.strip():
			print(f"Nothing to review for target '{args.target}'; no API request made.")
			return 0
		if len(diff) > MAX_DIFF_CHARS:
			raise ValueError(f"Diff exceeds the {MAX_DIFF_CHARS}-character limit.")
		scan_diff_for_secrets(diff)
		model = os.getenv("OPENAI_MODEL", "").strip()
		if not model:
			raise ValueError("OPENAI_MODEL is not configured.")
		request = build_review_request(model, diff, args.target)
		key = review_cache_key(repo_root, request, target=args.target)
		cache_path = REPORTS_DIR / ".cache" / f"{key}.json"
		api_key = os.getenv("OPENAI_API_KEY", "").strip()
		with review_cache_lock(cache_path.with_suffix(".lock")):
			review = None if args.refresh else load_cached_review(cache_path, diff)
			if review is None and not api_key:
				raise ValueError("OPENAI_API_KEY is not configured.")
			report_path, diff_path = make_artifact_paths(
				args.output, args.target, args.force,
			)
			report_path.parent.mkdir(parents=True, exist_ok=True)
			diff_path.parent.mkdir(parents=True, exist_ok=True)
			if not args.force and (report_path.exists() or diff_path.exists()):
				raise ValueError("Review artifact path already exists; refusing to overwrite it.")
			write_artifact(diff_path, diff, force=args.force)
			print(f"Review diff saved to {diff_path}", file=sys.stderr)
			from_cache = review is not None
			if review is None:
				client = OpenAI(api_key=api_key, timeout=45, max_retries=1)
				review = call_openai(client, model, diff, target=args.target)
			write_artifact(report_path, json.dumps(review, indent=2, ensure_ascii=False) + "\n", force=args.force)
			if from_cache:
				print("Reused a successful cached review; no API request made.", file=sys.stderr)
			else:
				save_cached_review(cache_path, review)
			print(json.dumps(review, indent=2, ensure_ascii=False))
			print(f"\nReview saved to {report_path}", file=sys.stderr)
		return 0
	except RateLimitError:
		print("AI review failed: API rate limit reached.", file=sys.stderr)
		return 1
	except APITimeoutError:
		print("AI review failed: API request timed out.", file=sys.stderr)
		return 1
	except APIConnectionError:
		print("AI review failed: could not connect to OpenAI.", file=sys.stderr)
		return 1
	except APIStatusError as exc:
		print(f"OpenAI API error: HTTP {exc.status_code}", file=sys.stderr)
		try:
			print(json.dumps(exc.response.json(), indent=2), file=sys.stderr)
		except (ValueError, OSError):
			print("Could not parse error response.", file=sys.stderr)
		return 1
	except (ValueError, OSError, RuntimeError) as exc:
		print(f"AI review failed: {exc}", file=sys.stderr)
		return 1
	finally:
		if client is not None:
			client.close()
