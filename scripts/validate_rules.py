#!/usr/bin/env python3
"""
validate_rules.py — Validate Akamai ER Cloudlet redirect rules before deployment.

Runs 6 checks against the new input CSV combined with the existing live ruleset
(parsed from match-rules.tf), then writes a report to GitHub Step Summary and
sets a GitHub Output flag for regex rule detection.

Usage:
    # Default — auto-discovers the single CSV in rules/ and uses ./match-rules.tf
    python scripts/validate_rules.py

    # Override defaults
    python scripts/validate_rules.py --input path/to/rules.csv
    python scripts/validate_rules.py --current-rules path/to/match-rules.tf
    python scripts/validate_rules.py --operation delete

CSV columns
───────────
Required:
  name                       Rule name (any string — often the RITM/ticket reference)
  redirect_url               Destination URL or path
  status_code                301 | 302 | 303 | 307 | 308

Source — one of the following (mutually exclusive):
  match_url                  Direct URL/path match (simple rule, no matches block)
  match_type + match_value   Criteria for a matches block (complex rule)
  matches_always             true → catch-all rule (no match condition)

matches block (only when using match_type / match_value):
  match_type                 path | regex | extension | hostname | protocol |
                             cookie | query | deviceCharacteristics |
                             countrycode | continent | regioncode |
                             clientip | proxy |
                             header | method | parameter
  match_value                Value to match (regex pattern when match_type=regex;
                             space-separated list for multi-value types)
  match_operator             equals (default) | contains | exists
  case_sensitive             true | false  (default: false)
  negate                     true | false  (default: false)
  check_ips                  CONNECTING_IP | XFF_HEADERS | BOTH
                             (REQUIRED for clientip/proxy/countrycode/continent/regioncode)
  object_match_value         REQUIRED for header/method/parameter; optional richer
                             form for cookie. Mini-DSL:
                               simple:V1|V2          → type=simple, value=[V1, V2]
                               object:NAME:V1|V2     → type=object, name=NAME,
                                                         options.value=[V1, V2]

Per-match_type field requirements (see MATCH_TYPE_SPEC below for the
machine-readable version used by the validator):
  flat match_value (single)         protocol, deviceCharacteristics, regex
  flat match_value (space-sep ok)   path, extension, hostname, query,
                                    countrycode, continent, regioncode
  flat match_value + check_ips      clientip, proxy, countrycode, continent,
                                    regioncode
  flat match_value (name=value)     cookie, query
  object_match_value REQUIRED       header, method, parameter

Optional top-level:
  use_relative_url           relative_url | copy_scheme_hostname | none  (default: none)
  use_incoming_query_string  true | false  (default: false)
  disabled                   true | false  (default: false)
  start                      Start time epoch seconds  (default: 0)
  end                        End time epoch seconds    (default: 0)
  matches_always             true | false  (default: false)
"""

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Data model — mirrors akamai_cloudlets_edge_redirector_match_rule schema
# ---------------------------------------------------------------------------

@dataclass
class RedirectRule:
    # Top-level rule attributes
    name: str = ""
    redirect_url: str = ""
    status_code: int = 301
    use_relative_url: str = "none"
    use_incoming_query_string: bool = False
    disabled: bool = False
    start: int = 0
    end: int = 0
    matches_always: bool = False

    # Simple rule (no matches block)
    match_url: str = ""

    # matches block attributes (complex / regex rules)
    match_type: str = ""
    match_value: str = ""
    match_operator: str = "equals"   # equals | contains | exists | matches
    case_sensitive: bool = False
    negate: bool = False
    check_ips: str = ""

    @property
    def source_path(self) -> str:
        """
        The path value used for loop detection and duplicate checking.
        Only meaningful for path-based rules.
        """
        if self.match_url:
            return self.match_url
        if self.match_type == "path" and self.match_value:
            return self.match_value
        return ""

    @property
    def is_regex(self) -> bool:
        return self.match_type.lower() == "regex"

    @property
    def is_simple(self) -> bool:
        return bool(self.match_url)

    @property
    def is_catch_all(self) -> bool:
        return self.matches_always


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------

