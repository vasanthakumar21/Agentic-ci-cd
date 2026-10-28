"""Plugin interface package for cicdgen.

The stable contracts both provider families implement so the core dispatches
without knowing any vendor/platform specifics:

 * CIVendorPlugin — emit pipeline files for a stage graph
 * TargetPlatformPlugin — implement the six stage verbs + supply IaC

Plus the vendor-neutral data that crosses the plugin boundary (``StageGraph``,
``VerbContext``, ``VerbResult``, ``GeneratedFile``, ``IaCArtifacts``) and the
fixed ``StageVerb`` set.

The stage-verb *contract* the core invokes uniformly, independent of platform:
the declarative stage -> verbs policy (``verbs_for_stage``) and the
platform-blind invocation helpers (``invoke_stage`` / ``invoke_pipeline``) that
compose per-verb results into a ``StageInvocation`` with NO vendor/platform
branching.

Plugins register by *existing on disk* under ``plugins/ci/<name>/`` or
``plugins/target/<name>/`` (discovered by ``cicdgen.ir.AvailablePlugins``); a
plugin's ``name`` must equal its directory name.

The core dispatch layer carries NO vendor/platform knowledge: ``load_plugin``
dynamically imports the plugin package named by the IR and obtains its concrete
class (via a ``get_plugin`` factory or single-subclass discovery), and
``dispatch`` orchestrates one generation pass — build the stage graph, invoke the
target plugin's verbs, emit the CI-vendor files, and collect the target IaC —
returning a ``DispatchResult`` bundling every generated file.
"""

from __future__ import annotations

from cicdgen.plugins.base import (
    PLUGIN_API_VERSION,
    SUPPORTED_PLUGIN_API,
    Capability,
    CIVendorPlugin,
    GeneratedFile,
    IaCArtifacts,
    PluginFamily,
    ProviderPlugin,
    StageGraph,
    StageVerb,
    TargetPlatformPlugin,
    VerbContext,
    VerbResult,
)
from cicdgen.plugins.dispatch import DispatchResult, dispatch
from cicdgen.plugins.loader import (
    FACTORY_ATTR,
    PluginLoadError,
    Registry,
    load_ci_vendor_plugin,
    load_plugin,
    load_target_platform_plugin,
)
from cicdgen.plugins.stage_contract import (
    NON_VERB_STAGE_MARKERS,
    STAGE_VERB_POLICY,
    StageInvocation,
    invoke_pipeline,
    invoke_stage,
    verbs_for_stage,
)

__all__ = [
    # interfaces
    "ProviderPlugin",
    "CIVendorPlugin",
    "TargetPlatformPlugin",
    # families / verbs
    "PluginFamily",
    "StageVerb",
    # capability matrix
    "Capability",
    # plugin API versioning
    "PLUGIN_API_VERSION",
    "SUPPORTED_PLUGIN_API",
    # boundary data
    "StageGraph",
    "VerbContext",
    "VerbResult",
    "GeneratedFile",
    "IaCArtifacts",
    # stage-verb contract
    "verbs_for_stage",
    "invoke_stage",
    "invoke_pipeline",
    "StageInvocation",
    "STAGE_VERB_POLICY",
    "NON_VERB_STAGE_MARKERS",
    # dynamic loader
    "load_plugin",
    "load_ci_vendor_plugin",
    "load_target_platform_plugin",
    "PluginLoadError",
    "Registry",
    "FACTORY_ATTR",
    # core dispatch / orchestration
    "dispatch",
    "DispatchResult",
]
