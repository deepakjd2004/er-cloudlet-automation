# scripts/validate_rules.py

Validates the redirect rules CSV against the existing live ruleset (`match-rules.tf`) before any Terraform changes are applied. Produces a report for GitHub Step Summary and exits with a non-zero code if any blocking check fails.

> Disclaimer: This project is intended for demo purposes only. A customer should deploy this in production only after thorough testing, validation, and approval by the relevant engineering and operational teams.

---

## Usage

```bash
# Default — auto-discovers the CSV in rules/ and uses match-rules.tf in the current directory
python scripts/validate_rules.py

# Explicit paths (override defaults)
python scripts/validate_rules.py \
  --input  rules/simple_rules.csv \
  --current-rules match-rules.tf
```

| Flag                     | Default                         | Description                              |
| ------------------------ | ------------------------------- | ---------------------------------------- |
| `--input` / `-i`         | Auto-discovered CSV in `rules/` | Input CSV file to validate               |
| `--current-rules` / `-c` | `./match-rules.tf`              | Existing live ruleset parsed for context |

> **Limitation — single CSV only:** The auto-discovery requires exactly one `.csv` file in the `rules/` folder. If more than one CSV is present you must specify the file explicitly with `--input`. Multiple CSVs in the same run are not supported.

---

## CSV Format

The CSV maps directly to the `akamai_cloudlets_edge_redirector_match_rule` Terraform provider schema (Akamai provider v10.2.0+). Lines starting with `#` are treated as comments and ignored.

### Header

```
name,match_url,match_type,match_value,match_operator,case_sensitive,negate,check_ips,redirect_url,status_code,use_relative_url,use_incoming_query_string,disabled,start,end,matches_always
```

### Column Reference

**Required**

| Column         | Values                        | Description                                            |
| -------------- | ----------------------------- | ------------------------------------------------------ |
| `name`         | any string                    | Rule label — ticket reference, short description, etc. |
| `redirect_url` | URL or path                   | Redirect destination                                   |
| `status_code`  | `301` `302` `303` `307` `308` | HTTP redirect status                                   |

**Source — choose exactly one of the following three approaches per row**

| Approach     | Columns to populate                             | HCL equivalent          |
| ------------ | ----------------------------------------------- | ----------------------- |
| Simple rule  | `match_url`                                     | `match_url = "/path"`   |
| Complex rule | `match_type` + `match_value` + `match_operator` | `matches { }` block     |
| Catch-all    | `matches_always = true`                         | `matches_always = true` |

`match_url` and `match_type`/`match_value` are mutually exclusive. If both are set, `match_url` takes precedence.

**`matches` block columns** _(only used with complex / regex rules)_

| Column           | Values                                                                                                                                                                    | Description                                                                                               |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `match_type`     | `path` `protocol` `extension` `hostname` `method` `clientip` `continent` `countrycode` `proxy` `regioncode` `cookie` `header` `parameter` `query` `deviceCharacteristics` | Type of match condition                                                                                   |
| `match_value`    | string                                                                                                                                                                    | Value to match against; use a regex pattern when `match_operator=matches`                                 |
| `match_operator` | `equals` `contains` `exists` `matches`                                                                                                                                    | Comparison operator (`matches` = regex)                                                                   |
| `case_sensitive` | `true` `false`                                                                                                                                                            | Default: `false`                                                                                          |
| `negate`         | `true` `false`                                                                                                                                                            | Invert the match result. Default: `false`                                                                 |
| `check_ips`      | `CONNECTING_IP` `XForwardedFor` `BOTH`                                                                                                                                    | Which IP to evaluate — only for `clientip`, `proxy`, `continent`, `countrycode`, `regioncode` match types |

**Optional top-level columns**

| Column                      | Values                                       | Default              |
| --------------------------- | -------------------------------------------- | -------------------- |
| `use_relative_url`          | `relative_url` `copy_scheme_hostname` `none` | `none`               |
| `use_incoming_query_string` | `true` `false`                               | `false`              |
| `disabled`                  | `true` `false`                               | `false`              |
| `start`                     | epoch seconds                                | `0` (no restriction) |
| `end`                       | epoch seconds                                | `0` (no restriction) |
| `matches_always`            | `true` `false`                               | `false`              |

### Examples