REQUIRED_CSV_COLUMNS = {"name", "redirect_url", "status_code"}

VALID_STATUS_CODES = {301, 302, 303, 307, 308}
VALID_USE_RELATIVE_URL = {"relative_url", "copy_scheme_hostname", "none", ""}
# Per the ER schema, only equals|contains|exists are valid match_operators.
# Regex matching is expressed as match_type="regex" (the pattern goes in match_value).
VALID_MATCH_OPERATORS = {"equals", "contains", "exists"}
VALID_CHECK_IPS = {"CONNECTING_IP", "XFF_HEADERS", "BOTH", ""}

# Per-match_type field requirements. Drives validator messages so authors get
# targeted feedback ("method requires object_match_value") instead of opaque
# API errors at deploy time.
#
# Keys:
#   omv(object_match_value)         'required' | 'optional' | 'forbidden'
#                  - required: object_match_value MUST be set; flat match_value
#                              is ignored (and typically rejected by the API).
#                  - optional: either flat match_value OR object_match_value works.
#                  - forbidden: object_match_value must be empty.
#   check_ips   'required' | 'ignored'
#                  - required: a non-empty check_ips MUST be set.
#                  - ignored:  check_ips has no effect for this type.
#   value_hint  Human-readable hint about how to format match_value for this type.
MATCH_TYPE_SPEC = {
    "path":                  {"omv": "forbidden", "check_ips": "ignored",  "value_hint": "/path or '/p1 /p2' (space-separated for multi-value)"},
    "regex":                 {"omv": "forbidden", "check_ips": "ignored",  "value_hint": "RE2 regex pattern, e.g. ^/foo/([a-z]+)$"},
    "extension":             {"omv": "forbidden", "check_ips": "ignored",  "value_hint": "'jpg' or 'jpg png' (space-separated)"},
    "hostname":              {"omv": "forbidden", "check_ips": "ignored",  "value_hint": "'host.com' or 'a.com b.com' (space-separated)"},
    "protocol":              {"omv": "forbidden", "check_ips": "ignored",  "value_hint": "'http' or 'https'"},
    "cookie":                {"omv": "optional", "check_ips": "ignored",  "value_hint": "'name=value' (flat) or object:NAME:V1|V2 (omv)"},
    "query":                 {"omv": "forbidden", "check_ips": "ignored",  "value_hint": "'name=value' (multi values space-separated)"},
    "deviceCharacteristics": {"omv": "forbidden", "check_ips": "ignored",  "value_hint": "characteristic name, e.g. 'accept_third_party_cookie'"},
    "countrycode":           {"omv": "forbidden", "check_ips": "required", "value_hint": "ISO code, e.g. 'AU' or 'AU NZ' (space-separated)"},
    "continent":             {"omv": "forbidden", "check_ips": "required", "value_hint": "continent code, e.g. 'OC' or 'OC AS'"},
    "regioncode":            {"omv": "forbidden", "check_ips": "required", "value_hint": "region code, e.g. 'NSW' or 'NSW VIC'"},
    "clientip":              {"omv": "forbidden", "check_ips": "required", "value_hint": "IP or CIDR, e.g. '1.1.1.1' or '1.1.1.1 2.2.2.2/24'"},
    "proxy":                 {"omv": "forbidden", "check_ips": "required", "value_hint": "'anonymous', 'transparent', or 'none'"},
    "header":                {"omv": "required",  "check_ips": "ignored",  "value_hint": "object_match_value=object:HEADER-NAME:V1|V2"},
    "method":                {"omv": "required",  "check_ips": "ignored",  "value_hint": "object_match_value=simple:GET|POST"},
    "parameter":             {"omv": "required",  "check_ips": "ignored",  "value_hint": "object_match_value=object:PARAM-NAME:V1|V2"},
}
VALID_MATCH_TYPES = set(MATCH_TYPE_SPEC.keys())


