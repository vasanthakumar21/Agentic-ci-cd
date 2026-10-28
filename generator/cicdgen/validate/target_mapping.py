"""Validate a target-platform plugin against the design §8b mapping table (task 9.3).

Task 9.3 — *validate against the target mapping table (design §8b) in the isolated
workspace*. A real Databricks workspace run is not available locally (R9 isolation —
no workspace / credentials), so this module performs the **mapping-table CONFORMANCE
validation** done locally against the commands + IaC each target-platform plugin
*generates*. It is the §8b analogue of :mod:`cicdgen.validate.azure_pipeline`'s §8a
``assert_mapping_table_rows_represented`` — but on the *target* axis (what the verbs
actually do) rather than the CI-vendor axis (how the pipeline is expressed).

Design §8b — target-platform axis (the rows this validator enforces)
--------------------------------------------------------------------
    | Verb     | aws-ecs                        | databricks                              |
    |----------|--------------------------------|-----------------------------------------|
    | build    | docker build                   | assemble notebooks / build wheel        |
    | package  | push image to ECR              | upload artifacts to DBFS/Volumes        |
    | deploy   | update ECS service/task def    | create/update Databricks job via CLI/API|
    | verify   | ECS health check               | run job / check run state               |
    | IaC      | ECR, ECS, IAM/OIDC (TF)        | workspace, Unity Catalog, job (TF/API)  |

The whole point of §8b is the **different-shape proof**: aws-ecs is Docker/ECR/ECS
shaped, databricks is wheel/DBFS/job shaped with NO image. So the table is encoded
here as data: for each ``target_platform`` and each verb, the ``markers`` the
emitted commands MUST contain *and* the ``anti`` markers they MUST NOT contain —
where one platform's anti-markers are literally the OTHER platform's shape. That
cross-platform contrast is what proves the two-axis model, not merely that each
verb "does something".

No live run (R9)
----------------
A genuine isolated-workspace run (``databricks jobs run-now`` against a throwaway
workspace, ``terraform apply``) needs credentials + a workspace this environment
does not have. The live run is the manual runbook captured in
``examples/client-azdo-databricks/iac/databricks/README.md`` (the honest
account-level boundary, R7.1). This validator is the local conformance gate: a pass
does not prove the job ran in a real workspace, but a failure means the generated
commands/IaC do not match the §8b shape and a live run could never be the right
shape either.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from cicdgen.ir.models import Pipeline
from cicdgen.plugins.base import (
    StageVerb,
    TargetPlatformPlugin,
    VerbContext,
)

#: The §8b verb rows that carry a platform-specific *shape* (lint/test are generic
#: project concerns, not part of the different-shape proof, so they are not table
#: rows here — see the module docstring / design §8b).
MAPPED_VERBS: tuple[StageVerb, ...] = (
    StageVerb.BUILD,
    StageVerb.PACKAGE,
    StageVerb.DEPLOY,
    StageVerb.VERIFY,
)

#: A synthetic row name for the IaC column of §8b (``supply_iac``), kept distinct
#: from the ``StageVerb`` rows so coverage can assert the IaC row too.
IAC_ROW = "IaC"


class TargetMappingValidationError(AssertionError):
    """Raised when a target plugin's output does not match its §8b column."""


@dataclass(frozen=True)
class VerbExpectation:
    """What one §8b cell (a verb on a platform) must and must not contain.

    ``markers`` are substrings the verb's emitted commands MUST ALL contain (the
    platform's shape for this verb). ``any_of`` groups are alternatives — each
    group is satisfied when AT LEAST ONE of its substrings is present (for cells
    with more than one acceptable spelling, e.g. DBFS *or* Volumes). ``anti`` are
    substrings that MUST NOT appear — these are (deliberately) the OTHER
    platform's shape, so the contrast is the different-shape proof.
    """

    markers: tuple[str, ...] = ()
    any_of: tuple[tuple[str, ...], ...] = ()
    anti: tuple[str, ...] = ()


@dataclass(frozen=True)
class TargetColumn:
    """One platform's full §8b column — a verb/IaC -> expectation mapping."""

    target_platform: str
    verbs: Mapping[str, VerbExpectation]
    iac: VerbExpectation


