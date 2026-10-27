"""Fail-closed local secret scanning of outgoing review diffs."""

import subprocess
import tempfile
from pathlib import Path

SECRET_FOUND_EXIT_CODE = 10


def scan_diff_for_secrets(diff: str) -> None:
	"""Scan all diff text without persisting it or exposing scanner output."""
	if not diff.strip():
		return
	with tempfile.TemporaryDirectory(prefix="reviewed-secret-scan-") as directory:
		config = Path(directory) / "gitleaks.toml"
		config.write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
		try:
			result = subprocess.run(
				[
					"gitleaks", "stdin", "--config", str(config),
					"--no-banner", "--no-color", "--redact=100",
					"--ignore-gitleaks-allow", "--exit-code", str(SECRET_FOUND_EXIT_CODE),
					"--log-level", "error",
				],
				input=diff, encoding="utf-8", capture_output=True, check=False,
				cwd=directory, timeout=30,
			)
		except FileNotFoundError:
			raise RuntimeError(
				"Gitleaks is required for non-empty reviews. Install Gitleaks 8.30 or later "
				"and make 'gitleaks' available on PATH; no review was sent."
			) from None
		except subprocess.TimeoutExpired:
			raise RuntimeError("Secret scanning timed out; no review was sent.") from None
		except OSError:
			raise RuntimeError("Could not run the local secret scanner; no review was sent.") from None
	if result.returncode == SECRET_FOUND_EXIT_CODE:
		raise ValueError(
			"Potential secret detected in the review diff. Remove the credential from "
			"added, deleted, or context lines before reviewing. No review was sent."
		)
	if result.returncode != 0:
		raise RuntimeError(
			f"Secret scanning failed (exit code {result.returncode}); no review was sent."
		)