def _bool(val: str, default: bool = False) -> bool:
    return val.lower() == "true" if val else default


def _int(val: str, default: int = 0) -> int:
    try:
        return int(val) if val else default
    except ValueError:
        return default


def parse_csv(path: Path) -> list[RedirectRule]:
    """Parse the input CSV file into a list of RedirectRule objects."""
    rules: list[RedirectRule] = []

    with path.open(newline="", encoding="utf-8-sig") as fh:
        # Strip comment lines (lines starting with #) before passing to DictReader
        lines = [
            line for line in fh
            if not line.lstrip().startswith("#")
        ]

    reader = csv.DictReader(lines)

    if reader.fieldnames is None:
        sys.exit("ERROR: CSV file is empty or has no header row.")

    normalised_fields = {f.strip().lower() for f in reader.fieldnames}
    missing = REQUIRED_CSV_COLUMNS - normalised_fields
    if missing:
        sys.exit(
            f"ERROR: CSV is missing required columns: {', '.join(sorted(missing))}"
        )

    for line_num, raw_row in enumerate(reader, start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in raw_row.items() if k}

        name = row.get("name", "")
        redirect_url = row.get("redirect_url", "")
        match_url = row.get("match_url", "")
        match_type = row.get("match_type", "")
        match_value = row.get("match_value", "")
        match_operator = (row.get("match_operator") or "equals").lower()
        check_ips = row.get("check_ips", "")
        use_relative_url = row.get("use_relative_url", "none")
        matches_always = _bool(row.get("matches_always", ""))
        object_match_value = row.get("object_match_value", "")

        # Auto-promote legacy `match_operator=matches` rules to the schema-compliant
        # form: match_type="regex", match_operator="equals". The Akamai ER schema
        # only allows equals|contains|exists for match_operator.
        if match_operator == "matches":
            print(
                f"  WARNING line {line_num}: match_operator='matches' is not in the ER schema. "
                f"Promoting to match_type='regex', match_operator='equals'. "
                f"Update the CSV to remove this warning."
            )
            match_type = "regex"
            match_operator = "equals"

        # Validate match_operator
        if match_operator not in VALID_MATCH_OPERATORS:
            print(
                f"  WARNING line {line_num}: unknown match_operator "
                f"'{match_operator}' — defaulting to 'equals'."
            )
            match_operator = "equals"

        # Validate status_code
        status_code = _int(row.get("status_code", ""), 301)
        if status_code not in VALID_STATUS_CODES:
            print(
                f"  WARNING line {line_num}: status_code {status_code} is not in "
                f"{sorted(VALID_STATUS_CODES)} — included but flagged."
            )

        # Validate use_relative_url
        if use_relative_url not in VALID_USE_RELATIVE_URL:
            print(
                f"  WARNING line {line_num}: use_relative_url '{use_relative_url}' "
                f"is not one of relative_url | copy_scheme_hostname | none."
            )

        # Require at least one source definition
        if (
            not match_url
            and not (match_type and (match_value or object_match_value))
            and not matches_always
        ):
            print(
                f"  WARNING line {line_num}: no source defined "
                f"(set match_url, or match_type+match_value/object_match_value, "
                f"or matches_always=true) — row skipped."
            )
            continue

        if not redirect_url:
            print(f"  WARNING line {line_num}: redirect_url is empty — row skipped.")
            continue

        # match_url and match_type/match_value are mutually exclusive
        if match_url and (match_type or match_value):
            print(
                f"  WARNING line {line_num}: match_url and match_type/match_value "
                f"are mutually exclusive — match_url takes precedence."
            )
            match_type = ""
            match_value = ""

        # Per-match_type schema checks (complex rules only)
        if match_type:
            spec = MATCH_TYPE_SPEC.get(match_type)
            if spec is None:
                print(
                    f"  WARNING line {line_num}: unknown match_type '{match_type}'. "
                    f"Valid types: {', '.join(sorted(VALID_MATCH_TYPES))}."
                )
            else:
                # object_match_value contract
                if spec["omv"] == "required" and not object_match_value:
                    print(
                        f"  ERROR line {line_num}: match_type='{match_type}' REQUIRES "
                        f"object_match_value. Hint: {spec['value_hint']}."
                    )
                elif spec["omv"] == "required" and match_value:
                    print(
                        f"  WARNING line {line_num}: match_type='{match_type}' uses "
                        f"object_match_value; flat match_value='{match_value}' will be "
                        f"ignored and may cause API rejection."
                    )
                elif spec["omv"] == "forbidden" and object_match_value:
                    print(
                        f"  WARNING line {line_num}: match_type='{match_type}' does "
                        f"not support object_match_value; use flat match_value instead. "
                        f"Hint: {spec['value_hint']}."
                    )

                # check_ips contract
                if spec["check_ips"] == "required" and not check_ips:
                    print(
                        f"  ERROR line {line_num}: match_type='{match_type}' REQUIRES "
                        f"check_ips (CONNECTING_IP | XFF_HEADERS | BOTH)."
                    )
                elif spec["check_ips"] == "ignored" and check_ips:
                    print(
                        f"  WARNING line {line_num}: match_type='{match_type}' ignores "
                        f"check_ips='{check_ips}' — column will have no effect."
                    )

                # protocol value sanity check
                if match_type == "protocol" and match_value and match_value.lower() not in {"http", "https"}:
                    print(
                        f"  WARNING line {line_num}: match_type='protocol' expects "
                        f"'http' or 'https', got '{match_value}'."
                    )

        # check_ips value sanity (always check format if set)
        if check_ips and check_ips not in VALID_CHECK_IPS:
            print(
                f"  WARNING line {line_num}: check_ips='{check_ips}' is not one of "
                f"CONNECTING_IP | XFF_HEADERS | BOTH."
            )

        # Simple-rule / catch-all field hygiene: flag ignored columns
        if match_url and (object_match_value or check_ips):
            print(
                f"  WARNING line {line_num}: simple rule (match_url set) ignores "
                f"object_match_value/check_ips — columns will have no effect."
            )
        if matches_always and (match_type or match_value or match_url or object_match_value):
            print(
                f"  WARNING line {line_num}: catch-all (matches_always=true) ignores "
                f"match_url/match_type/match_value/object_match_value — columns will have no effect."
            )

        rules.append(
            RedirectRule(
                name=name,
                redirect_url=redirect_url,
                status_code=status_code,
                use_relative_url=use_relative_url,
                use_incoming_query_string=_bool(row.get("use_incoming_query_string", "")),
                disabled=_bool(row.get("disabled", "")),
                start=_int(row.get("start", "")),
                end=_int(row.get("end", "")),
                matches_always=matches_always,
                match_url=match_url,
                match_type=match_type,
                match_value=match_value,
                match_operator=match_operator,
                case_sensitive=_bool(row.get("case_sensitive", "")),
                negate=_bool(row.get("negate", "")),
                check_ips=check_ips,
            )
        )

    return rules