# ---------------------------------------------------------------------------
# The §8b table, encoded as data (the single source of truth for this check)
# ---------------------------------------------------------------------------
# aws-ecs is Docker/ECR/ECS shaped; databricks is wheel/DBFS/job shaped. Each
# column's ``anti`` markers are the OTHER column's distinctive shape, so a plugin
# that drifts toward the wrong platform fails loudly.

#: Substrings that are distinctively the *databricks* shape (aws-ecs anti-markers).
_DATABRICKS_SHAPE: tuple[str, ...] = (
    "databricks fs cp",
    "databricks jobs",
    "databricks_job",
    "python -m build",
)

#: Substrings that are distinctively the *aws-ecs* shape (databricks anti-markers).
_AWS_ECS_SHAPE: tuple[str, ...] = (
    "docker build",
    "docker push",
    "ecr",
    "aws ecs",
    "aws_ecs",
)

TARGET_MAPPING: dict[str, TargetColumn] = {
    "aws-ecs": TargetColumn(
        target_platform="aws-ecs",
        verbs={
            StageVerb.BUILD: VerbExpectation(
                markers=("docker build",),
                anti=("python -m build", "databricks fs cp", "databricks jobs"),
            ),
            StageVerb.PACKAGE: VerbExpectation(
                markers=("docker push",),
                any_of=(("ecr",),),
                anti=("databricks fs cp", "databricks workspace import"),
            ),
            StageVerb.DEPLOY: VerbExpectation(
                any_of=(
                    ("aws ecs update-service", "register-task-definition"),
                ),
                anti=("databricks jobs", "databricks_job"),
            ),
            StageVerb.VERIFY: VerbExpectation(
                markers=("aws ecs",),
                any_of=(("wait services-stable", "describe-services"),),
                anti=("databricks jobs run-now", "get-run"),
            ),
        },
        iac=VerbExpectation(
            any_of=(
                ("aws_ecr_repository",),
                ("aws_ecs_service", "aws_ecs_cluster", "aws_ecs_task_definition"),
                ("aws_iam",),
            ),
            anti=("databricks_job", "databricks_schema"),
        ),
    ),
    "databricks": TargetColumn(
        target_platform="databricks",
        verbs={
            StageVerb.BUILD: VerbExpectation(
                markers=("python -m build",),
                any_of=(("notebooks",),),
                anti=("docker build", "docker push", "ecr"),
            ),
            StageVerb.PACKAGE: VerbExpectation(
                any_of=(("databricks fs cp", "databricks workspace import"),),
                anti=("docker push", "ecr", "docker build"),
            ),
            StageVerb.DEPLOY: VerbExpectation(
                markers=("databricks jobs",),
                any_of=(("create", "reset"),),
                anti=("aws ecs", "register-task-definition", "docker"),
            ),
            StageVerb.VERIFY: VerbExpectation(
                markers=("databricks jobs run-now",),
                any_of=(("get-run",),),
                anti=("aws ecs", "wait services-stable"),
            ),
        },
        iac=VerbExpectation(
            any_of=(("databricks_job",), ("databricks_schema",)),
            anti=("aws_ecs", "aws_ecr_repository", "docker build"),
        ),
    ),
}


# ---------------------------------------------------------------------------
# Context building — exercise a plugin's verbs against a representative IR
# ---------------------------------------------------------------------------
def _stage_for(pipeline: Pipeline, verb: StageVerb):
    """Pick a representative stage for ``verb`` from the IR.

    deploy/verify prefer a deploy stage (one with an ``environment``) so the verb
    is exercised in its natural place; build/package prefer a build stage. Falls
    back to the first stage so any IR works.
    """
    stages = list(pipeline.stages)
    if verb in (StageVerb.DEPLOY, StageVerb.VERIFY):
        for s in stages:
            if getattr(s, "environment", None):
                return s
    if verb in (StageVerb.BUILD, StageVerb.PACKAGE):
        for s in stages:
            if s.id and ("build" in s.id):
                return s
    return stages[0]