```csv
name,match_url,match_type,match_value,match_operator,case_sensitive,negate,check_ips,redirect_url,status_code,use_relative_url,use_incoming_query_string,disabled,start,end,matches_always

# Simple rule — match_url only
Shop Old Category,/shop/browse/old-category,,,,,,,/shop/browse/new-category,301,relative_url,false,false,0,0,false

# Complex rule — matches block (path, equals)
Shop Sale Path,,path,/shop/sale,equals,false,false,,/shop/specials,301,relative_url,false,false,0,0,false

# Regex rule — match_operator=matches; triggers regex-approval gate (Check 6 warning)
Old Product Slugs,,path,^/products/([a-z0-9-]+)/details$,matches,false,false,,/shop/product/$1,301,relative_url,false,false,0,0,false

# Catch-all — matches_always=true; leave match_url and match_type/match_value blank
Default Catch-All,,,,,,,,/home,301,relative_url,false,false,0,0,true
```

---

## Validation Checks

All checks run against the **new rules combined with the existing live ruleset**.

| #   | Check                     | Blocking     | What it catches                                                                                                                                                                  |
| --- | ------------------------- | ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | **Loop Detection**        | Yes          | Redirect cycles across the full ruleset (e.g. A→B, B→A). Uses iterative DFS — safe for large rulesets. Regex rules are excluded (their sources are patterns, not literal paths). |
| 2   | **Duplicate Source Path** | Yes          | A source path in the CSV already exists in the live `match-rules.tf`, or appears more than once within the CSV itself.                                                           |
| 3   | **Source == Target**      | Yes          | Source and redirect destination normalise to the same path (trailing slash stripped, lowercased).                                                                                |
| 4   | **Wildcard `*` in Path**  | Yes          | A literal `*` in `match_url`, `match_value`, or `redirect_url` — not a valid ER Cloudlet match character.                                                                        |
| 5   | **Root `/` as Source**    | Yes          | Source path normalises to `/` — would redirect every request on the site.                                                                                                        |
| 6   | **Regex Rule Flag**       | No (warning) | `match_operator=matches` detected — flags the rule for mandatory SME review via the `regex-approval` environment gate. Pipeline continues.                                       |

Checks 1–5 cause the script to exit with code `1`, stopping the pipeline. Check 6 exits with code `0` but sets the `has_regex_rules=true` GitHub Output to trigger the extra approval gate.

---

## Output

**Console** — formatted report printed to stdout.

**GitHub Step Summary** — markdown table appended to `$GITHUB_STEP_SUMMARY` automatically when running inside GitHub Actions.

**GitHub Output** — `has_regex_rules=true|false` written to `$GITHUB_OUTPUT` for use by downstream pipeline steps.

---

## Exit Codes

| Code | Meaning                                                         |
| ---- | --------------------------------------------------------------- |
| `0`  | All blocking checks passed (regex warning may still be present) |
| `1`  | One or more of checks 1–5 failed                                |

---

## Regex Rule Validator (RE2)

Use `scripts/validate_regex_rules.py` to validate regex-based redirect rules
with RE2 semantics (same regex engine family used by Akamai Cloudlets).

RE2 syntax reference:
https://github.com/google/re2/wiki/syntax

### Input format

Single object or array of objects:

```json
{
  "regex": "(product|sports)",
  "redirectURL": "https://www.example.com/$1",
  "sampleURL": "https://www.example.com/product"
}
```

### Usage

```bash
# Inline JSON
python scripts/validate_regex_rules.py \
  --payload '{"regex":"(product|sports)","redirectURL":"https://www.example.com/$1","sampleURL":"https://www.example.com/product"}'

# JSON file
python scripts/validate_regex_rules.py --input rules/regex_validation.json

# Machine-readable report
python scripts/validate_regex_rules.py --input rules/regex_validation.json --json-output
```

### What it validates

1. Regex compiles under RE2.
2. `sampleURL` matches the regex.
3. `redirectURL` is a valid absolute `http(s)` URL or leading-slash path.
4. Back-reference consistency for `$N` in `redirectURL` vs capture groups in `regex`.

### Exit codes

| Code | Meaning                                                           |
| ---- | ----------------------------------------------------------------- |
| `0`  | All entries passed                                                |
| `1`  | One or more entries failed validation                             |
| `2`  | Invalid input/usage, malformed JSON, or RE2 runtime not available |

### Runtime requirement

This script requires a Python RE2 binding:

```bash
pip install google-re2
```
