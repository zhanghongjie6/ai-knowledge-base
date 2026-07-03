"""Validate knowledge entry JSON files against the project schema.

Usage:
    python hooks/validate_json.py <json_file> [json_file2 ...]
    python hooks/validate_json.py knowledge/articles/*.json

Exit code 0 on success, 1 on failure.
"""

import argparse
import json
import re
import sys
from pathlib import Path


REQUIRED_FIELDS: dict[str, type] = {
    "id": str,
    "title": str,
    "source_url": str,
    "summary": str,
    "tags": list,
    "status": str,
}

VALID_STATUSES = {"draft", "review", "published", "archived"}

VALID_AUDIENCES = {"beginner", "intermediate", "advanced"}

# Accepts both sequential (github-20260504-001) and UUID v4 formats.
ID_PATTERN = re.compile(r"^(?:[a-z][a-z0-9_]*-\d{8}-\d{3}|[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12})$")

URL_PATTERN = re.compile(r"^https?://\S+")  # noqa: W605


def validate_file(filepath: Path) -> list[str]:
    """Validate a single JSON file, returning a list of error messages."""
    errors: list[str] = []

    # 1. JSON parse
    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"Invalid JSON: {e}"]
    except Exception as e:
        return [f"Read error: {e}"]

    if not isinstance(data, dict):
        return ["Root value must be a JSON object"]

    # 2. Required fields & types
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in data:
            errors.append(f"Missing required field: '{field}'")
            continue
        value = data[field]
        if not isinstance(value, expected_type):
            errors.append(
                f"Field '{field}' must be {expected_type.__name__}, "
                f"got {type(value).__name__}"
            )

    # 3. ID format (only if present and is str)
    if isinstance(data.get("id"), str):
        if not ID_PATTERN.match(data["id"]):
            errors.append(
                f"Field 'id' must match pattern {{source}}-{{YYYYMMDD}}-{{NNN}} "
                f"(e.g. github-20260317-001), got '{data['id']}'"
            )

    # 4. Status (only if present and is str)
    if isinstance(data.get("status"), str) and data["status"] not in VALID_STATUSES:
        valid_list = ", ".join(sorted(VALID_STATUSES))
        errors.append(
            f"Field 'status' must be one of {{{valid_list}}}, "
            f"got '{data['status']}'"
        )

    # 5. URL format
    if isinstance(data.get("source_url"), str):
        if not URL_PATTERN.match(data["source_url"]):
            errors.append(
                f"Field 'source_url' must be a valid http(s) URL, "
                f"got '{data['source_url']}'"
            )

    # 6. Summary length
    if isinstance(data.get("summary"), str) and len(data["summary"]) < 20:
        errors.append(
            f"Field 'summary' must be at least 20 characters, "
            f"got {len(data['summary'])}"
        )

    # 7. Tags count
    if isinstance(data.get("tags"), list) and len(data["tags"]) < 1:
        errors.append(
            f"Field 'tags' must contain at least 1 tag, got 0"
        )

    # 8. Optional: score (1-10)
    if "score" in data:
        score = data["score"]
        if not isinstance(score, (int, float)) or not (1 <= score <= 10):
            errors.append(
                f"Optional field 'score' must be a number between 1 and 10, "
                f"got {score!r}"
            )

    # 9. Optional: audience
    if "audience" in data:
        audience = data["audience"]
        if audience not in VALID_AUDIENCES:
            valid_list = ", ".join(sorted(VALID_AUDIENCES))
            errors.append(
                f"Optional field 'audience' must be one of {{{valid_list}}}, "
                f"got '{audience}'"
            )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate knowledge entry JSON files",
    )
    parser.add_argument(
        "files",
        metavar="FILE",
        nargs="+",
        type=str,
        help="JSON file(s) to validate (supports glob patterns)",
    )
    args = parser.parse_args()

    all_errors: dict[str, list[str]] = {}
    total_files = 0
    valid_files = 0
    invalid_files = 0

    for pattern in args.files:
        matched = list(Path().resolve().glob(pattern)) if ("*" in pattern or "?" in pattern) else [Path(pattern).resolve()]
        if not matched:
            print(f"Warning: no files matched '{pattern}'", file=sys.stderr)
            continue

        for filepath in matched:
            if filepath.name == "index.json":
                continue
            total_files += 1
            errors = validate_file(filepath)
            if errors:
                all_errors[str(filepath)] = errors
                invalid_files += 1
            else:
                valid_files += 1

    # Print results
    if all_errors:
        print("=" * 60, file=sys.stderr)
        for filepath, errors in all_errors.items():
            print(f"\nFile: {filepath}", file=sys.stderr)
            print("-" * 40, file=sys.stderr)
            for err in errors:
                print(f"  \u2716 {err}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)

    # Summary
    print(
        f"\nSummary: {total_files} file(s), "
        f"{valid_files} valid, "
        f"{invalid_files} invalid",
        file=sys.stderr,
    )

    return 1 if invalid_files else 0


if __name__ == "__main__":
    sys.exit(main())
