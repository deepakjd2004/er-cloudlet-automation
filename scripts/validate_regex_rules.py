#!/usr/bin/env python3
"""
validate_regex_rules.py — Validate regex-based redirect rules using RE2.

Akamai Cloudlets uses RE2 semantics. This script validates that patterns compile
with RE2 and checks sample URL matching behavior.

RE2 syntax reference:
https://github.com/google/re2/wiki/syntax

Accepted input shape (single object or list of objects):
{
  "regex": "(product|sports)",
  "redirectURL": "https://www.example.com/$1",
  "sampleURL": "https://www.example.com/product"
}

Usage:
  # Inline JSON payload
  python scripts/validate_regex_rules.py \
    --payload '{"regex":"(product|sports)","redirectURL":"https://www.example.com/$1","sampleURL":"https://www.example.com/product"}'

  # JSON file containing one object or an array
  python scripts/validate_regex_rules.py --input rules/regex_validation.json

Exit code:
  0 -> all entries valid
  1 -> one or more entries failed validation
  2 -> invalid script usage / invalid JSON / missing RE2 runtime
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _load_re2_module():
    """Load a Python RE2 binding (google-re2), or return None if unavailable."""
    try:
        import re2  # type: ignore

        return re2
    except Exception:
        return None


@dataclass
class RegexValidationInput:
    regex: str
    redirect_url: str
    sample_url: str
    case_sensitive: bool = False


@dataclass

class RuleResult:
    index: int
    regex: str
    sample_url: str
    compiled: bool = False
    sample_matches: bool = False
    capture_groups: int = 0
    redirect_url_valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors


def _normalise_input_obj(raw: dict[str, Any], index: int) -> RegexValidationInput:
    """Map multiple common key spellings to the internal model."""
    key_map = {k.lower(): k for k in raw.keys()}

    def pick(*names: str) -> Any:
        for n in names:
            original = key_map.get(n.lower())
            if original is not None:
                return raw.get(original)
        return None

    regex_value = pick("regex")
    redirect_url = pick("redirectURL", "redirect_url", "redirectUrl")
    sample_url = pick("sampleURL", "sample_url", "sampleUrl")
    case_sensitive = pick("caseSensitive", "case_sensitive")

    if regex_value is None:
        raise ValueError(f"Entry {index}: missing required field 'regex'")
    if redirect_url is None:
        raise ValueError(f"Entry {index}: missing required field 'redirectURL'")
    if sample_url is None:
        raise ValueError(f"Entry {index}: missing required field 'sampleURL'")

    return RegexValidationInput(
        regex=str(regex_value),
        redirect_url=str(redirect_url),
        sample_url=str(sample_url),
        case_sensitive=bool(case_sensitive) if case_sensitive is not None else False,
    )


def parse_payload(payload_text: str) -> list[RegexValidationInput]:
    """Parse payload JSON into a list of regex validation entries."""
    try:
        parsed = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON payload: {exc}") from exc

    if isinstance(parsed, dict):
        return [_normalise_input_obj(parsed, 1)]

    if isinstance(parsed, list):
        if not parsed:
            raise ValueError("Input array is empty")
        entries: list[RegexValidationInput] = []
        for i, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Entry {i}: expected JSON object")
            entries.append(_normalise_input_obj(item, i))
        return entries

    raise ValueError("Top-level JSON must be an object or array of objects")


def _is_reasonable_redirect_url(value: str) -> bool:
    """
    Treat absolute http/https URLs or leading-slash paths as valid redirect targets.
    """
    if value.startswith("/"):
        return True

    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)





def validate_rules(entries: list[RegexValidationInput]) -> list[RuleResult]:
    re2 = _load_re2_module()
    if re2 is None:
        raise RuntimeError(
            "RE2 runtime not found. Install the Python binding first: "
            "pip install google-re2"
        )

    results: list[RuleResult] = []

    for i, entry in enumerate(entries, start=1):
        result = RuleResult(index=i, regex=entry.regex, sample_url=entry.sample_url)

        try:
            options = re2.Options()
            options.case_sensitive = entry.case_sensitive
            pattern = re2.compile(entry.regex, options=options)
            result.compiled = True
        except Exception as exc:
            result.errors.append(f"Regex failed RE2 compilation: {exc}")
            results.append(result)
            continue

        try:
            match = pattern.search(entry.sample_url)
            result.sample_matches = match is not None
            if match is None:
                result.errors.append(
                    "sampleURL does not match the regex pattern"
                )
            else:
                result.capture_groups = len(match.groups())
        except Exception as exc:
            result.errors.append(f"Unable to evaluate sampleURL against regex: {exc}")

        result.redirect_url_valid = _is_reasonable_redirect_url(entry.redirect_url)
        if not result.redirect_url_valid:
            result.errors.append(
                "redirectURL must be absolute http(s) URL or a leading-slash path"
            )



        results.append(result)

    return results


def print_human_report(results: list[RuleResult]) -> None:
    print("\n" + "=" * 76)
    print("  REGEX VALIDATION REPORT (RE2)")
    print("=" * 76)

    for res in results:
        status = "PASS" if res.passed else "FAIL"
        print(f"\n[{status}] Entry {res.index}")
        print(f"  regex      : {res.regex}")
        print(f"  sampleURL  : {res.sample_url}")
        print(f"  compiled   : {res.compiled}")
        print(f"  matched    : {res.sample_matches}")
        print(f"  groups     : {res.capture_groups}")


        if res.errors:
            for err in res.errors:
                print(f"  ERROR      : {err}")
        if res.warnings:
            for warn in res.warnings:
                print(f"  WARNING    : {warn}")

    print("\n" + "=" * 76)


def build_json_report(results: list[RuleResult]) -> dict[str, Any]:
    return {
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed),
        },
        "results": [
            {
                "index": r.index,
                "regex": r.regex,
                "sampleURL": r.sample_url,
                "compiled": r.compiled,
                "sampleMatches": r.sample_matches,
                "captureGroups": r.capture_groups,

                "errors": r.errors,
                "warnings": r.warnings,
            }
            for r in results
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate regex redirect rules using RE2 semantics."
    )
    parser.add_argument(
        "--payload",
        help="Inline JSON payload (single object or array)",
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Path to JSON file (single object or array)",
    )
    parser.add_argument(
        "--json-output",
        action="store_true",
        help="Print machine-readable JSON report instead of human-readable report",
    )

    args = parser.parse_args()

    if bool(args.payload) == bool(args.input):
        print("ERROR: Provide exactly one of --payload or --input", file=sys.stderr)
        sys.exit(2)

    try:
        payload_text = args.payload if args.payload is not None else args.input.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"ERROR: Failed to read input: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        entries = parse_payload(payload_text)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        results = validate_rules(entries)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.json_output:
        print(json.dumps(build_json_report(results), indent=2))
    else:
        print_human_report(results)

    any_failed = any(not r.passed for r in results)
    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
