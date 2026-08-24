# Handover — Akamai ER Cloudlet Redirect Rules Automation

This document is a one-stop handover for any engineer taking over the Akamai Edge Redirector (ER) Cloudlet redirect-rules automation in this repository. It covers what the automation does, how to bring a new cloudlet under management, the day-to-day pipeline, the input file format, and the gotchas you need to know about.

> Disclaimer: This project is intended for demo purposes only. A customer should deploy this in production only after thorough testing, validation, and approval by the relevant engineering and operational teams.

For deeper reference material (per-`match_type` cheat sheet, full CLI flags, validation-check internals, troubleshooting. etc), see [README.md](README.md).

---

## Table of Contents

- [1. What this automation does](#1-what-this-automation-does)
- [2. How the automation works (overview)](#2-how-the-automation-works-overview)
- [3. One-time setup per cloudlet](#3-one-time-setup-per-cloudlet)
- [4. Files and folders that matter](#4-files-and-folders-that-matter)
- [5. Pipeline stages (what `local_run.py` does)](#5-pipeline-stages-what-local_runpy-does)
- [6. Validation checks (`scripts/validate_rules.py`)](#6-validation-checks-scriptsvalidate_rulespy)
- [7. Input CSV format](#7-input-csv-format)
- [8. Day-to-day workflow](#8-day-to-day-workflow)
- [9. Backups and rollback](#9-backups-and-rollback)
- [10. Moving from local-testing to CI/CD](#10-moving-from-local-testing-to-cicd)
- [11. Where to look when something breaks](#11-where-to-look-when-something-breaks)

---

## 1. What this automation does

Automates the full lifecycle (add / update / delete) of redirect rules inside an Akamai ER Cloudlet policy:

- Rules are authored in a **CSV** (the source of truth).
- Python tooling **validates** the CSV, then **generates** Terraform HCL for the rules.
- Terraform **applies** the generated HCL through the Akamai provider, activating the policy version on staging and (after approval) production.

Key design choices:

- The CSV is the source of truth. `match-rules.tf` is **generated** and should never be hand-edited.
- Regex rules use **RE2** semantics (what Akamai accepts) and must pass sample-URL tests before deploy.
- Wildcard and regex rules trigger an extra interactive approval gate.
- [local_run.py](local_run.py) mirrors the Azure DevOps pipeline stages exactly — what runs locally is what runs in CI.

---

## 2. How the automation works (overview)

1. **Identify** the ER cloudlet you want to manage.
2. **One-time setup**: import the existing policy state with the Akamai Terraform CLI so Terraform knows about the live cloudlet (see Section 3).
3. **Drop in the automation files** from this repo (see Section 4).
4. **Author your rules** in `rules/simple_rules_pass.csv` and set the operation in [input_pipeline_trigger.yaml](input_pipeline_trigger.yaml).
5. **Run [local_run.py](local_run.py)** — it validates, generates, plans, and (with the right flags) applies to staging then production.

---

## 3. One-time setup per cloudlet

Done **once per cloudlet** to bootstrap state. After this you only ever run the day-to-day workflow.

1. **Get the cloudlet/policy ID** from the Akamai UI or API.
2. **Export the existing policy** with the Akamai Terraform CLI:

   ```bash
   akamai terraform export-cloudlets-policy <policy_id>
   ```

   This generates an initial `policy.tf` and `match-rules.tf` from the live policy.

3. **Edit [variables.tf](variables.tf)** — point `edgerc_path` at your `~/.edgerc` and set `config_section` to the appropriate section (default `adv-sol`):

   ```ini
   [adv-sol]
   client_secret = ...
   host          = ...
   access_token  = ...
   client_token  = ...
   ```

4. **Run [import.sh](import.sh)** to import the live policy resources into Terraform state.
5. **Verify**: a local `terraform.tfstate` file should now exist in the repo root.

> **Backend & secrets**: this repo currently uses a **local** Terraform backend and a **local** `~/.edgerc`. Both are intentional for testing. In CI move to a remote backend (S3, Azure Storage, Terraform Cloud) and a pipeline secret store for the EdgeRC credentials.

---

## 4. Files and folders that matter

| Path                                                               | Owned by   | Purpose                                                                                                                                                                         |
| ------------------------------------------------------------------ | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [input_pipeline_trigger.yaml](input_pipeline_trigger.yaml)         | You        | Pipeline control file — operation (`add`/`update`/`delete`), regex flag, regex test cases                                                                                       |
| [local_run.py](local_run.py)                                       | Automation | End-to-end pipeline runner. Mirrors all Azure DevOps stages.                                                                                                                    |
| [match-rules.tf](match-rules.tf)                                   | Generated  | All policy rules in HCL. First created by Akamai CLI export, then mutated by [scripts/generate_match_rules.py](scripts/generate_match_rules.py) on every run. Do not hand-edit. |
| [policy.tf](policy.tf)                                             | Akamai CLI | Cloudlet policy resource + provider config. Automation does not touch this.                                                                                                     |
| [variables.tf](variables.tf)                                       | You        | `edgerc_path`, `config_section`                                                                                                                                                 |
| [requirements.txt](requirements.txt)                               | You        | Python dependencies — install with `pip install -r requirements.txt`                                                                                                            |
| [rules/](rules)                                                    | You        | Input CSVs. The pipeline reads `rules/simple_rules_pass.csv` by default; override with `--input-file rules/your_file.csv`.                                                      |
| [scripts/generate_match_rules.py](scripts/generate_match_rules.py) | Automation | CSV → HCL generator                                                                                                                                                             |
| [scripts/validate_rules.py](scripts/validate_rules.py)             | Automation | Business-logic validator (6 checks)                                                                                                                                             |
| [scripts/validate_regex_rules.py](scripts/validate_regex_rules.py) | Automation | RE2 regex compile + sample URL match validation                                                                                                                                 |
| [backups/](backups)                                                | Automation | Timestamped snapshots of `match-rules.tf` taken on every pipeline run (kept: 50 most recent)                                                                                    |

### `input_pipeline_trigger.yaml` quick reference

```yaml
operation: add # add | update | delete
regex: false # true if any new rule uses match_type: regex
testing-required: false # true to enforce Stage 2 regex test cases
tests: [] # required when regex+testing-required are true
```

When `regex: true` and `testing-required: true`, each test entry must look like:

```yaml
tests:
  - regex: "^/products/([a-z0-9-]+)/details$"
    sampleURL: "/products/widget-123/details"
    redirectURL: "/shop/product/widget-123"
```

---

## 5. Pipeline stages (what `local_run.py` does)

| Stage | What it does                                                                                     | Skipped when                                                                          |
| ----- | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| 1     | Parse [input_pipeline_trigger.yaml](input_pipeline_trigger.yaml)                                 | Never                                                                                 |
| 2     | Run regex test cases (via [scripts/validate_regex_rules.py](scripts/validate_regex_rules.py))    | `operation: delete` **or** `regex: false` **or** `testing-required: false`            |
| 3     | Run [scripts/validate_rules.py](scripts/validate_rules.py) — 6 checks                            | Checks 1–3 skipped on `delete`; check 2 skipped on `update`; checks 1, 3–6 always run |
| 3a    | Validation approval gate                                                                         | Auto-passed when no wildcard/regex warnings present                                   |
| 4     | Run [scripts/generate_match_rules.py](scripts/generate_match_rules.py) → mutate `match-rules.tf` | Never                                                                                 |
| 5     | `terraform init` + `terraform plan`                                                              | `--skip-terraform`                                                                    |
| 6     | `terraform apply` — staging                                                                      | `--dry-run` (the default)                                                             |
| 6a    | Staging approval gate                                                                            | `--no-prompt`                                                                         |
| 7     | `terraform apply` — production                                                                   | Unless `--apply-prod` is passed                                                       |
| 7a    | Production approval gate                                                                         | `--no-prompt`                                                                         |

After a successful production deploy, an audit entry is appended to `local-deploy-audit.log`.

---

## 6. Validation checks (`scripts/validate_rules.py`)

Six checks run against the CSV combined with the existing live ruleset parsed from `match-rules.tf`. Errors fail the pipeline; warnings still require explicit human approval at the validation gate.

| #   | Check                     | Severity | What it does                                                                                                 |
| --- | ------------------------- | -------- | ------------------------------------------------------------------------------------------------------------ |
| 1   | **Loop Detection**        | Error    | Builds a graph of `source → target` paths across new + existing rules and fails on any cycle (A→B→A, etc.).  |
| 2   | **Duplicate Source Path** | Error    | Fails if a new rule's source path is already present in the live ruleset, or appears twice in the input CSV. |
| 3   | **Source == Target**      | Error    | Fails if a rule's source and destination normalise to the same path (e.g. `/foo` and `/foo/`).               |
| 4   | **Wildcard `*` in Path**  | Warning  | Flags `*` characters in `match_url`, `match_value`, or `redirect_url`                                        |
| 5   | **Root `/` as Source**    | Warning  | Flags rules whose source normalises to `/` (would redirect every request on the property).                   |
| 6   | **Regex Rules Detected**  | Warning  | Flags any rule using `match_type: regex` — triggers the regex approval gate.                                 |

In addition to the six numbered checks, the validator enforces **per-`match_type` schema contracts** on each row (e.g. `method` requires `object_match_value`; `clientip` requires `check_ips`). See the "Rule type cheat sheet" in [README.md](README.md) for the full table.

**Operation-specific skips:**

- `delete` — checks 1–3 skipped (the rules are about to be removed).
- `update` — check 2 skipped (the source path is expected to already exist; that's how `update` finds the rule to mutate). Check 1 (loop detection) runs against an effective graph where the old versions of the rules being updated are dropped first, so a `redirect_url` edit that would introduce a cycle is still caught.
- `add` — all six checks run.

---

## 7. Input CSV format

CSV header (column order matters):

```
name,match_url,match_type,match_value,match_operator,case_sensitive,negate,check_ips,redirect_url,status_code,use_relative_url,use_incoming_query_string,disabled,start,end,matches_always,object_match_value
```

Column reference and the per-`match_type` cheat sheet (which columns are required / optional / forbidden for each `match_type`) live in [README.md](README.md#rulessimple_rules_passcsv). The most important conventions are summarised below.

### Three rule shapes

Akamai's API has a different schema for **simple** versus **complex** rules. The parser/generator picks the shape based on which "source" column you populate:

| Aspect                | Simple rule                                         | Complex rule                                                                                                                             | Catch-all                                                   |
| --------------------- | --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| Source column         | `match_url` populated                               | `match_url` empty; `match_type` + (`match_value` and/or `object_match_value`) populated                                                  | All three above empty; `matches_always=true`                |
| CSV rows per rule     | Always exactly 1                                    | 1 or more (one per AND condition)                                                                                                        | 1                                                           |
| Generated HCL         | Top-level `match_url = "…"`, no `matches { }` block | `match_url = ""`, one or more `matches { }` blocks                                                                                       | `match_url = ""`, no `matches { }`, `matches_always = true` |
| What it matches       | Literal URL/path as one whole string                | A typed condition (path, header, cookie, regex, IP, etc.) with operator / negate / case-sensitive support; multi-condition AND supported | Every request                                               |
| Multi-condition (AND) | Not possible                                        | Yes — use continuation rows (see below)                                                                                                  | N/A                                                         |

> Mental model: **complex rule = "Show advanced options" in the Akamai UI.**

### Complex rules with multiple matches (continuation rows)

A complex rule with more than one AND condition is expressed as **multiple CSV rows** sharing the same `name` and `redirect_url`, with `match_url` empty on every row. Each row contributes one `matches { }` block.

Example — one rule, two conditions (`protocol=https` AND `path=/e2e-test/multi-match-old`):

```csv
E2E: Complex Multi Match,,protocol,https,equals,false,false,,/e2e-test/multi-match-new,301,relative_url,false,false,0,0,false,
E2E: Complex Multi Match,,path,/e2e-test/multi-match-old,equals,false,false,,/e2e-test/multi-match-new,301,relative_url,false,false,0,0,false,
```

**Grouping rule used by [scripts/generate_match_rules.py](scripts/generate_match_rules.py)** — a row is merged into the previous one **iff all four** are true:

| Condition                                | Why                                                                                |
| ---------------------------------------- | ---------------------------------------------------------------------------------- |
| `name` matches the previous row          | Same logical rule identity                                                         |
| `redirect_url` matches the previous row  | Same destination — a different target means a different rule                       |
| Current row's `match_url` is empty       | A populated `match_url` is the marker of a simple/standalone rule                  |
| Previous row's `match_url` is also empty | The previous rule must already be a complex (matches-based) rule, not a simple one |

### Gotchas (read these before editing CSVs)

1. **`redirect_url` must be identical on every continuation row.** A typo silently splits one rule into two.
2. **`match_url` must be empty on every row of a complex rule — including the first.** A stray value on row 1 will (a) make row 1 a simple rule and (b) make row 2 start its own new rule.
3. **All rule-level metadata** (`status_code`, `use_relative_url`, `start`, `end`, `disabled`, `matches_always`, `use_incoming_query_string`) **is taken from the first row only.** Editing those columns on row 2+ has no effect.
4. **Continuation rows must be contiguous.** Grouping is sequential — an unrelated row inserted between two `E2E: Complex Multi Match` rows breaks the merge.
5. **Ordering inside the rule = CSV row order.** Row 1's match becomes the first `matches { }` block, row 2 the second, etc.

### Rule identity (what `update` / `delete` match on)

- Simple rule: `(name, match_url)`
- Complex rule: `(name, [(match_type, match_value, match_operator), ...])`

`update` and `delete` find the live rule by this identity tuple. If any identity field drifts from what's in `match-rules.tf`, the operation will silently skip the rule.

---

## 8. Day-to-day workflow

```bash
# 1. Install deps (first time only)
pip install -r requirements.txt

# 2. Author rules
$EDITOR rules/simple_rules_pass.csv

# 3. Set operation
$EDITOR input_pipeline_trigger.yaml      # operation: add | update | delete

# 4. Dry run — validates, generates HCL, runs terraform plan
python local_run.py

# 5. Apply to staging (interactive approval at the gate)
python local_run.py --apply-staging

# 6. After verifying staging, apply to production
python local_run.py --apply-staging --apply-prod
```

Useful overrides:

| Flag                         | Effect                                                              |
| ---------------------------- | ------------------------------------------------------------------- |
| `--input-file rules/foo.csv` | Use a different CSV instead of `rules/simple_rules_pass.csv`        |
| `--skip-terraform`           | Stop after generating `match-rules.tf` (validation/generation only) |
| `--no-prompt`                | Suppress interactive approval gates (use in CI only)                |
| `--repo-root /path/to/repo`  | Run against a different repo root                                   |

Example E2E test CSVs are checked in at [rules/e2e_test_pass.csv](rules/e2e_test_pass.csv) (add) and [rules/e2e_test_update.csv](rules/e2e_test_update.csv) (update).

---

## 9. Backups and rollback

Two independent backup mechanisms protect `match-rules.tf`:

1. **In-run rollback (`match-rules.tf.bak`)** — created before Stage 4. Automatically restored if `terraform plan` fails or if the run is a dry-run. Short-lived; deleted at end of run.
2. **Archival snapshots ([backups/](backups))** — created at the **start of every run**, named `match-rules.tf.<UTC-timestamp>.bak`. The 50 most recent are retained; older ones are pruned. To roll back manually:

   ```bash
   cp backups/match-rules.tf.20260603T024500Z.bak match-rules.tf
   ```

Both are listed in [.gitignore](.gitignore) and never committed.

---

## 10. Moving from local-testing to CI/CD

This repo currently runs locally. To productionise:

| Concern            | Local (today)                                       | CI/CD (target)                                                                                        |
| ------------------ | --------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Terraform backend  | Local — `terraform.tfstate` in repo root            | Remote backend (Azure Storage / S3 / Terraform Cloud) for state-locking and multi-engineer safety     |
| Akamai credentials | `~/.edgerc` on developer machine                    | Stored as pipeline secret variables, written to a temp `.edgerc` at the start of the job              |
| Approval gates     | Interactive `[y/N]` in [local_run.py](local_run.py) | Azure DevOps environment checks / PR approvals — replace the gate prompts with no-prompt mode         |
| Trigger            | Manual `python local_run.py …`                      | Pipeline triggered by commit to [input_pipeline_trigger.yaml](input_pipeline_trigger.yaml) + CSV      |
| Validation report  | Console output                                      | Posted to GitHub Step Summary by [scripts/validate_rules.py](scripts/validate_rules.py) automatically |
| Backups            | `backups/` folder on dev machine                    | Upload `backups/match-rules.tf.<ts>.bak` as a pipeline artefact per run                               |

[scripts/validate_rules.py](scripts/validate_rules.py) already writes the validation report to `$GITHUB_STEP_SUMMARY` and sets a `has_regex_rules` GitHub Output, so the script side is CI-ready.

---

## 11. Where to look when something breaks

| Symptom                                                              | Most likely cause                                                                                                               | Fix                                                                                                                               |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `Check 2 FAIL: '/foo' already exists in the live ruleset`            | Running `add` against rules that are already live                                                                               | Switch to `operation: update` (or change the rule's `name` / match criteria to make a distinct rule)                              |
| `Skipping update: Rule '…' not found` from `generate_match_rules.py` | The CSV's identity tuple drifted from what's in `match-rules.tf` (e.g. `match_value` typo)                                      | Read the live rule from `match-rules.tf` and align the CSV's `name`/`match_type`/`match_value`/`match_operator` exactly           |
| `/matchRules/N: Encountered null value` at terraform apply           | Wrong shape for that `match_type` (e.g. `clientip` with `object_match_value` set instead of flat `match_value`)                 | Check the per-`match_type` cheat sheet in [README.md](README.md); the validator now flags most of these pre-deploy                |
| One complex rule arrived at Akamai as two separate rules             | Continuation-row grouping broke — `redirect_url` typo on row 2, or a row inserted in between, or non-empty `match_url` on row 1 | See "Gotchas" in Section 7                                                                                                        |
| Regex rule pattern accepted by `re.compile` but rejected by Akamai   | The pattern uses constructs RE2 doesn't support (lookaheads, backreferences)                                                    | Re-write with RE2-supported syntax; [scripts/validate_regex_rules.py](scripts/validate_regex_rules.py) will catch this in Stage 2 |
| `match-rules.tf` looks corrupted after a failed run                  | Stage 4 wrote partial output, plan failed, rollback didn't trigger                                                              | Restore from `match-rules.tf.bak` (in-run) or the latest [backups/](backups) snapshot                                             |
| `terraform plan` shows unexpected deletions                          | Local `terraform.tfstate` out of sync with live Akamai state                                                                    | Re-import (`terraform import …`) or run `terraform refresh`                                                                       |

For the full troubleshooting list and per-error guidance, see [README.md](README.md#troubleshooting).

---

**Need more depth?** Everything in this document is backed by [README.md](README.md), which has the exhaustive column reference, per-`match_type` cheat sheet, CLI flag tables, and pipeline-internals detail.
