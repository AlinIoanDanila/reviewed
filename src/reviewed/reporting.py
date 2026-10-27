"""Persist review reports and their exact input diffs."""

import os
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("REVIEWED_HOME") or Path.home() / ".reviewed")
REPORTS_DIR = DATA_DIR / "reports"


def write_artifact(path: Path, content: str, *, force: bool) -> None:
	"""Write an artifact without overwriting an existing file unless requested."""
	mode = "w" if force else "x"
	with path.open(mode, encoding="utf-8", newline="\n") as file:
		file.write(content)


def make_artifact_paths(
	output: Path | None,
	target: str,
	force: bool,
	reports_dir: Path | None = None,
) -> tuple[Path, Path]:
	"""Choose report and matching diff paths without accidental overwrites."""
	if output is not None:
		if output.suffix.lower() != ".json":
			raise ValueError("The --output path must end in .json so its paired diff can use .diff.")
		report_path = output
		diff_path = output.with_suffix(".diff")
		if not force and (report_path.exists() or diff_path.exists()):
			existing = report_path if report_path.exists() else diff_path
			raise ValueError(
				f"Review artifact already exists: {existing}. Choose another --output path "
				"or use --force to overwrite both artifacts."
			)
		return report_path, diff_path
	timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
	destination = reports_dir or REPORTS_DIR
	base = destination / f"{timestamp}-{target}-review"
	report_path = base.with_suffix(".json")
	diff_path = base.with_suffix(".diff")
	counter = 1
	while report_path.exists() or diff_path.exists():
		suffix = f"-{counter:02d}"
		report_path = REPORTS_DIR / f"{timestamp}-{target}-review{suffix}.json"
		diff_path = REPORTS_DIR / f"{timestamp}-{target}-review{suffix}.diff"
		counter += 1
	return report_path, diff_path
