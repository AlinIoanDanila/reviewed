"""Validate structured model reviews against the supplied diff."""

from .git_diff import changed_lines_by_file

REVIEW_SCHEMA = {
	"type": "object",
	"properties": {
		"summary": {"type": "string"},
		"risk": {"type": "string", "enum": ["low", "medium", "high"]},
		"findings": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
					"file": {"type": "string"},
					"line": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]},
					"title": {"type": "string"},
					"evidence": {"type": "string"},
					"explanation": {"type": "string"},
					"recommendation": {"type": "string"},
					"confidence": {"type": "string", "enum": ["low", "medium", "high"]},
				},
				"required": ["severity", "file", "line", "title", "evidence", "explanation", "recommendation", "confidence"],
				"additionalProperties": False,
			},
		},
	},
	"required": ["summary", "risk", "findings"],
	"additionalProperties": False,
}


def validate_review_structure(result: object) -> dict:
	"""Validate review fields and types without accepting its file/line references."""
	if not isinstance(result, dict):
		raise ValueError("Review must be a JSON object.")
	if set(result) != {"summary", "risk", "findings"}:
		raise ValueError("Review has missing or unexpected fields.")
	if not isinstance(result.get("summary"), str):
		raise ValueError("Review summary must be a string.")
	if not isinstance(result.get("risk"), str) or result["risk"] not in {"low", "medium", "high"}:
		raise ValueError("Invalid overall risk level.")
	findings = result.get("findings")
	if not isinstance(findings, list):
		raise ValueError("Findings must be an array.")
	required = {"severity", "file", "line", "title", "evidence", "explanation", "recommendation", "confidence"}
	valid_severities = {"low", "medium", "high", "critical"}
	valid_confidence = {"low", "medium", "high"}
	for index, finding in enumerate(findings):
		if not isinstance(finding, dict):
			raise ValueError(f"Finding {index} must be an object.")
		if set(finding) != required:
			raise ValueError(f"Finding {index} has missing or unexpected fields.")
		for field in required - {"line"}:
			if not isinstance(finding[field], str):
				raise ValueError(f"Finding {index}: {field} must be a string.")
		if finding["severity"] not in valid_severities:
			raise ValueError(f"Finding {index} has invalid severity.")
		if finding["confidence"] not in valid_confidence:
			raise ValueError(f"Finding {index} has invalid confidence.")
		line = finding["line"]
		if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
			raise ValueError(f"Finding {index} has an invalid line number.")
	return result


def validate_review(result: object, diff: str) -> dict:
	"""Validate the model response and check its file/line references."""
	result = validate_review_structure(result)
	changed_lines = changed_lines_by_file(diff)
	for index, finding in enumerate(result["findings"]):
		line = finding["line"]
		file = finding["file"]
		if file not in changed_lines:
			raise ValueError(f"Finding {index} references a file absent from the diff.")
		if line is not None and line not in changed_lines[file]:
			raise ValueError(
				f"Finding {index} ({file}:{line}) references a line that is not an added line in the diff. "
				f"Valid added lines: {sorted(changed_lines[file])}. "
				"Use null for a file-level finding."
			)
	return result
