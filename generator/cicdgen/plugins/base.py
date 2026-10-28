"""Plugin interface for BOTH provider families.

*Everything is a provider plugin*. A single IR names a ``ci_vendor`` and a
``target_platform`` independently; the core never hard-codes a vendor or a
platform. Adding a plugin never touches the core. This module defines the two
stable contracts that make that possible:

 *:class:`CIVendorPlugin` — emit the pipeline files for a stage graph
 (``.github/workflows``, ``azure-pipelines.yml``,
 ``.gitlab-ci.yml``, …).
 *:class:`TargetPlatformPlugin`— implement the stage verbs
 ``lint / test / build / package / deploy /
 verify`` and supply the backing IaC
 (Terraform / API).

The two families deliberately share nothing beyond :class:`ProviderPlugin`
(name + family), because they answer different questions: a CI-vendor plugin
answers *how is the pipeline expressed* while a target-platform plugin answers
*what do the verbs actually do*. Any ``ci_vendor`` × ``target_platform``
combination is valid, so the core composes one of each without either knowing
about the other.

These are abstract base classes so a half-finished plugin fails loudly at
import/instantiation rather than silently returning nothing.

Plugin discovery / registration
--------------------------------
Plugins are discovered *from the filesystem*, not from a hand-maintained
registry. The :class:`cicdgen.ir.plugins.AvailablePlugins` scan covers::

 plugins/ci/<name>/ -> a CIVendorPlugin named "<name>"
 plugins/target/<name>/ -> a TargetPlatformPlugin named "<name>"

A plugin therefore "registers" simply by existing as a directory under the
right family folder whose ``name`` matches the IR value (``ci_vendor`` /
``target_platform``). The loader imports the plugin package from that directory
and obtains a concrete subclass of the matching base class here. The contract
that ties the directory name to the class is :attr:`ProviderPlugin.name` — it
MUST equal the directory name so the IR value, the on-disk folder, and the
loaded class all agree.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from cicdgen.ir.models import Pipeline, Stage


# ---------------------------------------------------------------------------
# Plugin API versioning
# ---------------------------------------------------------------------------
#: The current plugin-contract revision. Version 2 adds ``capabilities`` +
#: optional promotion hooks. A plugin declares the contract version it was built
#: against via :attr:`ProviderPlugin.plugin_api_version`; the core refuses to load
#: a plugin built against an incompatible contract so a plugin written for an
#: older/newer ``base.py`` cannot silently misbehave.
PLUGIN_API_VERSION = 2

#: The contract revisions the running core accepts. A plugin loads iff its
#: declared ``plugin_api_version`` is in this set (enforced in the loader's
#: ``_validate_plugin``). Version 1 plugins are still accepted.
SUPPORTED_PLUGIN_API = (1, 2)


# ---------------------------------------------------------------------------
# Plugin families
# ---------------------------------------------------------------------------
class PluginFamily(StrEnum):
    """The two independent plugin axes.

 Mirrors the on-disk layout: ``plugins/ci/`` and ``plugins/target/``.
 """

    CI_VENDOR = "ci"
    TARGET_PLATFORM = "target"


# ---------------------------------------------------------------------------
# Plugin capability matrix
# ---------------------------------------------------------------------------
class Capability(StrEnum):
    """The closed vocabulary of capabilities a plugin may declare.

 Data, NOT behaviour: a :class:`Capability` is just a name a plugin opts into
 by listing it in :attr:`ProviderPlugin.capabilities`. The core derives the
 capabilities an IR *requires* from the IR structure alone (never from plugin
 names) and checks them against the UNION of the two chosen plugins' declared
 sets (see :mod:`cicdgen.core.capabilities`). This enum is the single source of
 truth for both sides, so a typo cannot silently create a new capability.

 A CI-vendor plugin declares the pipeline-expression capabilities it can render
 (``APPROVALS``, ``MANUAL_GATES``, ``OIDC``, ``SELF_HOSTED_RUNNERS``,
 ``MATRIX_BUILDS``, ``SECURITY_SCANS``); a target plugin declares workload
 capabilities (``CONTAINER_IMAGES``, ``ARTIFACT_PROMOTION``, ``OIDC``,
 ``ENVIRONMENTS``). ``OIDC`` can be supplied by EITHER axis — which is exactly
 why the validator checks the union of the two sets.
 """

    APPROVALS = "approvals"
    OIDC = "oidc"
    SELF_HOSTED_RUNNERS = "self_hosted_runners"
    MATRIX_BUILDS = "matrix_builds"
    ARTIFACT_PROMOTION = "artifact_promotion"
    CONTAINER_IMAGES = "container_images"
    MANUAL_GATES = "manual_gates"
    SECURITY_SCANS = "security_scans"
    ENVIRONMENTS = "environments"
    SECRETS_BY_REFERENCE = "secrets_by_reference"


# ---------------------------------------------------------------------------
# Stage verbs — the stable contract the core invokes uniformly
# ---------------------------------------------------------------------------
class StageVerb(StrEnum):
    """The fixed set of verbs every target-platform plugin implements.

 The core dispatches these uniformly, independent of platform; what each verb
 *does* is the platform's business: on aws-ecs ``build`` is ``docker build``
 and ``package`` pushes to ECR, while on databricks ``build`` assembles
 notebooks/a wheel and there is no image.
 """

    LINT = "lint"
    TEST = "test"
    BUILD = "build"
    PACKAGE = "package"
    DEPLOY = "deploy"
    VERIFY = "verify"


# ---------------------------------------------------------------------------
# Data carried in / out of the plugin boundary
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GeneratedFile:
    """One file a plugin wants written, relative to the target repo root.

 Plugins return these instead of touching the filesystem themselves so the
 core owns writing + the ``generated.manifest.json`` manifest and can perform
 clean removal on an axis swap. ``path`` is always a
 POSIX-style relative path (e.g. ``.github/workflows/ci.yml``); ``executable``
 lets a plugin mark a helper script ``+x`` without the core guessing.
 """

    path: str
    content: str
    executable: bool = False


@dataclass(frozen=True)
class StageGraph:
    """The ordered stage graph handed to a CI-vendor plugin.

 This is the vendor-neutral view of the pipeline a CI plugin renders into its
 own syntax (``needs`` / ``stages`` / dependsOn …). It carries the validated
