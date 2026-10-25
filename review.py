#!/usr/bin/env python3
"""Compatibility entrypoint for the modular AI code reviewer."""

from cli import main
from git_diff import (
	MAX_DIFF_BYTES,
	MAX_DIFF_CHARS,
	REPO_ROOT,
	TARGET_PATHS,
	changed_lines_by_file,
	filter_diff,
	get_git_diff,
	get_repo_root,
	is_in_scope,
	is_sensitive_path,
	paths_in_diff_section,
	read_diff_file,
	untracked_file_to_diff,
	untracked_files,
	warn_on_exposed_secret_files,
)
from reporting import make_artifact_paths, write_artifact
from reviewer import call_openai, log_usage_and_cost
from validation import REVIEW_SCHEMA, validate_review

if __name__ == "__main__":
	raise SystemExit(main())