#!/usr/bin/env python3
"""
local_run.py — Local end-to-end test runner for Akamai ER Cloudlet pipeline.

Mirrors the Azure DevOps pipeline stages locally:
  Stage 1 — Parse input_pipeline_trigger.yaml
  Stage 2 — Run regex test cases  (if testing-required: true) [Skipped on Delete]
  Stage 3 — Run validate_rules.py [Runs checks 4,5,6 on Delete]
  Stage 4 — Run generate_match_rules.py  → match-rules.tf
  Stage 5 — Terraform init + plan  (always)
  Stage 6 — Terraform apply staging  (skipped on --dry-run)
  Stage 7 — Terraform apply prod     (skipped on --dry-run, requires --apply-prod)

Usage:
  python local_run.py                        # plan only (dry-run by default)
  python local_run.py --dry-run              # explicit dry-run (same as default)
  python local_run.py --apply-staging        # apply to staging, stop
  python local_run.py --apply-staging --apply-prod  # apply staging + prod
  python local_run.py --skip-terraform       # Python stages only, no TF
  python local_run.py --repo-root /path/to/repo     # override repo location

Requirements:
  pip install pyyaml colorama
  terraform must be on PATH
  ~/.edgerc must exist with a [default] section
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

# ── Optional colour support ───────────────────────────────────────────────────
try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
    def green(s):  return Fore.GREEN  + str(s) + Style.RESET_ALL
    def red(s):    return Fore.RED    + str(s) + Style.RESET_ALL
    def yellow(s): return Fore.YELLOW + str(s) + Style.RESET_ALL
    def cyan(s):   return Fore.CYAN   + str(s) + Style.RESET_ALL
    def bold(s):   return Style.BRIGHT + str(s) + Style.RESET_ALL
except ImportError:
    def green(s):  return str(s)
    def red(s):    return str(s)
    def yellow(s): return str(s)
    def cyan(s):   return str(s)
    def bold(s):   return str(s)

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml is not installed. Run: pip install pyyaml")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

DIVIDER      = "═" * 60
THIN_DIVIDER = "─" * 60

def header(title: str):
    print(f"\n{cyan(DIVIDER)}")
    print(f"  {bold(title)}")
    print(f"{cyan(DIVIDER)}")

def step(msg: str):
    print(f"\n  {cyan('▶')} {msg}")

def ok(msg: str):
    print(f"  {green('✅')} {msg}")

def warn(msg: str):
    print(f"  {yellow('⚠️ ')} {msg}")

def fail(msg: str):
    print(f"\n  {red('✗  FAILED:')} {msg}")

def abort(msg: str, stage: str):
    print(f"\n{red(DIVIDER)}")
    print(f"  {red('PIPELINE STOPPED')} at {stage}")
    print(f"  {msg}")
    print(f"{red(DIVIDER)}\n")
    sys.exit(1)

def run_cmd(cmd: list[str], cwd: Path, env: dict | None = None) -> tuple[int, str]:
    """Run a shell command, stream output, return (exit_code, combined_output)."""
    merged_env = {**os.environ, **(env or {})}
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=merged_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines = []
    for line in proc.stdout:
        print("    " + line, end="")
        lines.append(line)
    proc.wait()
    return proc.returncode, "".join(lines)

def confirm(prompt: str) -> bool:
    """Ask yes/no — returns True if user confirms."""
    while True:
        ans = input(f"\n  {yellow('?')} {prompt} [y/N] ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("", "n", "no"):
            return False


# ─────────────────────────────────────────────────────────────────────────────
#  Archival backup — runs on every pipeline invocation
# ─────────────────────────────────────────────────────────────────────────────

BACKUP_DIR_NAME = "backups"
BACKUP_RETENTION = 50  # keep the most recent N timestamped backups


def archive_timestamped_backup(repo_root: Path) -> Path | None:
    """
    Copy the current match-rules.tf into backups/match-rules.tf.<UTC-timestamp>.bak
    on every pipeline run, before any stage modifies it.

    Returns the path to the created backup, or None if there was nothing to back up.
    Old backups beyond BACKUP_RETENTION are pruned (oldest first).
    """
    src = repo_root / "match-rules.tf"
    if not src.exists():
        warn(f"No match-rules.tf to archive (skipping timestamped backup).")
        return None

    backup_dir = repo_root / BACKUP_DIR_NAME
    backup_dir.mkdir(exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = backup_dir / f"match-rules.tf.{ts}.bak"
    shutil.copy2(src, dest)

    # Prune oldest backups beyond retention
    existing = sorted(backup_dir.glob("match-rules.tf.*.bak"))
    if len(existing) > BACKUP_RETENTION:
        for old in existing[: len(existing) - BACKUP_RETENTION]:
            try:
                old.unlink()
            except OSError:
                pass

    print(f"\n{THIN_DIVIDER}")
    print(f"  Archived match-rules.tf → {dest.relative_to(repo_root)}")
    print(f"  (keeping last {BACKUP_RETENTION} backups in {BACKUP_DIR_NAME}/)")
    print(f"{THIN_DIVIDER}\n")
    return dest


# ─────────────────────────────────────────────────────────────────────────────
#  Stage 1 — Parse input_pipeline_trigger.yaml
# ─────────────────────────────────────────────────────────────────────────────

def stage_parse_input(repo_root: Path) -> dict:
    header("STAGE 1 — Parse input_pipeline_trigger.yaml")

    trigger_file = repo_root / "input_pipeline_trigger.yaml"
    if not trigger_file.exists():
        abort(f"input_pipeline_trigger.yaml not found at {trigger_file}", "Stage 1")

    step(f"Reading {trigger_file}")
    with open(trigger_file) as f:
        raw = f.read()

    print(f"\n{THIN_DIVIDER}")
    for line in raw.splitlines():
        print(f"    {line}")
    print(THIN_DIVIDER)

    cfg = yaml.safe_load(raw)

    operation        = str(cfg.get("operation", "Add")).strip()
    regex            = str(cfg.get("regex", False)).lower() in ("true", "1", "yes")
    testing_required = str(cfg.get("testing-required", False)).lower() in ("true", "1", "yes")
    tests            = cfg.get("tests", []) or []

    valid_ops = {"add", "update", "delete"}
    if operation.lower() not in valid_ops:
        abort(f"Invalid operation '{operation}'. Must be one of: {valid_ops}", "Stage 1")

    result = {
        "operation":        operation,
        "regex":            regex,
        "testing_required": testing_required,
        "tests":            tests,
    }

    print()
    print(f"    operation        : {green(operation)}")
    print(f"    regex            : {green(regex)}")
    print(f"    testing-required : {green(testing_required)}")
    print(f"    test cases       : {green(len(tests))}")

    ok("Input parsed successfully.")
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Stage 3 — validate_rules.py
# ─────────────────────────────────────────────────────────────────────────────

def stage_validate(repo_root: Path, cfg: dict, input_file: Path) -> bool:
    """Returns True if wildcard or regex rules were detected (triggers extra gate prompt)."""
    header("STAGE 3 — validate_rules.py")

    validate_script = repo_root / "scripts" / "validate_rules.py"
    if not validate_script.exists():
        abort(f"validate_rules.py not found at {validate_script}", "Stage 3")

    current_rules = repo_root / "match-rules.tf"
    report_path   = repo_root / "validation-report.txt"

    step(f"Input file    : {input_file}")
    step(f"Current rules : {current_rules}")

    if not input_file.exists():
        abort(f"Input file not found: {input_file}", "Stage 3")

    # Pass down the --operation argument dynamically so validate_rules.py 
    # knows whether to limit scope to checks 4, 5, and 6.
    cmd = [
        sys.executable, str(validate_script),
        "--input",         str(input_file),
        "--current-rules", str(current_rules),
        "--operation",     cfg["operation"],
    ]

    print()
    exit_code, output = run_cmd(cmd, cwd=repo_root)

    # Print report
    if report_path.exists():
        print(f"\n{THIN_DIVIDER}  VALIDATION REPORT  {THIN_DIVIDER}")
        print(report_path.read_text())
        print(THIN_DIVIDER)

    if exit_code == 1:
        abort("Hard validation errors detected. Fix input and re-run.", "Stage 3")

    # Detect wildcard or regex warnings in output
    wildcard_detected = False
    regex_detected = False
    for line in output.splitlines():
        if "Wildcard '*'" in line:
            wildcard_detected = True
        if "regex rules detected" in line or "requires regex-approval gate" in line:
            regex_detected = True

    if wildcard_detected or regex_detected:
        warn("Wildcard or regex rules detected — extra approval required.")
        warn("Continuing locally — review the report above carefully before proceeding.")
        approval_required = True
    else:
        ok("All validation checks passed.")
        approval_required = False

    return approval_required

# ─────────────────────────────────────────────────────────────────────────────
#  Stage 2 — Run regex test cases
# ─────────────────────────────────────────────────────────────────────────────

def stage_regex_tests(cfg: dict):
    header("STAGE 2 — Regex Test Cases")

    # Skip completely if this is a Delete operation
    if cfg.get("operation", "").lower() == "delete":
        step("Operation is Delete — skipping Stage 2 completely.")
        return

    if not cfg["testing_required"]:
        step("testing-required=false — skipping.")
        return

    if not cfg["regex"]:
        step("regex=false — no regex tests to run.")
        return

    tests = cfg["tests"]
    if not tests:
        warn("testing-required=true but no test cases defined in yaml.")
        return

    step(f"Running {len(tests)} test case(s)...")
    errors = []

    for i, t in enumerate(tests, 1):
        pattern      = t.get("regex", "")
        sample_url   = t.get("sampleURL", "")
        redirect_url = t.get("redirectURL", "")

        print(f"\n    Test {i}:")
        print(f"      regex      : {pattern}")
        print(f"      sampleURL  : {sample_url}")
        print(f"      redirectURL: {redirect_url}")

        try:
            compiled = re.compile(pattern)
        except re.error as e:
            msg = f"Invalid regex '{pattern}' — {e}"
            errors.append(f"Test {i}: {msg}")
            print(f"      {red('✗ ' + msg)}")
            continue

        match = compiled.search(sample_url)
        if match:
            try:
                result = compiled.sub(redirect_url, sample_url)
            except Exception:
                result = redirect_url
            print(f"      {green('✅ Matched')} → computed redirect: {result}")
        else:
            msg = f"sampleURL '{sample_url}' did NOT match regex '{pattern}'"
            errors.append(f"Test {i}: {msg}")
            print(f"      {red('✗ No match')}")

    if errors:
        print()
        for e in errors:
            fail(e)
        abort(f"{len(errors)} regex test(s) failed.", "Stage 2")

    ok(f"All {len(tests)} regex test(s) passed.")


# ─────────────────────────────────────────────────────────────────────────────
#  Approval gate simulation
# ─────────────────────────────────────────────────────────────────────────────

def gate_approval(label: str, instructions: str):
    header(f"🔐 APPROVAL GATE — {label}")
    print(f"\n  {instructions}\n")
    if not confirm(f"Approve and continue ({label})?"):
        abort("Rejected at approval gate — no changes applied.", f"Gate: {label}")
    ok(f"Approved: {label}")


# ─────────────────────────────────────────────────────────────────────────────
#  Stage 4 — generate_match_rules.py
# ─────────────────────────────────────────────────────────────────────────────

def stage_generate(repo_root: Path, cfg: dict, input_file: Path):
    header("STAGE 4 — generate_match_rules.py")

    generate_script = repo_root / "scripts" / "generate_match_rules.py"
    if not generate_script.exists():
        abort(f"generate_match_rules.py not found at {generate_script}", "Stage 4")

    cmd = [
        sys.executable, str(generate_script),
        "--input",    str(input_file),
        "--target",    str(repo_root / "match-rules.tf"),
        "--operation", cfg["operation"],
    ]

    step("Running generate_match_rules.py...")
    print()
    exit_code, _ = run_cmd(cmd, cwd=repo_root)

    if exit_code != 0:
        abort("generate_match_rules.py failed.", "Stage 4")

    match_rules_tf = repo_root / "match-rules.tf"
    print(f"\n{THIN_DIVIDER}  Generated match-rules.tf  {THIN_DIVIDER}")
    print(match_rules_tf.read_text())
    print(THIN_DIVIDER)

    ok("match-rules.tf generated.")


# ─────────────────────────────────────────────────────────────────────────────
#  Terraform helpers
# ─────────────────────────────────────────────────────────────────────────────

def build_tf_env(repo_root: Path) -> dict:
    edgerc = Path.home() / ".edgerc"
    if not edgerc.exists():
        abort("~/.edgerc not found. Configure Akamai credentials first.", "Terraform")

    env = {
        "AKAMAI_EDGERC":         str(edgerc),
        "AKAMAI_EDGERC_SECTION": "adv-sol",
        "TF_CLI_ARGS_init": (
            f'-backend-config="path={repo_root}/terraform.tfstate" '
            f'-reconfigure'
        ),
    }
    return env


def check_terraform():
    result = subprocess.run(
        ["terraform", "version"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        abort("terraform not found on PATH. Install Terraform >= 1.0 first.", "Terraform")
    version_line = result.stdout.splitlines()[0] if result.stdout else "unknown"
    step(f"Terraform found: {version_line}")


# ─────────────────────────────────────────────────────────────────────────────
#  Stage 5 — Terraform init + plan
# ─────────────────────────────────────────────────────────────────────────────

def stage_tf_plan(repo_root: Path) -> bool:
    """Returns True if there are changes to apply."""
    header("STAGE 5 — Terraform Init & Plan")

    check_terraform()
    tf_env = build_tf_env(repo_root)

    step("terraform init (local backend, state in repo root)...")
    print()
    exit_code, _ = run_cmd(["terraform", "init"], cwd=repo_root, env=tf_env)
    if exit_code != 0:
        abort("terraform init failed.", "Stage 5")

    plan_file = repo_root / "tfplan.local"
    step("terraform plan...")
    print()
    exit_code, _ = run_cmd(
        ["terraform", "plan", f"-out={plan_file}", "-detailed-exitcode"],
        cwd=repo_root,
        env=tf_env,
    )

    if exit_code == 0:
        warn("No infrastructure changes detected.")
        return False
    elif exit_code == 1:
        abort("terraform plan failed.", "Stage 5")
    else:
        ok("Plan complete — changes detected. Plan saved to tfplan.local")
        return True


# ─────────────────────────────────────────────────────────────────────────────
#  Stage 6 — Terraform apply staging
# ─────────────────────────────────────────────────────────────────────────────

def stage_tf_apply_staging(repo_root: Path):
    header("STAGE 6 — Terraform Apply — Staging")

    tf_env    = build_tf_env(repo_root)
    plan_file = repo_root / "tfplan.local"

    step("Applying: akamai_cloudlets_policy + policy_activation_staging")
    print()

    cmd = [
        "terraform", "apply", "-auto-approve",
        "-target=akamai_cloudlets_policy.policy",
        "-target=akamai_cloudlets_policy_activation.policy_activation_staging",
    ]

    exit_code, _ = run_cmd(cmd, cwd=repo_root, env=tf_env)
    if exit_code != 0:
        abort("terraform apply (staging) failed.", "Stage 6")

    ok("Staging deploy complete.")


# ─────────────────────────────────────────────────────────────────────────────
#  Stage 7 — Terraform apply production
# ─────────────────────────────────────────────────────────────────────────────

def stage_tf_apply_prod(repo_root: Path, cfg: dict):
    header("STAGE 7 — Terraform Apply — Production")

    tf_env = build_tf_env(repo_root)

    step("Applying: policy_activation_prod (staging NOT re-touched)")
    print()

    cmd = [
        "terraform", "apply", "-auto-approve",
        "-target=akamai_cloudlets_policy_activation.policy_activation_prod",
    ]

    exit_code, _ = run_cmd(cmd, cwd=repo_root, env=tf_env)
    if exit_code != 0:
        abort("terraform apply (production) failed.", "Stage 7")

    ok("Production deploy complete.")

    audit_log = repo_root / "local-deploy-audit.log"
    import datetime
    entry = (
        f"{datetime.datetime.now().isoformat()} | "
        f"operation={cfg['operation']} | "
        f"user={os.environ.get('USER', 'unknown')} | "
        f"prod deploy\n"
    )
    with open(audit_log, "a") as f:
        f.write(entry)
    ok(f"Audit entry written to {audit_log}")


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Local end-to-end test runner — mirrors the Azure DevOps pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          python local_run.py
          python local_run.py --apply-staging
          python local_run.py --apply-staging --apply-prod
          python local_run.py --skip-terraform
          python local_run.py --repo-root ~/projects/cloudlet
        """),
    )
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--apply-staging", action="store_true", default=False)
    parser.add_argument("--apply-prod", action="store_true", default=False)
    parser.add_argument("--skip-terraform", action="store_true", default=False)
    parser.add_argument("--repo-root", type=str, default=None)
    parser.add_argument("--no-prompt", action="store_true", default=False)
    parser.add_argument(
        "--input-file",
        type=str,
        default=None,
        help="Path to the input CSV (default: <repo-root>/rules/simple_rules_pass.csv). "
             "Relative paths are resolved against the repo root.",
    )

    args = parser.parse_args()

    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()
    else:
        repo_root = Path(__file__).resolve().parent

    if not repo_root.is_dir():
        print(f"ERROR: repo root does not exist: {repo_root}")
        sys.exit(1)

    if args.apply_prod and not args.apply_staging:
        print("ERROR: --apply-prod requires --apply-staging")
        sys.exit(1)

    if args.input_file:
        input_file = Path(args.input_file)
        if not input_file.is_absolute():
            input_file = (repo_root / input_file).resolve()
    else:
        input_file = repo_root / "rules" / "simple_rules_pass.csv"

    if not input_file.exists():
        print(f"ERROR: input file not found: {input_file}")
        sys.exit(1)

    dry_run = args.dry_run or (not args.apply_staging and not args.apply_prod)

    print(f"\n{cyan(DIVIDER)}")
    print(f"  {bold('Akamai ER Cloudlet — Local Pipeline Runner')}")
    print(f"{cyan(DIVIDER)}")
    print(f"  Repo root     : {repo_root}")
    print(f"  Input file    : {input_file}")
    print(f"  Dry run       : {yellow(dry_run)}")
    print(f"  Apply staging : {green(args.apply_staging)}")
    print(f"  Apply prod    : {green(args.apply_prod)}")
    print(f"  Skip Terraform: {yellow(args.skip_terraform)}")

    # ── Archival backup: every run, timestamped, kept regardless of outcome ──
    archive_timestamped_backup(repo_root)

    # ── Stage 1: Parse input yaml ─────────────────────────────
    cfg = stage_parse_input(repo_root)

    # ── Stage 2: Regex tests (Skips if operation is 'Delete') ──
    stage_regex_tests(cfg)

    # ── Stage 3: Validate rules ───────────────────────────────
    approval_required = stage_validate(repo_root, cfg, input_file)

    # ── Approval gate simulation ──────────────────────────────
    if not args.no_prompt:
        if approval_required:
            gate_approval(
                "validation-gate",
                "Wildcard or regex rules detected. Review the validation report above.\n"
                "  Confirm all warnings are understood and checks passed before proceeding.",
            )
        else:
            gate_approval(
                "validation-gate",
                "Review the validation report above.\n"
                "  Confirm all checks passed before proceeding.",
            )

    # ── Stage 4: Generate match-rules.tf ─────────────────────
    match_rules_tf = repo_root / "match-rules.tf"
    backup_tf = repo_root / "match-rules.tf.bak"
    if match_rules_tf.exists():
        shutil.copy2(match_rules_tf, backup_tf)
    stage_generate(repo_root, cfg, input_file)

    if args.skip_terraform:
        header("✅ Done — Terraform skipped (--skip-terraform)")
        print(f"\n  Python stages complete. match-rules.tf has been generated.")
        sys.exit(0)

    # ── Stage 5: Terraform plan ───────────────────────────────
    try:
        has_changes = stage_tf_plan(repo_root)
    except SystemExit:
        if backup_tf.exists():
            shutil.copy2(backup_tf, match_rules_tf)
            print(f"\n{THIN_DIVIDER}\nRestored match-rules.tf from backup after plan failure.\n{THIN_DIVIDER}\n")
        raise

    if dry_run:
        if backup_tf.exists():
            shutil.copy2(backup_tf, match_rules_tf)
            print(f"\n{THIN_DIVIDER}\nRestored match-rules.tf from backup after dry run.\n{THIN_DIVIDER}\n")
            backup_tf.unlink()
        header("✅ Dry Run Complete")
        sys.exit(0)

    if not has_changes:
        header("✅ No Changes")
        if backup_tf.exists():
            backup_tf.unlink()
        sys.exit(0)

    # ── Stage 6: Apply staging ────────────────────────────────
    if args.apply_staging:
        if not args.no_prompt:
            gate_approval(
                "staging",
                "About to apply to Akamai STAGING.\n"
                "  This will push changes to the Akamai staging network.",
            )
        stage_tf_apply_staging(repo_root)

    # ── Stage 7: Apply production ─────────────────────────────
    if args.apply_prod:
        if not args.no_prompt:
            gate_approval(
                "production",
                "About to apply to Akamai PRODUCTION.\n"
                "  Verify staging is clean before confirming.\n"
                "  This will push changes live to production.",
            )
        stage_tf_apply_prod(repo_root, cfg)

    header("✅ Pipeline Complete")


if __name__ == "__main__":
    main()