:class:`Pipeline` IR plus the stages in declared (execution) order so a plugin
 never re-derives ordering. The core builds it; plugins only read.

 ``stage_commands`` is the bridge that makes "deploy once, full pipeline" real.
 It maps each stage id to the ordered shell commands the chosen
 *target-platform* plugin produced for that stage (its verb commands — e.g. the
 ``aws glue create-job`` / ``aws lambda update-function-code`` steps). The core
 composes it from the dispatch-time verb invocations and hands it to the CI
 plugin, which renders the commands as steps **in order** without ever
 inspecting what they do — orchestration stays in the CI plugin, business logic
 stays in the verbs. It is EMPTY when the core builds a graph without target
 verbs (e.g. a CI plugin unit test), in which case a plugin falls back to the
 IR's ``stage.runs``.
 """

    pipeline: Pipeline
    stages: tuple[Stage, ...]
    stage_commands: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_pipeline(
        cls,
        pipeline: Pipeline,
        *,
        stages: tuple[Stage, ...] | None = None,
        stage_commands: Mapping[str, tuple[str, ...]] | None = None,
    ) -> StageGraph:
        """Build the graph from a validated IR (declared order = pipeline order).

 Args:
 pipeline: the validated IR.
 stages: override the stage order (e.g. the topological order the core
 computes); defaults to the IR's declared order.
 stage_commands: per-stage-id target verb commands to render as steps;
 defaults to empty (the CI plugin then uses ``stage.runs``).
 """
        return cls(
            pipeline=pipeline,
            stages=tuple(stages) if stages is not None else tuple(pipeline.stages),
            stage_commands=dict(stage_commands) if stage_commands else {},
        )

    def commands_for(self, stage: Stage) -> tuple[str, ...]:
        """Return the target verb commands for ``stage`` (empty tuple if none).

 A CI-vendor plugin calls this to render a stage's steps: when it returns
 commands, those ARE the stage's work (the target plugin's verbs); when it
 returns empty, the plugin falls back to ``stage.runs``.
 """
        return tuple(self.stage_commands.get(stage.id, ()))


@dataclass(frozen=True)
class VerbContext:
    """Everything a target-platform verb needs to emit its steps.

 Passed to every :class:`TargetPlatformPlugin` verb so the signature is stable
 across all six verbs and across platforms. It is intentionally read-only and
 secrets-free: ``settings`` carries resolved, non-secret knobs (region, project
 name, …); secret *references* (never values) are emitted by the verb itself as
 key names (secrets by reference).
 """

    pipeline: Pipeline
    stage: Stage
    verb: StageVerb
    settings: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VerbResult:
    """What a target-platform verb produces for one stage.

 ``commands`` are the shell steps the CI pipeline will run for this verb on
 this platform (e.g. ``docker build …`` for aws-ecs ``build``). ``files`` are
 any support files the verb needs emitted alongside the pipeline (a
 ``Dockerfile``, a job JSON, …). A verb that is a no-op on a given platform
 (e.g. an image ``package`` step on databricks) returns empty tuples — the
 uniform contract holds even when the platform does nothing.
 """

    commands: tuple[str, ...] = ()
    files: tuple[GeneratedFile, ...] = ()


@dataclass(frozen=True)
class IaCArtifacts:
    """The backing infrastructure a target-platform plugin supplies.

 ``files`` are the IaC sources to write (Terraform, or an API-driver script
 where no Terraform provider fits — e.g. a Databricks job created via CLI/API).
 ``summary`` is a short, human-readable description of what the IaC provisions,
 used in the migration runbook. Like everything else crossing this boundary,
 IaC references secrets by key, never by value.
 """

    files: tuple[GeneratedFile, ...] = ()
    summary: str = ""


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------
class ProviderPlugin(ABC):
    """Common base for both plugin families.

 The ONLY thing both families share: an identity (:attr:`name`) and which
