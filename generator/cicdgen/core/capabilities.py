"""Plugin capability gate.

The core lets each plugin DECLARE the capabilities it supports (as data —
:class:`~cicdgen.plugins.base.Capability`) and validates a given IR against the
**combined** capabilities of the chosen ``ci_vendor`` + ``target_platform``,
failing fast with a precise, value-free message when the IR requests something
neither chosen plugin can provide.

Two platform-blind pure functions make up the gate:

 *:func:`required_capabilities` — derives the set of capabilities an IR
 *requires* from the IR STRUCTURE ALONE (never from the plugin names):

 | IR signal (vendor-neutral) | required |
 |------------------------------------------------------------|-------------------|
 | any ``stage.approvals`` present | ``APPROVALS`` |
 | any ``environment.gate == manual`` or ``stage.when==manual``| ``MANUAL_GATES`` |
 | ``cloud.auth == oidc`` | ``OIDC`` |
 | any ``stage.scans`` non-empty | ``SECURITY_SCANS``|
 | ``environments`` non-empty | ``ENVIRONMENTS`` |
 | promotion requested | ``ARTIFACT_PROMOTION`` |

 *:func:`validate_capabilities` — computes ``required`` via the function
 above, ``provided`` as the UNION of the two plugins' declared capability
 sets (``OIDC`` may come from EITHER axis, hence the union), and raises
:class:`CapabilityError` on a non-empty ``required - provided``. The message
 names each missing capability, the IR signal that required it, and which
 axis would need to provide it — but never a secret value.

The gate runs AFTER both plugins load (so ``provided`` is known) and BEFORE any
render / file write. It holds no vendor/platform literal — every decision is
driven by the vendor-neutral IR and the data the plugins declare.
"""

from __future__ import annotations

from cicdgen.ir.models import CloudAuth, GateMode, Pipeline, WhenMode
from cicdgen.plugins.base import Capability, ProviderPlugin


class CapabilityError(RuntimeError):
    """Raised when an IR requests a capability the chosen plugin pair lacks.

 Fail-fast and value-free: the message names the missing capability, the IR
 signal that required it, and which axis would need to provide it — never a
 secret value.
 """


#: Human-readable description of the IR signal that makes each capability
#: required, and the axis that would supply it. Data only — used purely to build
#: the fail-fast message, never to branch on a vendor/platform.
_SIGNAL: dict[Capability, tuple[str, str]] = {
    Capability.APPROVALS: (
        "a stage declares 'approvals'",
        "the ci_vendor plugin",
    ),
    Capability.MANUAL_GATES: (
        "an environment has gate=manual or a stage has when=manual",
        "the ci_vendor plugin",
    ),
    Capability.OIDC: (
        "cloud.auth == oidc",
        "either axis (the ci_vendor and/or target_platform plugin)",
    ),
    Capability.SECURITY_SCANS: (
        "a stage declares 'scans'",
        "the ci_vendor plugin",
    ),
    Capability.ENVIRONMENTS: (
        "the IR declares one or more 'environments'",
        "the target_platform plugin",
    ),
    Capability.ARTIFACT_PROMOTION: (
        "the IR requests artifact promotion",
        "the target_platform plugin",
    ),
}


def _promotion_requested(pipeline: Pipeline) -> bool:
    """True when the IR requests artifact promotion.

 The promotion block is OPTIONAL. It is read defensively via ``getattr`` so the
 gate is correct whether or not the model carries the field: a ``promotion``
 with a truthy ``enabled`` flag (or, conservatively, a present-but-flagless
 block) counts as requested. When the field is absent this is always False.
 """
    promotion = getattr(pipeline, "promotion", None)
    if promotion is None:
        return False
    enabled = getattr(promotion, "enabled", None)
    # A block with no 'enabled' attribute at all still signals intent; a block
    # that carries the flag is honoured exactly.
    return True if enabled is None else bool(enabled)


def required_capabilities(pipeline: Pipeline) -> set[Capability]:
    """Derive the capabilities ``pipeline`` requires from its STRUCTURE alone.

 Platform-blind: the result depends only on the vendor-neutral IR shape —
 never on ``pipeline.ci_vendor`` / ``pipeline.target_platform``. See the
 module docstring for the signal → capability table.

 Args:
 pipeline: a validated IR.

 Returns:
 The set of :class:`Capability` the IR requires. Empty when the IR uses
 only always-available features.
 """
    required: set[Capability] = set()

    # any stage.approvals present -> APPROVALS
    if any(stage.approvals is not None for stage in pipeline.stages):
        required.add(Capability.APPROVALS)

    # any environment.gate == manual OR any stage.when == manual -> MANUAL_GATES
    manual_env = any(env.gate is GateMode.MANUAL for env in pipeline.environments)
    manual_stage = any(stage.when is WhenMode.MANUAL for stage in pipeline.stages)
    if manual_env or manual_stage:
        required.add(Capability.MANUAL_GATES)

    # cloud.auth == oidc -> OIDC
    cloud = pipeline.cloud
    if cloud is not None and cloud.auth is CloudAuth.OIDC:
        required.add(Capability.OIDC)

    # any stage.scans non-empty -> SECURITY_SCANS
    if any(stage.scans for stage in pipeline.stages):
        required.add(Capability.SECURITY_SCANS)

    # environments non-empty -> ENVIRONMENTS
    if pipeline.environments:
        required.add(Capability.ENVIRONMENTS)

    # promotion requested -> ARTIFACT_PROMOTION
    if _promotion_requested(pipeline):
        required.add(Capability.ARTIFACT_PROMOTION)

    return required


def validate_capabilities(
    pipeline: Pipeline,
    ci_plugin: ProviderPlugin,
    target_plugin: ProviderPlugin,
) -> None:
    """Fail fast if the IR needs a capability neither chosen plugin provides.

 Computes ``required`` from the IR structure (:func:`required_capabilities`)
 and ``provided`` as the UNION of the two plugins' declared capabilities
 (``OIDC`` may be supplied by either axis, so the union is correct). A
 non-empty ``required - provided`` raises :class:`CapabilityError`.

 Args:
 pipeline: a validated IR.
 ci_plugin: the loaded ``ci_vendor`` plugin.
 target_plugin: the loaded ``target_platform`` plugin.

 Raises:
 CapabilityError: naming each missing capability, the IR signal that
 required it, and which axis would need to provide it (value-free).
 """
    required = required_capabilities(pipeline)
    provided = ci_plugin.capabilities | target_plugin.capabilities
    missing = required - provided
    if not missing:
        return

    lines = [
        f"IR requires capabilities {sorted(c.value for c in missing)} that the "
        f"chosen pair ci_vendor='{ci_plugin.name}' + "
        f"target_platform='{target_plugin.name}' does not provide "
        f"(provided={sorted(c.value for c in provided)}).",
    ]
    for cap in sorted(missing, key=lambda c: c.value):
        signal, axis = _SIGNAL.get(cap, ("an IR feature", "a plugin axis"))
        lines.append(
            f"  - '{cap.value}' is required because {signal}; it must be declared "
            f"by {axis}."
        )
    raise CapabilityError("\n".join(lines))