def _run_verb_text(
    plugin: TargetPlatformPlugin,
    pipeline: Pipeline,
    verb: StageVerb,
    *,
    settings: Mapping[str, str] | None = None,
) -> str:
    """Run one verb and return its commands + emitted file contents as one blob."""
    ctx = VerbContext(
        pipeline=pipeline,
        stage=_stage_for(pipeline, verb),
        verb=verb,
        settings=dict(settings or {}),
    )
    result = plugin.run_verb(ctx)
    parts: list[str] = list(result.commands)
    parts.extend(f.content for f in result.files)
    return "\n".join(parts)


def _iac_text(plugin: TargetPlatformPlugin, pipeline: Pipeline) -> str:
    """Run ``supply_iac`` and return every IaC file's content + the summary."""
    iac = plugin.supply_iac(pipeline)
    parts = [f.content for f in iac.files]
    parts.append(iac.summary)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Marker / anti-marker assertions (word-boundary aware for ecr/ecs noise)
# ---------------------------------------------------------------------------
#: Short, ambiguous tokens we match on a word boundary so "secret"/"recreate" do
#: not false-positive on "ecr"/"ecs" (mirrors the plugin's own contrast test).
_WORD_BOUNDARY_TOKENS = frozenset({"ecr", "ecs"})


def _contains(haystack: str, needle: str) -> bool:
    """Case-insensitive containment; word-boundary for ambiguous short tokens."""
    low_needle = needle.lower()
    low_hay = haystack.lower()
    if low_needle in _WORD_BOUNDARY_TOKENS:
        return re.search(rf"\b{re.escape(low_needle)}\b", low_hay) is not None
    return low_needle in low_hay


def _assert_expectation(
    where: str,
    text: str,
    expectation: VerbExpectation,
) -> None:
    """Assert all ``markers`` present, each ``any_of`` group satisfied, no ``anti``."""
    for marker in expectation.markers:
        if not _contains(text, marker):
            raise TargetMappingValidationError(
                f"{where}: expected marker {marker!r} not found in the emitted "
                f"commands/files — does not match its design §8b cell."
            )
    for group in expectation.any_of:
        if not any(_contains(text, alt) for alt in group):
            raise TargetMappingValidationError(
                f"{where}: none of the alternatives {list(group)} were found — "
                f"the §8b cell requires at least one."
            )
    for anti in expectation.anti:
        if _contains(text, anti):
            raise TargetMappingValidationError(
                f"{where}: forbidden anti-marker {anti!r} IS present — this is the "
                f"OTHER platform's shape leaking in, breaking the §8b different-shape "
                f"proof."
            )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MappingReport:
    """Per-row result of a mapping-table conformance run (for coverage asserts)."""

    target_platform: str
    rows_checked: tuple[str, ...] = field(default_factory=tuple)

    def covers(self, row: str) -> bool:
        return row in self.rows_checked


def assert_target_matches_mapping(
    plugin: TargetPlatformPlugin,
    pipeline: Pipeline,
    *,
    settings: Mapping[str, str] | None = None,
) -> MappingReport:
    """Assert ``plugin``'s verbs + IaC match its design §8b column.

    Builds a :class:`VerbContext` for each §8b verb row against ``pipeline``, runs
    the verb, and asserts the expected markers are present and the anti-markers
    (the OTHER platform's shape) absent — proving aws-ecs is Docker/ECR/ECS-shaped
    and databricks is wheel/DBFS/job-shaped with NO image. Then does the same for
    the IaC row via ``supply_iac``.

    Args:
        plugin: a loaded target-platform plugin (e.g. via
            ``load_target_platform_plugin``). Its ``name`` must be a key in
            :data:`TARGET_MAPPING`.
        pipeline: a representative IR whose ``target_platform`` matches the plugin.
        settings: optional non-secret knob overrides forwarded to every verb.

    Returns:
        A :class:`MappingReport` naming every §8b row checked (the verbs +
        :data:`IAC_ROW`) so a caller can assert full table coverage.

    Raises:
        TargetMappingValidationError: on the first cell that does not conform.
    """
    name = plugin.name
    if name not in TARGET_MAPPING:
        raise TargetMappingValidationError(
            f"no design §8b column is encoded for target platform {name!r}; "
            f"known columns: {sorted(TARGET_MAPPING)}."
        )
    column = TARGET_MAPPING[name]

    checked: list[str] = []
    for verb in MAPPED_VERBS:
        expectation = column.verbs.get(verb)
        if expectation is None:  # pragma: no cover - table completeness guarded below
            raise TargetMappingValidationError(
                f"{name}: §8b column is missing a row for verb {verb!r}."
            )
        text = _run_verb_text(plugin, pipeline, verb, settings=settings)
        _assert_expectation(f"{name}/{verb}", text, expectation)
        checked.append(str(verb))

    iac_text = _iac_text(plugin, pipeline)
    _assert_expectation(f"{name}/{IAC_ROW}", iac_text, column.iac)
    checked.append(IAC_ROW)

    return MappingReport(target_platform=name, rows_checked=tuple(checked))