# ---------------------------------------------------------------------------
# Terraform HCL parser
# ---------------------------------------------------------------------------

def _extract_blocks(content: str, keyword: str) -> list[str]:
    """
    Extract all `keyword { ... }` blocks from HCL content,
    handling arbitrarily nested braces.
    """
    blocks: list[str] = []
    pattern = re.compile(r"\b" + re.escape(keyword) + r"\s*\{")
    i = 0
    while i < len(content):
        m = pattern.search(content, i)
        if not m:
            break
        start = m.end()
        depth = 1
        j = start
        while j < len(content) and depth > 0:
            if content[j] == "{":
                depth += 1
            elif content[j] == "}":
                depth -= 1
            j += 1
        blocks.append(content[start : j - 1])
        i = m.start() + 1
    return blocks


def _hcl_str(pattern: str, text: str, default: str = "") -> str:
    m = re.search(pattern, text)
    return m.group(1).strip().strip('"') if m else default


def _hcl_bool(pattern: str, text: str, default: bool = False) -> bool:
    m = re.search(pattern, text)
    return m.group(1).lower() == "true" if m else default


def _parse_match_rule_block(block: str) -> RedirectRule | None:
    """Parse a single match_rules { ... } block into a RedirectRule."""
    name = _hcl_str(r"\bname\s*=\s*\"([^\"]*)\"", block)
    redirect_url = _hcl_str(r"\bredirect_url\s*=\s*\"([^\"]*)\"", block)
    match_url = _hcl_str(r"\bmatch_url\s*=\s*\"([^\"]*)\"", block)
    disabled = _hcl_bool(r"\bdisabled\s*=\s*(true|false)", block)
    matches_always = _hcl_bool(r"\bmatches_always\s*=\s*(true|false)", block)

    try:
        status_code = int(_hcl_str(r"\bstatus_code\s*=\s*(\d+)", block, "301"))
    except ValueError:
        status_code = 301

    use_relative_url = _hcl_str(
        r"\buse_relative_url\s*=\s*\"([^\"]*)\"", block, "none"
    )
    use_incoming_query_string = _hcl_bool(
        r"\buse_incoming_query_string\s*=\s*(true|false)", block
    )
    start = int(_hcl_str(r"\bstart\s*=\s*(\d+)", block, "0"))
    end = int(_hcl_str(r"\bend\s*=\s*(\d+)", block, "0"))

    # Determine source from match_url or path matches block
    match_type = ""
    match_value = ""
    match_operator = "equals"
    case_sensitive = False
    negate = False
    check_ips = ""

    if not match_url:
        for mb in _extract_blocks(block, "matches"):
            mt = _hcl_str(r"\bmatch_type\s*=\s*\"([^\"]*)\"", mb)
            if mt == "path":
                match_type = mt
                match_value = _hcl_str(r"\bmatch_value\s*=\s*\"([^\"]*)\"", mb)
                match_operator = _hcl_str(
                    r"\bmatch_operator\s*=\s*\"([^\"]*)\"", mb, "equals"
                ).lower()
                case_sensitive = _hcl_bool(r"\bcase_sensitive\s*=\s*(true|false)", mb)
                negate = _hcl_bool(r"\bnegate\s*=\s*(true|false)", mb)
                check_ips = _hcl_str(r"\bcheck_ips\s*=\s*\"([^\"]*)\"", mb)
                break

    if not redirect_url:
        return None

    # Skip rules with no recognisable source (no match_url, no path match, not catch-all)
    if not match_url and not match_value and not matches_always:
        return None

    return RedirectRule(
        name=name,
        redirect_url=redirect_url,
        status_code=status_code,
        use_relative_url=use_relative_url,
        use_incoming_query_string=use_incoming_query_string,
        disabled=disabled,
        start=start,
        end=end,
        matches_always=matches_always,
        match_url=match_url,
        match_type=match_type,
        match_value=match_value,
        match_operator=match_operator,
        case_sensitive=case_sensitive,
        negate=negate,
        check_ips=check_ips,
    )