:attr:`family` they belong to.:attr:`name` MUST equal the plugin's directory
 name under ``plugins/<family>/`` so the IR value, the folder, and the loaded
 class all agree (see module docstring).
 """

    #: Plugin identity — MUST match the directory name under plugins/<family>/.
    name: str

    #: Which axis this plugin implements. Set by the family base class below.
    family: PluginFamily

    #: The plugin-contract revision this plugin was built against.
    #: Defaults to the core's current :data:`PLUGIN_API_VERSION` so existing
    #: plugins declare a supported version without a line of boilerplate; a
    #: plugin pinned to an older/newer contract overrides this. The loader refuses
    #: to load a plugin whose version is not in :data:`SUPPORTED_PLUGIN_API`.
    plugin_api_version: int = PLUGIN_API_VERSION

    #: The capabilities this plugin declares it supports. Defaults to the EMPTY
    #: set so a plugin that never declares anything
    #: degrades safely — the capability gate only fails if such a plugin is asked
    #: for a capability it never declared. Each family populates this with the
    #: capabilities it can actually render / do (see the concrete plugins).
    capabilities: frozenset[Capability] = frozenset()


# ---------------------------------------------------------------------------
# CI-vendor family
# ---------------------------------------------------------------------------
class CIVendorPlugin(ProviderPlugin):
    """Emit the pipeline files for a given stage graph.

 A CI-vendor plugin knows how to express the vendor-neutral stage graph in one
 vendor's syntax: stage order, manual gates, approvals, timeouts, secret
 references, and cloud OIDC. It does NOT know what the stages *do* — the shell
 commands come from the target-platform plugin's verbs. The core takes the
 files returned here and writes them + records them in the manifest.
 """

    family = PluginFamily.CI_VENDOR

    @abstractmethod
    def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
        """Render the pipeline file(s) for ``graph`` in this vendor's syntax.

 Args:
 graph: the vendor-neutral stage graph (validated IR + ordered stages).

 Returns:
 Every file this vendor needs written (relative paths), e.g.
 ``.github/workflows/ci.yml`` for GitHub or ``azure-pipelines.yml``
 for Azure DevOps. The core owns writing + the manifest.
 """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Target-platform family
# ---------------------------------------------------------------------------
class TargetPlatformPlugin(ProviderPlugin):
    """Implement the stage verbs and supply the IaC.

 Each verb returns the concrete steps (and any support files) for one stage on
 this platform; the core calls them uniformly regardless of platform.
:meth:`supply_iac` returns the backing infrastructure. The Databricks shape —
 no image, ``deploy`` = create/update a job via CLI/API — is the proof that the
 verb contract is platform-shaped, not Docker-shaped.

 A convenience :meth:`run_verb` dispatches by :class:`StageVerb` so the core
 can invoke verbs by value without a per-verb ``if`` ladder.
 """

    family = PluginFamily.TARGET_PLATFORM

    # --- the six stage verbs -------------------------------------------------
    @abstractmethod
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Static checks / style for this platform's artifacts."""
        raise NotImplementedError

    @abstractmethod
    def test(self, ctx: VerbContext) -> VerbResult:
        """Run the platform-appropriate test suite."""
        raise NotImplementedError

    @abstractmethod
    def build(self, ctx: VerbContext) -> VerbResult:
        """Produce the deployable artifact (image / wheel / notebooks / zip)."""
        raise NotImplementedError

    @abstractmethod
    def package(self, ctx: VerbContext) -> VerbResult:
        """Publish the artifact to its registry/store (ECR / DBFS / …).

 May be a no-op (empty result) where the platform has no package step.
 """
        raise NotImplementedError

    @abstractmethod
    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Roll the artifact out (update ECS service / create Databricks job / …)."""
        raise NotImplementedError

    @abstractmethod
    def verify(self, ctx: VerbContext) -> VerbResult:
        """Confirm the deployment is healthy (health check / job run state / …)."""
        raise NotImplementedError

    # --- IaC -----------------------------------------------------------------
    @abstractmethod
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the backing infrastructure for this platform.

 Args:
 pipeline: the validated IR (region, cloud, environments drive the IaC).

 Returns:
 The IaC files (Terraform and/or API-driver scripts) + a short summary
 for the migration runbook. Secrets are referenced by key only.
 """
        raise NotImplementedError

    # --- uniform dispatch (helper for the core) ------------------------------
    def run_verb(self, ctx: VerbContext) -> VerbResult:
        """Dispatch ``ctx.verb`` to the matching verb method.

 Lets the core invoke any verb by :class:`StageVerb` value without knowing
 the platform or hard-coding a per-verb branch.
 """
        dispatch = {
            StageVerb.LINT: self.lint,
            StageVerb.TEST: self.test,
            StageVerb.BUILD: self.build,
            StageVerb.PACKAGE: self.package,
            StageVerb.DEPLOY: self.deploy,
            StageVerb.VERIFY: self.verify,
        }
        return dispatch[ctx.verb](ctx)