def expected_rows() -> tuple[str, ...]:
    """The full set of §8b rows every target column must represent (verbs + IaC)."""
    return tuple(str(v) for v in MAPPED_VERBS) + (IAC_ROW,)


def assert_full_table_coverage(report: MappingReport) -> None:
    """Assert ``report`` covered EVERY §8b row (all mapped verbs + the IaC row).

    This is the "mapping-table coverage" guarantee: it is not enough that the
    checked rows passed — every row of the platform's §8b column must have been
    exercised, so a silently-skipped verb cannot hide a shape regression.
    """
    missing = [row for row in expected_rows() if not report.covers(row)]
    if missing:
        raise TargetMappingValidationError(
            f"{report.target_platform}: mapping-table coverage incomplete — these "
            f"§8b rows were not checked: {missing}."
        )


def assert_cross_platform_contrast(
    reports: Iterable[MappingReport] | Sequence[MappingReport],
) -> None:
    """Assert the encoded table itself pits each platform's shape against the other.

    The different-shape proof only holds if one platform's anti-markers really are
    the OTHER platform's distinctive markers. This checks the encoded
    :data:`TARGET_MAPPING` is self-consistent in that sense (independent of any one
    plugin run): aws-ecs's cells forbid the databricks shape and vice versa, so a
    plugin cannot pass both columns.

    ``reports`` is accepted (and its platforms validated as the known pair) so the
    call reads naturally after running both plugins, but the contrast assertion is
    on the table data, not the reports.
    """
    names = {r.target_platform for r in reports}
    known = set(TARGET_MAPPING)
    if not names <= known:
        raise TargetMappingValidationError(
            f"unknown target platform(s) {sorted(names - known)}; "
            f"known: {sorted(known)}."
        )

    aws_anti = _collect_anti(TARGET_MAPPING["aws-ecs"])
    db_anti = _collect_anti(TARGET_MAPPING["databricks"])

    # aws-ecs must forbid the databricks shape ...
    for shape in _DATABRICKS_SHAPE:
        if not any(_token_in(shape, a) for a in aws_anti):
            raise TargetMappingValidationError(
                f"cross-platform contrast broken: aws-ecs column does not forbid the "
                f"databricks marker {shape!r} anywhere."
            )
    # ... and databricks must forbid the aws-ecs shape.
    for shape in _AWS_ECS_SHAPE:
        if not any(_token_in(shape, a) for a in db_anti):
            raise TargetMappingValidationError(
                f"cross-platform contrast broken: databricks column does not forbid the "
                f"aws-ecs marker {shape!r} anywhere."
            )


def _collect_anti(column: TargetColumn) -> set[str]:
    anti: set[str] = set()
    for expectation in column.verbs.values():
        anti.update(a.lower() for a in expectation.anti)
    anti.update(a.lower() for a in column.iac.anti)
    return anti


def _token_in(shape: str, anti: str) -> bool:
    """True when the ``anti`` marker covers the distinctive ``shape`` token.

    A column's anti-marker ``"docker"`` covers the shape token ``"docker build"``,
    and ``"aws ecs"`` covers ``"aws ecs"``; match by substring either direction so
    a coarser anti-marker still counts as forbidding the finer shape token.
    """
    shape_l = shape.lower()
    return shape_l in anti or anti in shape_l
