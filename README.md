# Akamai Edge Redirector (ER) Cloudlet — Redirect Rules Pipeline

A CI/CD pipeline for managing Akamai Edge Redirector (ER) Cloudlet redirect rules via Terraform, with full local development support, input validation, and optional regex rule testing.

> Disclaimer: This project is intended for demo purposes only. A customer should deploy this in production only after thorough testing, validation, and approval by the relevant engineering and operational teams.

---

## Table of Contents

- [Overview](#overview)
- [Repository Structure](#repository-structure)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Pipeline Stages](#pipeline-stages)
- [Input File Reference](#input-file-reference)
  - [input_pipeline_trigger.yaml](#input_pipeline_triggeryaml)
  - [simple_rules_pass.csv](#simple_rules_passcsvcsv)
- [Script Reference](#script-reference)
  - [local_run.py](#local_runpy)
  - [generate_match_rules.py](#generate_match_rulespy)
  - [validate_rules.py](#validate_rulespy)
  - [validate_regex_rules.py](#validate_regex_rulespy)
- [Terraform Configuration](#terraform-configuration)
- [Rule Types](#rule-types)
- [Validation Checks](#validation-checks)
- [Approval Gates](#approval-gates)
- [Azure DevOps Pipeline](#azure-devops-pipeline)
- [Troubleshooting](#troubleshooting)

---

## Overview

This project automates the lifecycle of Akamai ER Cloudlet redirect rules. Rules are defined in a CSV file, validated locally and in CI, compiled into a Terraform HCL data block (`match-rules.tf`), and then deployed to Akamai staging and production via `terraform apply`.

The pipeline supports three operations on the ruleset: **Add**, **Update**, and **Delete**.

**Key design decisions:**

- Rules are the source of truth in CSV — the HCL file is generated, not hand-edited.
- Regex rules use RE2 semantics (as required by Akamai) and must pass sample URL tests before deployment.
- Wildcard and regex rules trigger an extra human approval gate in both local and CI runs.
- The local runner (`local_run.py`) mirrors the Azure DevOps pipeline stages exactly, making it safe to iterate locally before pushing.

---

## Repository Structure

```
.
├── input_pipeline_trigger.yaml     # Pipeline control file — operation, regex flag, test cases
├── match-rules.tf                  # Generated HCL — do not edit by hand
├── policy.tf                       # Terraform Akamai provider + policy resource
├── variables.tf                    # Terraform input variables (edgerc path, config section)
├── local_run.py                    # Local end-to-end pipeline runner
├── rules/
│   └── simple_rules_pass.csv       # Input redirect rules
└── scripts/
    ├── generate_match_rules.py     # Reads CSV → writes match-rules.tf
    ├── validate_rules.py           # Business-logic validation (6 checks)
    └── validate_regex_rules.py     # RE2 regex compilation + sample URL validation
```

---

## Prerequisites

| Requirement     | Notes                                                             |
| --------------- | ----------------------------------------------------------------- |
| Python 3.10+    | Required for `match \| case` syntax and `dict \| None` type hints |
| `pyyaml`        | `pip install pyyaml`                                              |
| `colorama`      | `pip install colorama` (optional, enables coloured output)        |
| `google-re2`    | `pip install google-re2` — required for regex validation only     |
| Terraform ≥ 1.0 | Must be on `PATH`                                                 |
| `~/.edgerc`     | Akamai credentials file with a `appropiate` section               |

Install Python dependencies in one step:

```bash
pip install pyyaml colorama google-re2
```

---

## Quick Start

**1. Describe your change in `input_pipeline_trigger.yaml`:**

```yaml
operation: add # add | update | delete
regex: false # true if any new rule uses a regex match
testing-required: false
tests: []
```

**2. Add your rules to `rules/simple_rules_pass.csv`** (see [CSV Reference](#simple_rules_passcsvcsv) below).

**3. Run the local pipeline in dry-run mode** (validates and plans, no changes applied):

```bash
python local_run.py
```

**4. Apply to staging when ready:**

```bash
python local_run.py --apply-staging
```

**5. Apply to production after verifying staging:**

```bash
python local_run.py --apply-staging --apply-prod
```

---

## Pipeline Stages

The pipeline runs the following stages in order. Each stage must succeed before the next begins.

| Stage  | Description                                             | Skipped when                                                                          |
| ------ | ------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| **1**  | Parse `input_pipeline_trigger.yaml`                     | Never                                                                                 |
| **2**  | Run regex test cases from the YAML                      | `operation: delete` or `testing-required: false` or `regex: false`                    |
| **3**  | Run `validate_rules.py` — 6 business-logic checks       | Checks 1–3 skipped on `delete`; check 2 skipped on `update`; checks 1, 3–6 always run |
| **3a** | Validation approval gate                                | Auto-passed if no wildcard/regex warnings                                             |
| **4**  | Run `generate_match_rules.py` → update `match-rules.tf` | Never                                                                                 |
| **5**  | `terraform init` + `terraform plan`                     | `--skip-terraform`                                                                    |
| **6**  | `terraform apply` — staging only                        | `--dry-run` (default)                                                                 |
| **6a** | Staging approval gate                                   | When `--no-prompt` is set                                                             |
| **7**  | `terraform apply` — production only                     | Unless `--apply-prod` is set                                                          |
| **7a** | Production approval gate                                | When `--no-prompt` is set                                                             |

After a successful production deploy, an audit entry is appended to `local-deploy-audit.log`.

---

## Input File Reference

### `input_pipeline_trigger.yaml`

Controls the operation type and provides optional test cases for regex rules.

```yaml
operation: add # Required. add | update | delete (case-insensitive)
regex: true # Required. true if any rule in the CSV uses a regex match
testing-required: true # Required. true to run Stage 2 regex test cases

tests: # Required when testing-required: true and regex: true
  - regex: "^/products/([a-z0-9-]+)/details$"
    sampleURL: "/products/widget-123/details"
    redirectURL: "/shop/product/widget-123"
```

**Field details:**

| Field                 | Type    | Description                                                                                    |
| --------------------- | ------- | ---------------------------------------------------------------------------------------------- |
| `operation`           | string  | `add`, `update`, or `delete` — applied to the CSV rules against the live ruleset               |
| `regex`               | boolean | Set `true` if any rule in the CSV uses `match_type: regex`. Triggers Stage 2.                  |
| `testing-required`    | boolean | Set `true` to enforce Stage 2 regex testing. Ignored if `regex: false` or `operation: delete`. |
| `tests[].regex`       | string  | The regex pattern to test                                                                      |
| `tests[].sampleURL`   | string  | A URL that should match the pattern                                                            |
| `tests[].redirectURL` | string  | The expected redirect destination (used to show computed output)                               |

---

### `rules/simple_rules_pass.csv`

Defines the redirect rules to add, update, or delete. Lines beginning with `#` are treated as comments and ignored.

**Column reference:**

| Column                      | Required | Description                                                                                                                                                                               |
| --------------------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`                      | Yes      | Human-readable rule name (e.g. ticket reference like `RITM0012345`)                                                                                                                       |
| `redirect_url`              | Yes      | Destination URL or path (e.g. `/shop/new-path` or `https://example.com/page`)                                                                                                             |
| `status_code`               | Yes      | HTTP redirect code: `301`, `302`, `303`, `307`, or `308`                                                                                                                                  |
| `match_url`                 | One of†  | Direct URL/path for a simple rule (no `matches` block)                                                                                                                                    |
| `match_type`                | One of†  | Criteria type for a complex rule — see values below                                                                                                                                       |
| `match_value`               | One of†  | The value to match against (use a regex pattern when `match_type=regex`)                                                                                                                  |
| `match_operator`            | No       | `equals` (default), `contains`, `exists`                                                                                                                                                  |
| `object_match_value`        | No       | Required for `header`, `cookie`, `parameter`, `query`, `method`, and IP-list `clientip` rules. Mini-DSL: `simple:V1\|V2\|V3` or `object:NAME:V1\|V2`. When set, `match_value` is ignored. |
| `case_sensitive`            | No       | `true` / `false` (default: `false`)                                                                                                                                                       |
| `negate`                    | No       | `true` / `false` (default: `false`)                                                                                                                                                       |
| `check_ips`                 | No       | `CONNECTING_IP`, `XForwardedFor`, or `BOTH` — only for IP-based match types                                                                                                               |
| `use_relative_url`          | No       | `relative_url`, `copy_scheme_hostname`, or `none` (default: `none`)                                                                                                                       |
| `use_incoming_query_string` | No       | `true` / `false` (default: `false`)                                                                                                                                                       |
| `disabled`                  | No       | `true` / `false` (default: `false`)                                                                                                                                                       |
| `start`                     | No       | Epoch seconds for rule activation start (default: `0`)                                                                                                                                    |
| `end`                       | No       | Epoch seconds for rule activation end (default: `0`)                                                                                                                                      |
| `matches_always`            | No       | `true` to create a catch-all rule (default: `false`)                                                                                                                                      |

† A rule must use exactly one of: `match_url` (simple), `match_type` + `match_value` (complex), or `matches_always: true` (catch-all).

**Supported `match_type` values:**

`path`, `protocol`, `extension`, `hostname`, `method`, `clientip`, `continent`, `countrycode`, `proxy`, `regioncode`, `cookie`, `header`, `parameter`, `query`, `regex`, `deviceCharacteristics`

> For the authoritative list of allowed `match_type` values and their accepted operators, see the Akamai Cloudlets API documentation: <https://techdocs.akamai.com/cloudlets/reference/api>.

#### Rule type cheat sheet

Use this table to know which columns are **required**, **optional**, or **ignored** for each `match_type`. The validator (`scripts/validate_rules.py`) enforces these contracts and will flag mistakes before deploy.

| `match_type`            | `match_value` format                                  | `object_match_value`              | `check_ips`  | Notes                                                                                                                                                |
| ----------------------- | ----------------------------------------------------- | --------------------------------- | ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `path`                  | `/path` or `'/p1 /p2'` (space-sep multi)              | ✗ forbidden                       | — ignored    | Most common rule type                                                                                                                                |
| `regex`                 | RE2 pattern, e.g. `^/foo/([a-z]+)$`                   | ✗ forbidden                       | — ignored    | Triggers regex approval gate                                                                                                                         |
| `extension`             | `jpg` or `'jpg png pdf'`                              | ✗ forbidden                       | — ignored    | File extension, no leading dot                                                                                                                       |
| `hostname`              | `host.com` or `'a.com b.com'`                         | ✗ forbidden                       | — ignored    |                                                                                                                                                      |
| `protocol`              | `http` or `https`                                     | ✗ forbidden                       | — ignored    | Single value only                                                                                                                                    |
| `cookie`                | `name=value` (flat) **or** `object:NAME:V1\|V2` (omv) | optional                          | — ignored    | Use object form for multiple values per cookie                                                                                                       |
| `query`                 | `name=value` (space-sep for multi)                    | ✗ forbidden                       | — ignored    | Matches query-string parameter                                                                                                                       |
| `deviceCharacteristics` | characteristic name                                   | ✗ forbidden                       | — ignored    | e.g. `accept_third_party_cookie` — [full list](https://techdocs.akamai.com/cloudlets/reference/match-properties#device-characteristics-match-values) |
| `countrycode`           | `AU` or `'AU NZ'`                                     | ✗ forbidden                       | **required** | ISO 3166-1 alpha-2                                                                                                                                   |
| `continent`             | `OC` or `'OC AS'`                                     | ✗ forbidden                       | **required** | Continent codes                                                                                                                                      |
| `regioncode`            | `NSW` or `'NSW VIC'`                                  | ✗ forbidden                       | **required** | State/province codes                                                                                                                                 |
| `clientip`              | `1.1.1.1` or `'1.1.1.1 2.2.2.2/24'`                   | ✗ forbidden                       | **required** | Flat space-separated IPs / CIDRs (working form)                                                                                                      |
| `proxy`                 | `anonymous`, `transparent`, or `none`                 | ✗ forbidden                       | **required** |                                                                                                                                                      |
| `header`                | _(leave empty)_                                       | **required** `object:NAME:V1\|V2` | — ignored    | Header name goes in the object name                                                                                                                  |
| `method`                | _(leave empty)_                                       | **required** `simple:GET\|POST`   | — ignored    | List HTTP methods                                                                                                                                    |
| `parameter`             | _(leave empty)_                                       | **required** `object:NAME:V1\|V2` | — ignored    | URL parameter name goes in the object name                                                                                                           |

Top-level rule shapes:

| Shape     | Marker columns                  | Required                                                          | Forbidden / ignored                                            |
| --------- | ------------------------------- | ----------------------------------------------------------------- | -------------------------------------------------------------- |
| Simple    | `match_url` set                 | `name`, `redirect_url`, `status_code`                             | `match_type`, `match_value`, `object_match_value`, `check_ips` |
| Complex   | `match_type` + value source set | `name`, `redirect_url`, `status_code`, plus per-type fields above | `match_url`, `matches_always`                                  |
| Catch-all | `matches_always = true`         | `name`, `redirect_url`, `status_code`                             | `match_url`, `match_type`, `match_value`, `object_match_value` |

**`object_match_value` mini-DSL** (one column per match row):

| CSV value                    | Generates                                                                                         |
| ---------------------------- | ------------------------------------------------------------------------------------------------- |
| _(empty)_                    | No `object_match_value` block — uses flat `match_value` (default)                                 |
| `simple:GET\|POST`           | `object_match_value { type = "simple"; value = ["GET", "POST"] }`                                 |
| `object:X-My-Hdr:val1\|val2` | `object_match_value { type = "object"; name = "X-My-Hdr"; options { value = ["val1", "val2"] } }` |

Use `simple:…` for `method`, `clientip` (IP lists), `extension`, etc. Use `object:NAME:…` for `header`, `cookie`, `parameter`, `query` (the name identifies which header/cookie/parameter to inspect).

**Example rows:**

```csv
name,match_url,match_type,match_value,match_operator,...,object_match_value
# Simple rule
Old Category Page,/shop/browse/old-category,,,,,,/shop/browse/new-category,301,...,

# Complex path rule
Shop Sale Path,,path,/shop/sale,equals,false,false,,/shop/specials,301,...,

# Regex rule (uses match_type=regex; triggers regex approval gate)
Old Product Slugs,,regex,^/products/([a-z0-9-]+)/details$,equals,false,false,,/shop/product/$1,301,...,

# Method-based rule (object_match_value REQUIRED by the ER schema)
Block POST Checkout,,method,,equals,false,false,,/shop/cart,301,...,simple:POST

# Header-based rule (object form: name + values)
Internal Header Route,,header,,equals,false,false,,/internal/home,301,...,object:X-E2E-Test:test-value

# Catch-all rule
Default Catch-All,,,,,,,/home,301,...,
```

#### Complex rules with multiple match conditions

A complex rule may need more than one `matches { }` block — for example, "match `protocol = https` **AND** `path = /shop/sale`". The CSV format expresses this as **one row per `matches` block**, all sharing the same `name` and `redirect_url`:

- The **first row** carries the full rule metadata (`name`, `redirect_url`, `status_code`, `use_relative_url`, etc.) **and** the first match condition (`match_type`, `match_value`, `match_operator`, ...).
- Each **continuation row** repeats `name` and `redirect_url` (so the parser groups them) and fills only the match columns for the next condition. `match_url` must be left empty on every row of a complex rule.

**Example — single rule with two conditions (`https` AND `/shop/sale`):**

```csv
name,match_url,match_type,match_value,match_operator,case_sensitive,negate,check_ips,redirect_url,status_code,use_relative_url,use_incoming_query_string,disabled,start,end,matches_always
REQ1944524 Sale Page,,protocol,https,equals,false,false,,/shop/specials,301,none,false,false,0,0,false
REQ1944524 Sale Page,,path,/shop/sale,equals,false,false,,/shop/specials,301,none,false,false,0,0,false
```

Both rows compile into a single `match_rules { }` entry containing two nested `matches { }` blocks (AND-ed together by the Cloudlet at match time).

**Grouping rules used by the parser** (`scripts/generate_match_rules.py`):

- Rows are merged into one rule when **all** of the following are true:
  - `name` is identical to the previous row
  - `redirect_url` is identical to the previous row
  - `match_url` is empty on **both** rows (a non-empty `match_url` always starts a new simple rule)
- A change in `name` or `redirect_url`, or any non-empty `match_url`, starts a new rule.
- The composite key used for add/update/delete is `(name, tuple of all (match_type, match_value, match_operator)`) — so changing any condition produces a different rule identity.

> **Authoring tip:** keep the rule metadata columns identical across continuation rows. A stray edit to `redirect_url` on row 2 will split one complex rule into two separate rules at generation time.

---

## Script Reference

### `local_run.py`

End-to-end local pipeline runner. Mirrors all Azure DevOps stages.

```
Usage:
  python local_run.py                              # Dry run (plan only, no apply)
  python local_run.py --dry-run                    # Explicit dry run
  python local_run.py --apply-staging              # Apply to staging, stop
  python local_run.py --apply-staging --apply-prod # Apply staging + prod
  python local_run.py --skip-terraform             # Python stages only, no Terraform
  python local_run.py --no-prompt                  # Skip all interactive approval gates
  python local_run.py --repo-root /path/to/repo    # Override repo root directory
```

**Flags:**

| Flag               | Default           | Description                                                               |
| ------------------ | ----------------- | ------------------------------------------------------------------------- |
| `--dry-run`        | `true` (implicit) | Run all Python stages and `terraform plan` but do not apply               |
| `--apply-staging`  | `false`           | Apply `akamai_cloudlets_policy` and `policy_activation_staging` resources |
| `--apply-prod`     | `false`           | Apply `policy_activation_prod` resource. Requires `--apply-staging`.      |
| `--skip-terraform` | `false`           | Stop after Stage 4 — useful for validating rule generation only           |
| `--no-prompt`      | `false`           | Suppress all interactive `[y/N]` approval prompts                         |
| `--repo-root`      | Script directory  | Override the repo root path                                               |

> **Note:** `match-rules.tf` is automatically backed up before Stage 4 and restored if `terraform plan` fails or if running in dry-run mode.

---

### `generate_match_rules.py`

Reads the input CSV and writes (or modifies) `match-rules.tf`. The HCL file contains a single `data "akamai_cloudlets_edge_redirector_match_rule"` block.

```
Usage:
  python scripts/generate_match_rules.py \
    --operation add|update|delete \
    --input    rules/simple_rules_pass.csv \
    --target   match-rules.tf
```

**Operation behaviour:**

| Operation | Behaviour                                                                                                    |
| --------- | ------------------------------------------------------------------------------------------------------------ |
| `add`     | Prepends new rules to the top of the existing ruleset. Skips rules whose `(name, match)` key already exists. |
| `update`  | Replaces existing rules in-place by matching `(name, match)` key. Skips rules not found in the current file. |
| `delete`  | Removes all rules whose `(name, match)` key appears in the input CSV.                                        |

Rules are uniquely identified by a composite key of `(name, match_criteria)` — not just by name. This means a rule with the same name but different match criteria is treated as a distinct rule.

Any comment lines or preamble above the `data` block in the existing `match-rules.tf` are preserved.

---

### `validate_rules.py`

Runs 6 validation checks against the input CSV combined with the existing live ruleset from `match-rules.tf`.

```
Usage:
  python scripts/validate_rules.py
  python scripts/validate_rules.py --input rules/simple_rules.csv
  python scripts/validate_rules.py --current-rules match-rules.tf
  python scripts/validate_rules.py --operation delete
```

**Exit codes:**

| Code | Meaning                                           |
| ---- | ------------------------------------------------- |
| `0`  | All checks passed (warnings may be present)       |
| `1`  | One or more hard errors detected — pipeline stops |

See [Validation Checks](#validation-checks) for a full description of each check.

---

### `validate_regex_rules.py`

Validates that regex patterns in the YAML test cases compile under RE2 semantics and that sample URLs match as expected. Akamai Cloudlets uses the RE2 regex engine, which excludes some constructs valid in Python's `re` module (e.g. lookaheads, backreferences).

```
Usage:
  # Inline JSON
  python scripts/validate_regex_rules.py \
    --payload '{"regex":"^/old/(.*)$","redirectURL":"/new/$1","sampleURL":"/old/page"}'

  # JSON file (single object or array)
  python scripts/validate_regex_rules.py --input rules/regex_tests.json

  # Machine-readable JSON report
  python scripts/validate_regex_rules.py --input rules/regex_tests.json --json-output
```

**Input format (JSON):**

```json
[
  {
    "regex": "^/products/([a-z0-9-]+)/details$",
    "sampleURL": "/products/widget-123/details",
    "redirectURL": "/shop/product/$1",
    "caseSensitive": false
  }
]
```

**What is validated:**

- Pattern compiles successfully under RE2 (`google-re2` library)
- `sampleURL` matches the pattern
- `redirectURL` is an absolute `http(s)://` URL or a leading-slash path

**Exit codes:** `0` = all pass, `1` = one or more fail, `2` = invalid usage or missing RE2 runtime.

> **RE2 reference:** https://github.com/google/re2/wiki/syntax

---

## Terraform Configuration

### `policy.tf`

Defines the Akamai provider, the cloudlet policy resource, and the (currently commented-out) staging and production activation resources.

Key resource names:

| Resource                             | Name                        | Description                                                          |
| ------------------------------------ | --------------------------- | -------------------------------------------------------------------- |
| `akamai_cloudlets_policy`            | `policy`                    | The ER Cloudlet policy — references the generated `match_rules` JSON |
| `akamai_cloudlets_policy_activation` | `policy_activation_staging` | Activates the policy version on staging                              |
| `akamai_cloudlets_policy_activation` | `policy_activation_prod`    | Activates the policy version on production                           |

The activation resources are targeted individually during deployment:

- Stage 6 targets `akamai_cloudlets_policy.policy` and `policy_activation_staging`
- Stage 7 targets `policy_activation_prod` only (staging is not re-touched)

### `variables.tf`

| Variable         | Default     | Description                                |
| ---------------- | ----------- | ------------------------------------------ |
| `edgerc_path`    | `~/.edgerc` | Path to the Akamai EdgeRC credentials file |
| `config_section` | `adv-sol`   | Section name within the EdgeRC file        |

### Credentials

Terraform reads Akamai credentials from `~/.edgerc`. The expected section is `[adv-sol]`:

```ini
[adv-sol]
client_secret = ...
host          = ...
access_token  = ...
client_token  = ...
```

---

## Rule Types

| Type                       | How to define                                                                                             | Notes                                                                                                                                   |
| -------------------------- | --------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| **Simple**                 | Set `match_url`                                                                                           | Direct path match; fastest and simplest. One CSV row = one rule.                                                                        |
| **Complex (single match)** | Set `match_type` + `match_value` + `match_operator`                                                       | Supports path, header, cookie, geo, device, and more. One CSV row = one rule.                                                           |
| **Complex (multi-match)**  | One CSV row per `matches { }` block, all sharing the same `name` + `redirect_url`, with empty `match_url` | All conditions AND-ed at match time. See [Complex rules with multiple match conditions](#complex-rules-with-multiple-match-conditions). |
| **Regex**                  | Set `match_type: regex` with the pattern in `match_value`                                                 | Triggers the regex approval gate; uses RE2 semantics. `match_operator=matches` is **not** valid per the ER schema.                      |
| **Catch-all**              | Set `matches_always: true`                                                                                | Matches every request; should be the last rule                                                                                          |

---

## Validation Checks

`validate_rules.py` runs these checks. Checks 1–3 are skipped for `delete` operations; check 2 is skipped for `update` operations (an update's source path is expected to already exist in the live ruleset). For `update`, Check 1 (loop detection) runs against an effective graph that drops the old versions of the rules being replaced — so it still catches loops a `redirect_url` edit might introduce, without false positives from the pre-update state.

| Check                         | Severity | Description                                                                                                                                       |
| ----------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1 — Loop Detection**        | Error    | Detects redirect chains where rule A redirects to rule B's source, and B redirects back to A's source (or any longer cycle).                      |
| **2 — Duplicate Source Path** | Error    | Detects when a new rule's source path already exists in the live ruleset, which would create an ambiguous or unreachable rule.                    |
| **3 — Source Equals Target**  | Error    | Detects rules where the source and destination resolve to the same normalised path (e.g. `/path` and `/path/` are treated as equal).              |
| **4 — Wildcard `*` in Path**  | Warning  | Flags literal `*` characters in `match_url` or `match_value` — these are not valid path wildcards in the ER Cloudlet. Triggers the approval gate. |
| **5 — Root `/` as Source**    | Warning  | Flags rules whose source normalises to `/`, which would redirect every request on the property.                                                   |
| **6 — Regex Rules Detected**  | Warning  | Flags any rule using `match_type: regex`. Triggers the regex approval gate.                                                                       |

Warnings do not stop the pipeline but do require explicit human approval before proceeding.

---

## Approval Gates

Two interactive approval prompts appear during a local run (suppressed by `--no-prompt`):

| Gate                | Triggered by           | Prompt                                                |
| ------------------- | ---------------------- | ----------------------------------------------------- |
| **validation-gate** | Always (after Stage 3) | Confirms the validation report has been reviewed      |
| **staging**         | `--apply-staging`      | Confirms intent to push to the Akamai staging network |
| **production**      | `--apply-prod`         | Confirms intent to push to Akamai production          |

When wildcard or regex warnings are detected in Stage 3, the validation-gate prompt includes an explicit reminder to review the report carefully.

---

## Azure DevOps Pipeline

The Azure DevOps pipeline mirrors the stages in `local_run.py`. The main differences from local runs:

- Approval gates are implemented as Azure DevOps environment checks or pull request approvals rather than interactive prompts.
- Terraform state is stored remotely rather than in `terraform.tfstate` in the repo root.
- GitHub Step Summary output from `validate_rules.py` is posted to the pipeline run summary.
- A GitHub Output variable (`regex_rules_detected`) is set when regex rules are found, enabling downstream conditional steps.

To trigger the pipeline, commit changes to both `input_pipeline_trigger.yaml` and `rules/simple_rules_pass.csv`.

---

## Troubleshooting

**`ERROR: pyyaml is not installed`**
Run `pip install pyyaml`.

**`ERROR: RE2 runtime not found`**
Run `pip install google-re2`. Note: `google-re2` requires a C++ build environment. On macOS, `brew install re2` may be needed first.

**`~/.edgerc not found`**
Ensure your Akamai EdgeRC credentials file exists at `~/.edgerc` with an `[adv-sol]` section.

**`terraform not found on PATH`**
Install Terraform ≥ 1.0 and ensure it is available on your system `PATH`.

**`match-rules.tf` corrupted after a failed run**
`local_run.py` creates a backup at `match-rules.tf.bak` before Stage 4. Restore it manually:

```bash
cp match-rules.tf.bak match-rules.tf
```

**`Skipping add: Rule '...' already exists`**
The rule's `(name, match)` key already exists in `match-rules.tf`. Use `--operation update` to overwrite it, or change the rule name/match criteria.

**`terraform plan` shows unexpected deletions**
The local Terraform state (`terraform.tfstate`) may be out of sync with the remote Akamai state. Run `terraform import` for the affected resources, or refresh state with `terraform refresh`.
