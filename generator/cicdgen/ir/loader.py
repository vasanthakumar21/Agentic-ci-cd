"""Load, validate, and resolve the IR (``pipeline.yaml``).

The public entry point for IR loading. It:

 1. loads the IR from YAML,
 2. validates it with Pydantic (fail-fast, precise field-level errors),
 3. resolves documented defaults (done by the models themselves),
 4. optionally confirms the named ci_vendor / target_platform plugins exist,
 5. returns a clean, parsed :class:`~cicdgen.ir.models.Pipeline` model.

Pydantic's ``ValidationError`` is reformatted into an :class:`IRValidationError`
whose message names each offending field by its dotted path (e.g.
``stages.3.timeout_minutes``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from cicdgen.ir.models import Pipeline
from cicdgen.ir.plugins import AvailablePlugins, check_plugins_exist


class IRValidationError(ValueError):
    """Raised when the IR fails validation. Message names the offending field(s)."""


def _format_validation_error(source: str, exc: ValidationError) -> str:
    lines = [f"Invalid IR ({source}) — {exc.error_count()} error(s):"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


#: The shared-base IR filename auto-discovered up the directory tree. A monorepo
#: of services keeps the fields common to every service (``version``,
#: ``ci_vendor``, ``cloud``, …) in ONE ``pipeline.base.yaml`` at the client root;
#: each service's ``pipeline.yaml`` carries only its unique keys and is deep-merged
#: ON TOP of the base. Absent base => the service file is used verbatim.
BASE_IR_FILENAME = "pipeline.base.yaml"


def load_ir_dict(path: Path | str) -> dict[str, Any]:
    """Read a YAML IR file into a dict. Raises IRValidationError on bad YAML/shape."""
    p = Path(path)
    if not p.is_file():
        raise IRValidationError(f"IR file not found: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise IRValidationError(f"IR file is not valid YAML ({p}): {exc}") from exc
    if not isinstance(data, dict):
        raise IRValidationError(
            f"IR file must contain a YAML mapping at the top level ({p}); got {type(data).__name__}."
        )
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return ``base`` deep-merged with ``override`` winning on every conflict.

 Nested mappings are merged key-by-key (so a service may override just
 ``cloud.region`` while inheriting ``cloud.provider``/``cloud.auth`` from the
 base). Every non-mapping value — crucially lists like ``stages`` — is REPLACED
 wholesale by the override, never concatenated: a service's ``stages`` are its
 own, not appended to a base's. Pure; inputs are not mutated.
 """
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        existing = out.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            out[key] = _deep_merge(existing, value)
        else:
            out[key] = value
    return out


def _discover_base_ir(service_ir_path: Path) -> Path | None:
    """Find the nearest ``pipeline.base.yaml`` at or above the service IR's dir.

 Walks from the service ``pipeline.yaml``'s directory upward to the filesystem
 root, returning the first ``pipeline.base.yaml`` found (never the service file
 itself). Returns ``None`` when no base exists — a single-file IR then loads
 as usual.
 """
    start = service_ir_path.resolve().parent
    for directory in (start, *start.parents):
        candidate = directory / BASE_IR_FILENAME
        if candidate.is_file() and candidate.resolve() != service_ir_path.resolve():
            return candidate
    return None


def load_merged_ir_dict(path: Path | str) -> dict[str, Any]:
    """Load a service IR dict, deep-merged onto an auto-discovered base (if any).

 Resolution: read the nearest ``pipeline.base.yaml`` at/above the service file's
 directory (shared fields: ``version``, ``ci_vendor``, ``cloud``, …), then
 deep-merge the service ``pipeline.yaml`` ON TOP so the service's unique keys
 (``target_platform``, ``project``, ``stages``) win. With no base present the
 service dict is returned unchanged.

 Raises:
 IRValidationError: on bad YAML/shape in EITHER file (the base or service).
 """
    service_path = Path(path)
    service_data = load_ir_dict(service_path)

    base_path = _discover_base_ir(service_path)
    if base_path is None:
        return service_data

    base_data = load_ir_dict(base_path)
    return _deep_merge(base_data, service_data)


def validate_ir(
    data: dict[str, Any],
    *,
    source: str = "<dict>",
    check_plugins: bool = True,
    available_plugins: AvailablePlugins | None = None,
    repo_root: Path | str | None = None,
) -> Pipeline:
    """Validate an IR dict and return the parsed, defaults-resolved model.

 Args:
 data: the raw IR mapping (e.g. from :func:`load_ir_dict`).
 source: label used in error messages (a path or a description).
 check_plugins: when True, also confirm the named plugins exist.
 available_plugins: injectable known-plugin set for the existence check;
 when None, plugins are discovered from the filesystem.
 repo_root: discovery root for plugin discovery (when injecting is not used).

 Raises:
 IRValidationError: on any shape/enum/required/cross-field error, or on a
 missing plugin — always with a precise, field-naming message.
 """
    try:
        model = Pipeline.model_validate(data)
    except ValidationError as exc:
        raise IRValidationError(_format_validation_error(source, exc)) from exc

    if check_plugins:
        try:
            check_plugins_exist(
                model.ci_vendor.value,
                model.target_platform.value,
                available=available_plugins,
                repo_root=repo_root,
            )
        except ValueError as exc:
            raise IRValidationError(f"Invalid IR ({source}) — {exc}") from exc

    return model


def load_and_validate_ir(
    path: Path | str,
    *,
    check_plugins: bool = True,
    available_plugins: AvailablePlugins | None = None,
    repo_root: Path | str | None = None,
) -> Pipeline:
    """Load an IR from YAML and return the validated, defaults-resolved model.

 This is the one call the generator core and CLI use to go from a
 ``pipeline.yaml`` path to a clean :class:`Pipeline` model. When a
 ``pipeline.base.yaml`` exists at or above the IR's directory, its shared fields
 are deep-merged under the service file first (the service wins on conflicts).
 """
    data = load_merged_ir_dict(path)
    return validate_ir(
        data,
        source=str(path),
        check_plugins=check_plugins,
        available_plugins=available_plugins,
        repo_root=repo_root,
    )
