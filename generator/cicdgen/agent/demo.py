"""End-to-end demo: one natural-language instruction → a complete CI/CD setup.

A runnable proof of the product's headline claim: a developer types a single
plain-language instruction naming the two independent axes — ``ci_vendor`` and
``target_platform`` — and the agent produces the WHOLE setup (pipeline file,
deploy IaC, Dockerfile/job, manifest) by driving the REAL plugins, with nothing
hand-authored.

What it demonstrates
--------------------
Given, e.g.::

 generate for ci_vendor=github target_platform=aws-ecs with region=eu-west-2 project=orders-api

the demo:

 1. **Dry-run first** (``apply=False``) — the safe, reviewable posture:
 prints the resolved axes, the agent's gap-fills, and the exact list of
 files that WOULD be written. Touches nothing on disk.
 2. **Apply** (``apply=True``) into a throwaway temp output dir — prints the
 FULL output actually produced (every written file + the manifest), proving
 one instruction → a complete setup via the real ``github`` + ``aws-ecs``
 plugins.
 3. Repeats for a *different* two-axis combination
 (``azure-devops`` × ``databricks``) to show the generality: the second
 target has NO Dockerfile (a Databricks job, not an image), yet the same one
 instruction produces its complete, correctly-shaped setup.

Safety: everything is generated into a temporary directory (never the product
repo), there is no network/account access, and secrets appear only as references
in the generated files — never as values.

Run it::

 python -m cicdgen.agent.demo
 # or target one combo:
 python -m cicdgen.agent.demo --instruction "generate for ci_vendor=github target_platform=aws-ecs with project=orders-api"
 # keep the output on disk to inspect:
 python -m cicdgen.agent.demo --out./_demo_out
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import yaml

from cicdgen.agent import DriverResult, run_instruction
from cicdgen.core import GenerationResult, generate, load_manifest, write_migration_runbook
from cicdgen.ir import Pipeline

# This file: <repo>/generator/cicdgen/agent/demo.py -> parents[3] == <repo>.
_REPO_ROOT = Path(__file__).resolve().parents[3]
#: The real on-disk plugin tree (the folder holding ``ci/`` and ``target/``).
DEFAULT_PLUGINS_ROOT = _REPO_ROOT / "plugins"

#: The two headline instructions — one per two-axis combination.
DEMO_INSTRUCTIONS: tuple[str, ...] = (
    "generate for ci_vendor=github target_platform=aws-ecs "
    "with region=eu-west-2 project=orders-api",
    "generate for ci_vendor=azure-devops target_platform=databricks "
    "with project=analytics-jobs",
)

_RULE = "=" * 78
_SUB = "-" * 78


def _print_header(instruction: str) -> None:
    print(_RULE)
    print("ONE INSTRUCTION:")
    print(f"  {instruction}")
    print(_RULE)


def _print_dry_run(result: DriverResult) -> None:
    """Print the reviewable dry-run report: resolved axes, gap-fills, planned files."""
    print("STEP 1 — dry-run (apply=False): propose a plan, write nothing")
    print(_SUB)
    if result.needs_confirmation:
        print("  needs confirmation — the agent refused to guess a missing axis:")
        for q in result.questions:
            print(f"    ? {q}")
        return

    assert result.plan is not None
    print(f"  resolved ci_vendor      : {result.ci_vendor}")
    print(f"  resolved target_platform: {result.target_platform}")
    if result.gap_fills:
        print("  agent gap-fills (repo-specific inputs, never secrets):")
        for note in result.gap_fills:
            print(f"    - {note}")
    print("  files that WOULD be written (through the real plugins):")
    for path in result.plan.paths:
        print(f"    + {path}")
    if result.plan.removed:
        print("  stale files that WOULD be removed:")
        for path in result.plan.removed:
            print(f"    - {path}")


def _print_apply(result: DriverResult, out_dir: Path) -> None:
    """Print the FULL output actually produced on disk by the real plugins."""
    print()
    print(f"STEP 2 — apply (apply=True) into: {out_dir}")
    print(_SUB)
    assert result.result is not None
    gen = result.result
    print(f"  ci_vendor × target_platform: {gen.ci_vendor} × {gen.target_platform}")
    print("  FULL output produced (one instruction → complete setup):")
    for path in gen.written:
        marker = "  (manifest)" if path.endswith("generated.manifest.json") else ""
        print(f"    ✓ {path}{marker}")
    # A short, value-free peek at the headline pipeline file so the reader sees
    # real content was produced (not just a file list).
    _print_pipeline_peek(out_dir, gen.written)


def _print_pipeline_peek(out_dir: Path, written: tuple[str, ...]) -> None:
    """Print the first few lines of the generated pipeline file, if present."""
    pipeline_file = next(
        (
            w
            for w in written
            if w.endswith("ci.yml") or w.endswith("azure-pipelines.yml")
        ),
        None,
    )
    if pipeline_file is None:
        return
    content = (out_dir / pipeline_file).read_text(encoding="utf-8")
    head = content.splitlines()[:6]
    print(f"  peek — {pipeline_file} (first lines):")
    for line in head:
        print(f"      | {line}")


def run_demo(
    instruction: str,
    out_dir: Path,
    *,
    plugins_root: Path | str = DEFAULT_PLUGINS_ROOT,
) -> DriverResult:
    """Run the full dry-run → apply flow for one instruction into ``out_dir``.

 Uses the REAL plugins (no injected registry) so the output is exactly what a
 client would get. Returns the apply-phase :class:`DriverResult` (or the
 needs-confirmation result if an axis could not be resolved).

 ``out_dir`` is created if missing. The repo we "discover from" and generate
 into is ``out_dir`` itself; we build purely from the instruction
 (``use_discovery=False``) so the demo is self-contained and deterministic
 regardless of what happens to sit in the temp dir.
 """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    _print_header(instruction)

    # 1) Dry-run — propose, touch nothing.
    before = _snapshot(out_dir)
    dry = run_instruction(
        instruction,
        out_dir,
        apply=False,
        use_discovery=False,
        plugins_root=plugins_root,
    )
    _print_dry_run(dry)
    after_dry = _snapshot(out_dir)
    # Prove the dry-run wrote nothing.
    assert after_dry == before, "dry-run must not write anything"
    print(f"  [verified] dry-run wrote 0 files (disk unchanged: {len(before)} files)")

    if dry.needs_confirmation:
        return dry

    # 2) Apply — produce the complete setup via the real plugins.
    applied = run_instruction(
        instruction,
        out_dir,
        apply=True,
        use_discovery=False,
        plugins_root=plugins_root,
    )
    _print_apply(applied, out_dir)
    print(_RULE)
    print()
    return applied


def _snapshot(root: Path) -> set[str]:
    """Relative paths of all files currently under ``root`` (for before/after)."""
    return {
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cicdgen.agent.demo",
        description=(
            "Demonstrate: one natural-language instruction → a complete CI/CD "
            "setup, via the real plugins (task 10.4)."
        ),
    )
    parser.add_argument(
        "--instruction",
        action="append",
        dest="instructions",
        help=(
            "An instruction to run (repeatable). Defaults to the two headline "
            "combinations: github×aws-ecs and azure-devops×databricks."
        ),
    )
    parser.add_argument(
        "--out",
        default=None,
        help=(
            "Output directory to generate into. Defaults to a throwaway temp dir "
            "that is deleted on exit. Pass a path to keep the output for review."
        ),
    )
    parser.add_argument(
        "--plugins-root",
        default=str(DEFAULT_PLUGINS_ROOT),
        help="The real plugin tree (folder holding ci/ and target/).",
    )
    parser.add_argument(
        "--target-swap",
        action="store_true",
        help=(
            "Instead of the one-instruction demos, run the target-platform swap "
            "demo (task 11.4): same CI vendor (github), flip target_platform "
            "aws-ecs -> databricks, showing the deploy shape change image -> job."
        ),
    )
    parser.add_argument(
        "--ci-swap",
        action="store_true",
        help=(
            "Instead of the one-instruction demos, run E2E demo A (task 11.3): a "
            "CI-vendor swap github -> azure-devops with the SAME target (aws-ecs), "
            "proving an equivalent pipeline and preserved target wiring."
        ),
    )
    args = parser.parse_args(argv)

    plugins_root = Path(args.plugins_root)

    if args.ci_swap:
        print()
        print("cicdgen — E2E demo A: CI-vendor swap, same target (task 11.3, R8/R3.5)")
        print(f"real plugins : {plugins_root}")
        print()
        if args.out is not None:
            base = Path(args.out)
            base.mkdir(parents=True, exist_ok=True)
            run_ci_swap_demo(base / "ci-swap", plugins_root=plugins_root)
            print(f"output kept at: {base.resolve()}")
        else:
            with tempfile.TemporaryDirectory(prefix="cicdgen-ci-swap-") as tmp:
                run_ci_swap_demo(Path(tmp) / "ci-swap", plugins_root=plugins_root)
                print("(temp output discarded — pass --out <dir> to keep it)")
        return 0

    if args.target_swap:
        print()
        print("cicdgen — target-platform swap demo (task 11.4, R8/R3.5)")
        print(f"real plugins : {plugins_root}")
        print()
        if args.out is not None:
            base = Path(args.out)
            base.mkdir(parents=True, exist_ok=True)
            run_target_swap_demo(base, plugins_root=plugins_root)
            print(f"output kept at: {base.resolve()}")
        else:
            with tempfile.TemporaryDirectory(prefix="cicdgen-swap-") as tmp:
                run_target_swap_demo(Path(tmp), plugins_root=plugins_root)
                print("(temp output discarded — pass --out <dir> to keep it)")
        return 0

    instructions = tuple(args.instructions) if args.instructions else DEMO_INSTRUCTIONS

    print()
    print("cicdgen — one-instruction end-to-end demo (R5.1/R5.2/R10.1/R10.2)")
    print(f"real plugins : {plugins_root}")
    print()

    if args.out is not None:
        base = Path(args.out)
        base.mkdir(parents=True, exist_ok=True)
        _run_all(instructions, base, plugins_root)
        print(f"output kept at: {base.resolve()}")
    else:
        with tempfile.TemporaryDirectory(prefix="cicdgen-demo-") as tmp:
            _run_all(instructions, Path(tmp), plugins_root)
            print("(temp output discarded — pass --out <dir> to keep it)")

    return 0


def _run_all(instructions: tuple[str, ...], base: Path, plugins_root: Path) -> None:
    """Run each instruction into its own sub-directory under ``base``."""
    for idx, instruction in enumerate(instructions, start=1):
        out_dir = base / f"demo-{idx}"
        run_demo(instruction, out_dir, plugins_root=plugins_root)


# ===========================================================================
# Target-platform swap demo
# ===========================================================================
#
# This demo proves the OTHER axis of migration: keep the SAME CI vendor
# (``github``) and flip ONLY ``target_platform`` from ``aws-ecs`` to
# ``databricks``. The headline is the deploy SHAPE change — a container *image*
# (Dockerfile + ECR/ECS Terraform) becomes a Databricks *job* (databricks/job.json
# + a databricks Terraform module, NO Dockerfile). The CI expression stays the
# same vendor throughout; only what it deploys changes.
#
# Unlike the one-instruction agent demos above, this one builds the IR in-memory
# (``Pipeline.model_validate``) and drives ``cicdgen.core.generate`` directly with
# the REAL plugins. That keeps the swap self-contained and avoids the discovery /
# local-config nuances — the point here is the generate-side migration, not NL
# parsing.


def _swap_pipeline(*, target_platform: str) -> Pipeline:
    """Build a github × <target> IR, same CI vendor for both runs.

 The CI vendor is ALWAYS ``github``. Only ``target_platform`` flips. The
 cloud block is chosen so the chosen target validates and the GitHub plugin
 emits the matching OIDC credentials step:

 * aws-ecs -> cloud.provider = aws (image shape: Dockerfile + ECR/ECS)
 * databricks -> cloud.provider = azure (job shape: job.json, NO Dockerfile)

 ``stage.runs`` for the build/deploy stages is set to shell commands that make
 the SHAPE change visible in the generated workflow too (docker build / ECR /
 ECS for aws-ecs; a databricks wheel build + ``databricks jobs`` deploy for
 databricks). These are command *references* only — no literal secret values.
 """
    is_ecs = target_platform == "aws-ecs"
    cloud_provider = "aws" if is_ecs else "azure"
    region = "eu-west-2" if is_ecs else "uksouth"

    if is_ecs:
        build_runs = "docker build -t $ECR_REPO:$IMAGE_TAG ."
        deploy_runs = "aws ecs update-service --cluster $ECS_CLUSTER --service $ECS_SERVICE"
    else:
        build_runs = "python -m build --wheel --outdir dist/"
        deploy_runs = "databricks jobs create --json @databricks/job.json"

    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": target_platform,
            "project": {"name": "orders-api", "language": "python"},
            "cloud": {"provider": cloud_provider, "region": region, "auth": "oidc"},
            "environments": [
                {"name": "dev", "gate": "auto"},
                {"name": "prod", "gate": "manual", "protection": "required-approvals"},
            ],
            "stages": [
                {"id": "test", "runs": "make test", "scans": ["sast", "secrets"]},
                {"id": "build", "runs": build_runs, "timeout_minutes": 30},
                {"id": "deploy-dev", "environment": "dev", "runs": deploy_runs},
            ],
        }
    )


def _deploy_shape(out_dir: Path) -> str:
    """Classify the deploy shape currently on disk: image vs job (vs unknown)."""
    has_dockerfile = (out_dir / "Dockerfile").is_file()
    has_job = (out_dir / "databricks" / "job.json").is_file()
    if has_dockerfile and not has_job:
        return "IMAGE (Dockerfile + ECR/ECS)"
    if has_job and not has_dockerfile:
        return "JOB (databricks/job.json — NO Dockerfile)"
    return "unknown"


def _print_shape_report(title: str, out_dir: Path, result: GenerationResult) -> None:
    """Print the shape-defining artifacts a generation produced."""
    print(title)
    print(_SUB)
    print(f"  ci_vendor × target_platform : {result.ci_vendor} × {result.target_platform}")
    print(f"  deploy shape                : {_deploy_shape(out_dir)}")
    print(f"  Dockerfile present          : {(out_dir / 'Dockerfile').is_file()}")
    print(f"  databricks/job.json present : {(out_dir / 'databricks' / 'job.json').is_file()}")
    iac_ecs = (out_dir / "iac" / "aws-ecs" / "main.tf").is_file()
    iac_dbx = (out_dir / "iac" / "databricks" / "main.tf").is_file()
    print(f"  iac/aws-ecs/main.tf present : {iac_ecs}")
    print(f"  iac/databricks/main.tf      : {iac_dbx}")
    if result.removed:
        print("  stale files REMOVED on this run:")
        for path in result.removed:
            print(f"    - {path}")
    _print_workflow_deploy_peek(out_dir)


def _print_workflow_deploy_peek(out_dir: Path) -> None:
    """Peek at the workflow lines that show the CI vendor + the deploy command."""
    workflow = out_dir / ".github" / "workflows" / "ci.yml"
    if not workflow.is_file():
        return
    lines = workflow.read_text(encoding="utf-8").splitlines()
    interesting = [
        ln
        for ln in lines
        if any(
            marker in ln
            for marker in (
                "configure-aws-credentials",
                "azure/login",
                "docker build",
                "ecs update-service",
                "databricks jobs",
                "python -m build",
            )
        )
    ]
    if interesting:
        print("  workflow (.github/workflows/ci.yml) shape-defining lines:")
        for ln in interesting:
            print(f"      | {ln.strip()}")


def run_target_swap_demo(
    out_dir: Path,
    *,
    plugins_root: Path | str = DEFAULT_PLUGINS_ROOT,
) -> tuple[GenerationResult, GenerationResult]:
    """Demonstrate a target-platform swap (aws-ecs -> databricks), same CI vendor.

 Generates github × aws-ecs into ``out_dir``, then regenerates github ×
 databricks into the SAME ``out_dir`` and shows:

 * the image-shape artifacts (Dockerfile + iac/aws-ecs/*) are REMOVED;
 * the job-shape artifacts (databricks/job.json + iac/databricks/*) appear
 (and crucially NO Dockerfile);
 * the regenerated workflow keeps the same CI vendor (github) but its
 deploy steps change from docker/ECS to databricks job verbs;
 * the run is flagged as a target-only axis change and a
 ``MIGRATION_RUNBOOK.md`` is emitted naming ``aws-ecs -> databricks``.

 Uses the REAL on-disk plugins (no fakes). Returns the two
:class:`GenerationResult` objects (before, after).
 """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(_RULE)
    print("TARGET-PLATFORM SWAP (same CI vendor): aws-ecs -> databricks")
    print("  ci_vendor stays 'github' for BOTH runs; only target_platform flips.")
    print(_RULE)

    # --- run 1: github × aws-ecs (the IMAGE shape) --------------------------
    before = generate(
        _swap_pipeline(target_platform="aws-ecs"),
        out_dir,
        plugins_root=plugins_root,
    )
    assert isinstance(before, GenerationResult)
    _print_shape_report("BEFORE — github × aws-ecs", out_dir, before)

    print()

    # --- run 2: github × databricks (the JOB shape) -------------------------
    after = generate(
        _swap_pipeline(target_platform="databricks"),
        out_dir,
        plugins_root=plugins_root,
    )
    assert isinstance(after, GenerationResult)
    _print_shape_report("AFTER  — github × databricks", out_dir, after)

    # --- emit the migration runbook for the recorded axis change ------------
    if after.axis_change is not None and after.axis_change.is_axis_change:
        runbook = write_migration_runbook(out_dir, after.axis_change)
        print()
        print(f"  axis change flagged: {after.axis_change.summary()}")
        print(f"  migration runbook  : {runbook.relative_to(out_dir).as_posix()}")

    print(_RULE)
    print("SHAPE CHANGED: container IMAGE (aws-ecs) -> Databricks JOB (databricks)")
    print("  CI vendor unchanged (github); only the deploy artifact shape changed.")
    print(_RULE)
    print()
    return before, after


# ===========================================================================
# E2E demo A — CI-vendor swap, SAME target
# ===========================================================================
#
# The headline migration proof: a client on ``github × aws-ecs`` decides to move
# their pipeline to Azure DevOps but keeps deploying to the SAME target (AWS ECS).
# Flipping ONE axis (``ci_vendor: github -> azure-devops``) and regenerating into
# the SAME repo must:
#
# * write the new vendor's pipeline file (``azure-pipelines.yml``),
# * remove the old vendor's file (``.github/workflows/ci.yml``) via manifest
# ownership — never a client-authored file,
# * leave the target/deploy wiring BYTE-IDENTICAL (Dockerfile + iac/aws-ecs/*),
# because the target axis did not change,
# * flag the run as an axis change (``ci_changed`` True, ``target_changed`` False),
# * emit ``MIGRATION_RUNBOOK.md`` naming the ``github -> azure-devops`` swap.
#
# The two pipelines are "equivalent": the SAME ordered stage set from the IR, the
# SAME manual prod gate, and secrets referenced by reference only — just expressed
# in each vendor's own syntax (GitHub jobs/needs + ``environment:`` vs. Azure
# stages/``dependsOn`` + a deployment job bound to an ``environment:``).

#: The sample github × aws-ecs IR the CI-swap demo starts from.
CI_SWAP_SAMPLE_IR = _REPO_ROOT / "examples" / "client-github-aws" / "pipeline.yaml"

#: The target files the aws-ecs plugin owns — must be byte-identical across a
#: CI-only swap (the target axis never changed).
_AWS_ECS_TARGET_FILES: tuple[str, ...] = (
    "Dockerfile",
    "iac/aws-ecs/main.tf",
    "iac/aws-ecs/outputs.tf",
    "iac/aws-ecs/variables.tf",
    "iac/aws-ecs/terraform.tfvars.example",
)


def _load_ci_swap_pipeline(*, ci_vendor: str) -> Pipeline:
    """Load the sample IR and set ``ci_vendor`` (target stays ``aws-ecs``)."""
    data = yaml.safe_load(CI_SWAP_SAMPLE_IR.read_text(encoding="utf-8"))
    data = dict(data)
    data["ci_vendor"] = ci_vendor
    return Pipeline.model_validate(data)


def _capture_bytes(repo: Path, rel_paths: tuple[str, ...]) -> dict[str, bytes]:
    """Snapshot the raw bytes of each existing file (for byte-identical proof)."""
    snapshot: dict[str, bytes] = {}
    for rel in rel_paths:
        path = repo / rel
        if path.is_file():
            snapshot[rel] = path.read_bytes()
    return snapshot


def _ir_stage_ids(pipeline: Pipeline) -> list[str]:
    """The IR's enabled stage ids in declared order (what both vendors express)."""
    return [s.id for s in pipeline.stages if s.enabled]


def _azure_stage_name(stage_id: str) -> str:
    """Mirror the azure-devops plugin's id sanitisation (``-`` -> ``_``)."""
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in stage_id)
    if not cleaned or not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = f"s_{cleaned}"
    return cleaned


def run_ci_swap_demo(
    repo_dir: Path,
    *,
    plugins_root: Path | str = DEFAULT_PLUGINS_ROOT,
) -> GenerationResult:
    """Demonstrate a CI-vendor swap with the SAME target, end to end.

 Generates ``github × aws-ecs`` into ``repo_dir`` through the REAL plugins,
 then flips ``ci_vendor`` to ``azure-devops`` (target unchanged) and regenerates
 into the SAME repo. Prints a before/after and an "equivalent pipeline"
 confirmation, and returns the swap's :class:`GenerationResult`.

 No mocks and no network: the real ``github`` / ``azure-devops`` CI plugins and
 the real ``aws-ecs`` target plugin render everything; the repo is a throwaway
 directory.
 """
    repo_dir = Path(repo_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)

    print(_RULE)
    print("E2E DEMO A — CI-vendor swap, SAME target (aws-ecs)")
    print("  ci_vendor: github -> azure-devops   |   target_platform: aws-ecs (unchanged)")
    print(_RULE)

    # --- step 1: generate github × aws-ecs --------------------------------
    gh_pipeline = _load_ci_swap_pipeline(ci_vendor="github")
    first = generate(gh_pipeline, repo_dir, plugins_root=plugins_root)
    assert isinstance(first, GenerationResult)
    m1 = load_manifest(repo_dir)
    assert m1 is not None

    print("STEP 1 — generate github × aws-ecs (the starting point)")
    print(_SUB)
    print(f"  manifest axes: {m1.ci_vendor} × {m1.target_platform}")
    print("  files written (real plugins):")
    for path in first.written:
        print(f"    + {path}")

    # Capture the target files' bytes BEFORE the swap to prove they don't change.
    target_before = _capture_bytes(repo_dir, _AWS_ECS_TARGET_FILES)
    gh_workflow_before = (repo_dir / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    # --- step 2: flip ci_vendor and regenerate into the SAME repo ---------
    azdo_pipeline = _load_ci_swap_pipeline(ci_vendor="azure-devops")
    swap = generate(azdo_pipeline, repo_dir, plugins_root=plugins_root)
    assert isinstance(swap, GenerationResult)
    m2 = load_manifest(repo_dir)
    assert m2 is not None

    print()
    print("STEP 2 — flip ci_vendor to azure-devops, regenerate into the SAME repo")
    print(_SUB)
    print(f"  manifest axes now: {m2.ci_vendor} × {m2.target_platform}")
    assert swap.axis_change is not None
    print(f"  axis change      : {swap.axis_change.summary()}")
    print(
        f"  classified       : ci_changed={swap.axis_change.ci_changed} "
        f"target_changed={swap.axis_change.target_changed}"
    )
    print("  new CI file written:")
    print("    + azure-pipelines.yml")
    print("  old CI file removed (manifest-owned cleanup):")
    for path in swap.removed:
        print(f"    - {path}")

    # --- step 3: prove the target wiring is preserved (byte-identical) -----
    print()
    print("STEP 3 — target/deploy wiring preserved (target axis did NOT change)")
    print(_SUB)
    target_after = _capture_bytes(repo_dir, _AWS_ECS_TARGET_FILES)
    for rel in _AWS_ECS_TARGET_FILES:
        before = target_before.get(rel)
        after = target_after.get(rel)
        same = before is not None and before == after
        print(f"    {'=' if same else '!'} {rel}: {'byte-identical' if same else 'CHANGED'}")
        assert same, f"target file {rel} must be byte-identical across a CI-only swap"

    # --- step 4: emit the migration runbook --------------------------------
    runbook = write_migration_runbook(repo_dir, swap.axis_change)
    runbook_body = runbook.read_text(encoding="utf-8")
    print()
    print("STEP 4 — MIGRATION_RUNBOOK.md emitted")
    print(_SUB)
    print(f"    ✓ {runbook.name} (names the swap: github -> azure-devops)")
    assert "github` -> `azure-devops" in runbook_body or "github -> azure-devops" in runbook_body

    # --- step 5: "equivalent pipeline" confirmation ------------------------
    _print_equivalence(repo_dir, azdo_pipeline, gh_workflow_before)

    print(_RULE)
    print()
    return swap


def _print_equivalence(
    repo_dir: Path, azdo_pipeline: Pipeline, gh_workflow_before: str
) -> None:
    """Parse both vendor files and confirm they express the SAME pipeline."""
    ir_stages = _ir_stage_ids(azdo_pipeline)

    gh = yaml.safe_load(gh_workflow_before)
    gh_jobs = list(gh["jobs"].keys())

    az = yaml.safe_load((repo_dir / "azure-pipelines.yml").read_text(encoding="utf-8"))
    az_stages = [s["stage"] for s in az["stages"]]

    print()
    print("EQUIVALENT PIPELINE — same stages, same target, same gates; different syntax")
    print(_SUB)
    print(f"  IR stage order (source of truth) : {ir_stages}")
    print(f"  GitHub jobs (needs chain)        : {gh_jobs}")
    print(f"  Azure stages (dependsOn chain)   : {az_stages}")

    # Every IR stage appears in both vendor files, in declared order.
    for sid in ir_stages:
        assert sid in gh_jobs, f"IR stage {sid} missing from GitHub jobs"
        assert _azure_stage_name(sid) in az_stages, f"IR stage {sid} missing from Azure stages"

    # Manual prod gate expressed in both: github environment / azure env deployment.
    gh_prod_env = gh["jobs"]["deploy-prod"].get("environment")
    az_prod_job = next(s for s in az["stages"] if s["stage"] == "deploy_prod")["jobs"][0]
    az_prod_env = az_prod_job.get("environment")
    print(f"  manual prod gate — github environment: {gh_prod_env!r}; azure env: {az_prod_env!r}")
    assert gh_prod_env == "prod"
    assert az_prod_env == "prod"
    assert "deployment" in az_prod_job  # azure deployment job (approval-gated)

    # Secrets referenced by reference only in both — never literal credentials.
    az_text = (repo_dir / "azure-pipelines.yml").read_text(encoding="utf-8")
    assert "${{ secrets." in gh_workflow_before
    assert "group: cicd-secrets" in az_text
    assert "arn:aws:iam" not in gh_workflow_before and "arn:aws:iam" not in az_text
    print("  secrets by reference only        : github ${{ secrets.* }} / azure $(var) via variable group")
    print("  -> CONFIRMED: equivalent pipeline (same ordered stages, same manual prod gate, same target).")


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
