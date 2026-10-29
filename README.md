# Reviewed

**A local-first Python CLI that reviews Git diffs with an OpenAI model, validates structured findings, and saves reproducible reports.**

Reviewed is designed to assist—not replace—human review. It filters changes, scans the exact outgoing diff with Gitleaks, validates model-reported files and added lines, and stores the reviewed diff beside its JSON report.

## Features

- Review tracked and eligible untracked changes in a Git worktree, staged changes, a branch diff, or a saved diff file.
- Review the whole repository by default, or select the built-in self-review profile.
- Exclude secret-like paths and fail closed if local Gitleaks scanning is missing or fails.
- Request structured JSON findings through the OpenAI Responses API.
- Validate finding fields, severity/confidence values, file references, and added-line references.
- Save timestamped JSON reports and the exact filtered `.diff` sent for review.
- Cache successful validated reviews to avoid duplicate API calls; `--refresh` requests a fresh review.
- Avoid overwriting existing reports by default.
- Run unit tests without making real model requests.

## Requirements

- Python 3.10 or later.
- Git available on `PATH`.
- [Gitleaks](https://github.com/gitleaks/gitleaks) 8.30 or later available as `gitleaks`.
- An OpenAI API key and a model supporting the Responses API and structured JSON output.

Gitleaks is required for every non-empty review, including cache hits. A missing, failed, or timed-out scan blocks the review. Empty diffs exit without an API request or secret scan.

## Installation

Clone this repository:

```bash
git clone https://github.com/AlinIoanDanila/reviewed
cd reviewed
python -m venv .venv
```

Activate the environment:

**Windows PowerShell**

```powershell
.\.venv\Scripts\Activate.ps1
```

**macOS/Linux**

```bash
source .venv/bin/activate
```

Install the package (provides the `reviewed` command; `python -m reviewed` also works):

```bash
python -m pip install -e .
```

Install Gitleaks using its official installation instructions and verify:

```bash
gitleaks version
```

Copy `.env.example` to `~/.reviewed/.env` (or `$REVIEWED_HOME/.env`), then configure:

```dotenv
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-supported-model
```

Keep `.env` private. Never commit credentials or include real secrets in issues or test fixtures.

## Usage

Run the commands from the Git repository you want to review. You can also specify a repository path using `--repo`.

### Review all changes in the current repository

```bash
reviewed --git worktree
```

This reviews tracked changes relative to `HEAD` and eligible untracked files. The default target is `all`, so paths across the repository are considered, subject to sensitive-path exclusions and secret scanning.

### Select a repository explicitly

```bash
reviewed --repo /path/to/project --git worktree
```

### Other review modes

```bash
# Review staged changes
reviewed --git staged

# Review the current branch against a base
reviewed --git branch --base origin/main

# Review a saved Git diff
reviewed --diff-file path/to/change.diff

# Request a fresh model response instead of using the successful cache
reviewed --git worktree --refresh

# Review this tool's own source files
reviewed --git worktree --target self

# Choose an explicit JSON report path (the paired .diff uses the same basename)
reviewed --git staged --output reports/review.json
```

Run `reviewed --help` for all options. `--force` is valid only with an explicit `--output` and permits overwriting both the report and its paired diff.

## Output and cache

By default, reports and cache are stored under `~/.reviewed/reports/` (override the base directory with `REVIEWED_HOME`). Protect that directory because reports and diffs may contain proprietary source code.

Each review produces a timestamped JSON report and the exact filtered diff sent to the model. Cache entries are written only for successful, validated reviews and are revalidated when read. The cache key includes the repository identity, model request, target, and provider configuration, but not the API key. `--refresh` requests a fresh review. Reports, diffs, and cache entries may contain proprietary source code in plaintext; protect them accordingly.

## Security and privacy

- Review only repositories you are authorized to send to the configured model provider.
- Local Gitleaks scans the filtered diff, including deleted and context lines, before it is persisted or sent.
- Secret-like filename exclusions are defense in depth; they cannot detect every confidential value.
- Requests use `store=False`. This disables Responses API application-state storage; it is **not** a zero-retention guarantee. Provider logging and account-level data policies may still apply. See the [OpenAI data controls documentation](https://developers.openai.com/api/docs/guides/your-data).
- Model findings can be wrong, incomplete, or miss real defects. Verify each finding and keep human review mandatory.
- The tool does not edit source files, approve changes, or merge code.

## Tests

Run the tests from this repository's root:

```bash
python -B -m unittest discover -s tests -v
```

Tests stub OpenAI calls and do not make live model requests. Gitleaks integration tests run when Gitleaks is available. Tests that require OS permissions to create symlinks may be skipped in restricted environments.

## Evaluation

Example evaluation: In one run using gpt-6-luna, the reviewer returned 23 findings for a benchmark of 24 seeded bugs and 5 benign edits. All 23 findings passed citation validation. Manual inspection found 23 seeded bugs detected, no actual false positives, and one missed bug: B18 (the unclosed file handle). The automatic matcher reported 22 detections and one false positive because it mismatched two findings in billing.py. This is a single synthetic evaluation, not a general performance guarantee.

## Contributing

Issues and pull requests are welcome. Please include regression tests for bug fixes and use synthetic data rather than real secrets or proprietary source code in fixtures.

## License

Licensed under the MIT License. See [LICENSE](LICENSE).