def parse_tf(path: Path) -> list[RedirectRule]:
    """Parse match-rules.tf into a list of RedirectRule objects."""
    content = path.read_text(encoding="utf-8")
    rules: list[RedirectRule] = []
    for block in _extract_blocks(content, "match_rules"):
        rule = _parse_match_rule_block(block)
        if rule:
            rules.append(rule)
    return rules


# ---------------------------------------------------------------------------
# Path normalisation
# ---------------------------------------------------------------------------

def normalise_path(url: str) -> str:
    """
    Extract and normalise the path portion of a URL or bare path:
    strips scheme+host from full URLs, removes trailing slash, lowercases.
    """
    url = url.strip()
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        path = urlparse(url).path
    else:
        path = url
    path = path.rstrip("/") or "/"
    return path.lower()


# ---------------------------------------------------------------------------
# Validation checks
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    check_id: int
    name: str
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.failures) == 0

    @property
    def status_icon(self) -> str:
        if self.failures:
            return "❌ FAIL"
        if self.warnings:
            return "⚠️ WARN"
        return "✅ PASS"


# ── Check 1: Loop / cycle detection ─────────────────────────────────────────

def _find_cycles_iterative(graph: dict[str, list[str]]) -> list[list[str]]:
    """
    Iterative DFS cycle detection for a directed graph.
    Safe for large graphs — avoids Python recursion limit issues.
    Returns a list of cycle paths, each ending with the repeated node.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    cycles: list[list[str]] = []
    seen_keys: set[frozenset] = set()

    for start in graph:
        if color.get(start, WHITE) != WHITE:
            continue

        path: list[str] = [start]
        stack: list[tuple[str, "Iterator"]] = [(start, iter(graph.get(start, [])))]
        color[start] = GRAY

        while stack:
            node, neighbours = stack[-1]
            try:
                nb = next(neighbours)
                nb_color = color.get(nb, WHITE)

                if nb_color == GRAY:
                    idx = path.index(nb)
                    cycle = path[idx:] + [nb]
                    key = frozenset(cycle)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        cycles.append(cycle)

                elif nb_color == WHITE:
                    color[nb] = GRAY
                    path.append(nb)
                    stack.append((nb, iter(graph.get(nb, []))))

            except StopIteration:
                color[node] = BLACK
                stack.pop()
                if path and path[-1] == node:
                    path.pop()

    return cycles


def check_loops(combined_rules: list[RedirectRule]) -> CheckResult:
    """
    Check 1 — Loop Detection.
    Builds a directed graph source_path → redirect_path for all active,
    path-based, non-regex rules, then finds cycles with iterative DFS.
    Regex rules (match_operator=matches) are excluded since their sources
    are patterns, not literal paths.
    """
    result = CheckResult(1, "Loop Detection")
    graph: dict[str, list[str]] = defaultdict(list)

    for rule in combined_rules:
        if rule.disabled or rule.is_regex or rule.is_catch_all:
            continue
        src = normalise_path(rule.source_path)
        tgt = normalise_path(rule.redirect_url)
        if src and tgt and src != tgt:
            graph[src].append(tgt)

    for cycle in _find_cycles_iterative(graph):
        result.failures.append(" → ".join(cycle))

    return result


# ── Check 2: Duplicate source path ──────────────────────────────────────────

def check_duplicates(
    new_rules: list[RedirectRule],
    existing_rules: list[RedirectRule],
) -> CheckResult:
    """
    Check 2 — Duplicate Source Path.
    Source must not already exist in the live ruleset, and must not appear
    more than once within the input CSV.
    Only applies to path-based rules (match_url or match_type=path).
    """
    result = CheckResult(2, "Duplicate Source Path")

    existing_sources = {
        normalise_path(r.source_path)
        for r in existing_rules
        if r.source_path
    }

    seen_in_input: dict[str, int] = {}

    for line_idx, rule in enumerate(new_rules, start=2):
        if not rule.source_path or rule.is_catch_all:
            continue
        src = normalise_path(rule.source_path)

        if src in existing_sources:
            result.failures.append(
                f"'{rule.source_path}' already exists in the live ruleset"
            )

        if src in seen_in_input:
            result.failures.append(
                f"'{rule.source_path}' appears more than once in the input CSV "
                f"(first at row {seen_in_input[src]})"
            )
        else:
            seen_in_input[src] = line_idx

    return result


# ── Check 3: Source equals target ───────────────────────────────────────────

def check_source_equals_target(new_rules: list[RedirectRule]) -> CheckResult:
    """
    Check 3 — Source == Target.
    Normalised path of source must differ from normalised path of redirect target.
    """
    result = CheckResult(3, "Source == Target")

    for rule in new_rules:
        if not rule.source_path or rule.is_catch_all:
            continue
        src = normalise_path(rule.source_path)
        tgt = normalise_path(rule.redirect_url)
        if src and tgt and src == tgt:
            result.failures.append(
                f"'{rule.source_path}' → '{rule.redirect_url}' "
                f"(both normalise to '{src}')"
            )

    return result


# ── Check 4: Wildcard * in path ─────────────────────────────────────────────

def check_wildcards(new_rules: list[RedirectRule]) -> CheckResult:
    """
    Check 4 — Wildcard * in Path.
    A literal '*' is not a valid ER Cloudlet match character.
    """
    result = CheckResult(4, "Wildcard * in Path")

    for rule in new_rules:
        source = rule.match_url or rule.match_value
        if source and "*" in source:
            result.warnings.append(f"Wildcard '*' in source: '{source}'")
        if "*" in rule.redirect_url:
            result.warnings.append(f"Wildcard '*' in redirect_url: '{rule.redirect_url}'")

    return result


# ── Check 5: Root / as source ───────────────────────────────────────────────

def check_root_source(new_rules: list[RedirectRule]) -> CheckResult:
    """
    Check 5 — Root / as Source.
    A source normalising to '/' would redirect every path on the site.
    """
    result = CheckResult(5, "Root / as Source")

    for rule in new_rules:
        if rule.is_catch_all:
            continue
        if rule.source_path and normalise_path(rule.source_path) == "/":
            result.failures.append(
                f"Source resolves to root '/': '{rule.source_path}'"
            )

    return result


# ── Check 6: Regex rule flag (warning only) ──────────────────────────────────

def check_regex_flag(new_rules: list[RedirectRule]) -> CheckResult:
    """
    Check 6 — Regex Rule Flag.
    match_operator=matches flags a rule for the regex-approval environment gate.
    This is a WARNING only — it does NOT block the pipeline.
    """
    result = CheckResult(6, "Regex Rule Flag")

    for rule in new_rules:
        if rule.is_regex:
            result.warnings.append(
                f"Regex rule (name: '{rule.name}', "
                f"match_value: '{rule.match_value}') "
                f"— requires regex-approval gate"
            )

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _truncate(items: list[str], limit: int = 5) -> str:
    if not items:
        return "—"
    shown = items[:limit]
    tail = f"<br>… and {len(items) - limit} more" if len(items) > limit else ""
    return "<br>".join(shown) + tail


def render_github_summary(results: list[CheckResult]) -> str:
    lines = [
        "## Redirect Rule Validation Report",
        "",
        "| # | Check | Status | Details |",
        "|---|-------|--------|---------|",
    ]
    for r in results:
        items = r.failures or r.warnings
        details = _truncate(items)
        lines.append(f"| {r.check_id} | {r.name} | {r.status_icon} | {details} |")
    lines.append("")
    return "\n".join(lines)


def render_console_report(results: list[CheckResult]) -> str:
    lines = ["", "=" * 72, "  REDIRECT RULE VALIDATION REPORT", "=" * 72]
    for r in results:
        if r.failures:
            label = "FAIL"
        elif r.warnings:
            label = "WARN"
        else:
            label = "PASS"
        lines.append(f"\n  [{label}] Check {r.check_id}: {r.name}")
        items = r.failures or r.warnings
        if items:
            for item in items:
                lines.append(f"         • {item}")
        else:
            lines.append("         No issues found.")
    lines.append("\n" + "=" * 72)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GitHub Actions helpers
# ---------------------------------------------------------------------------

def write_github_summary(content: str) -> None:
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as fh:
            fh.write(content)


def set_github_output(key: str, value: str) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _resolve_input_csv(given: Path | None) -> Path:
    """
    Resolve the input CSV path.
    If not provided, auto-discover the single CSV in the rules/ folder
    relative to the current working directory.
    """
    if given is not None:
        if not given.exists():
            sys.exit(f"ERROR: Input file not found: {given}")
        return given

    rules_dir = Path("rules")
    if not rules_dir.is_dir():
        sys.exit(
            "ERROR: No --input given and no rules/ directory found in the "
            "current working directory."
        )
    csvs = sorted(rules_dir.glob("*.csv"))
    if not csvs:
        sys.exit("ERROR: No --input given and no CSV files found in rules/.")
    if len(csvs) > 1:
        names = ", ".join(str(p) for p in csvs)
        sys.exit(
            f"ERROR: Multiple CSV files found in rules/ ({names}). "
            f"Specify one with --input."
        )
    return csvs[0]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Validate Akamai ER Cloudlet redirect rules (CSV) against "
            "the existing live ruleset (match-rules.tf)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Defaults:\n"
            "  --input         auto-discovered single CSV in rules/\n"
            "  --current-rules match-rules.tf in the current directory"
        ),
    )
    parser.add_argument(
        "--input", "-i",
        default=None,
        type=Path,
        metavar="CSV",
        help="Input CSV file (default: auto-discover single CSV in rules/)",
    )
    parser.add_argument(
        "--current-rules", "-c",
        default=Path("match-rules.tf"),
        type=Path,
        metavar="TF",
        help="Existing match-rules.tf file (default: ./match-rules.tf)",
    )
    parser.add_argument(
        "--operation", "-o",
        default="add",
        type=str,
        help="Pipeline operation (add, update, delete) (default: add)",
    )
    args = parser.parse_args()

    # ── Resolve inputs ────────────────────────────────────────────────────────
    input_csv = _resolve_input_csv(args.input)
    current_rules = args.current_rules
    if not current_rules.exists():
        sys.exit(f"ERROR: Current rules file not found: {current_rules}")

    print(f"Loading new rules       : {input_csv}")
    new_rules = parse_csv(input_csv)
    print(f"  {len(new_rules)} rule(s) loaded from CSV")

    print(f"Loading existing rules  : {current_rules}")
    existing_rules = parse_tf(current_rules)
    print(f"  {len(existing_rules)} rule(s) loaded from existing ruleset")

    combined = existing_rules + new_rules
    print(f"Combined ruleset size   : {len(combined)} rule(s)\n")

    # ── Run checks conditionally based on operation type ──────────────────────
    is_delete = args.operation.lower() == "delete"

    if is_delete:
        print("Delete operation detected: running checks 4, 5, and 6 only.")
        results = [
            CheckResult(1, "Loop Detection (Skipped for Delete)"),
            CheckResult(2, "Duplicate Source Path (Skipped for Delete)"),
            CheckResult(3, "Source == Target (Skipped for Delete)"),
            check_wildcards(new_rules),
            check_root_source(new_rules),
            check_regex_flag(new_rules),
        ]
    else:
        results = [
            check_loops(combined),
            check_duplicates(new_rules, existing_rules),
            check_source_equals_target(new_rules),
            check_wildcards(new_rules),
            check_root_source(new_rules),
            check_regex_flag(new_rules),
        ]

    # ── Console output ────────────────────────────────────────────────────────
    print(render_console_report(results))

    # ── GitHub Step Summary ───────────────────────────────────────────────────
    write_github_summary(render_github_summary(results))

    # ── GitHub Outputs ────────────────────────────────────────────────────────
    regex_check = next(r for r in results if r.check_id == 6)
    has_regex = bool(regex_check.warnings)
    set_github_output("has_regex_rules", "true" if has_regex else "false")

    # ── Exit code — checks 1–5 are blocking; check 6 is warning-only ─────────
    blocking_failures = [r for r in results if r.check_id != 6 and not r.passed]

    if blocking_failures:
        names = ", ".join(
            f"Check {r.check_id} ({r.name})" for r in blocking_failures
        )
        print(f"VALIDATION FAILED  — {names}")
        sys.exit(1)

    if has_regex:
        print("VALIDATION PASSED  — regex rules detected, regex-approval gate required")
    else:
        print("VALIDATION PASSED")

    sys.exit(0)


if __name__ == "__main__":
    main()