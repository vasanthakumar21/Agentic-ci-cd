"""IR (Intermediate Representation) package for cicdgen.

Public surface — the fail-fast IR validator:

 * models — Pydantic mirror of ir/pipeline.schema.json (+ defaults,
 cross-field rules)
 * loader — load YAML -> validate -> resolve defaults -> Pipeline
 * plugins — separate ci_vendor / target_platform existence check

Typical use:

 from cicdgen.ir import load_and_validate_ir
 pipeline = load_and_validate_ir("examples/client-azdo-databricks/pipeline.yaml")
"""

from __future__ import annotations

from cicdgen.ir.loader import (
    BASE_IR_FILENAME,
    IRValidationError,
    load_and_validate_ir,
    load_ir_dict,
    load_merged_ir_dict,
    validate_ir,
)
from cicdgen.ir.models import (
    Approvals,
    CiVendor,
    Cloud,
    CloudAuth,
    CloudProvider,
    Environment,
    GateMode,
    Notifications,
    Pipeline,
    Project,
    Promotion,
    Protection,
    ScanType,
    Stage,
    TargetPlatform,
    Thresholds,
    Triggers,
    WhenMode,
)
from cicdgen.ir.plugins import (
    AvailablePlugins,
    PluginNotFoundError,
    check_plugins_exist,
)

__all__ = [
    # loader / validate
    "load_and_validate_ir",
    "load_ir_dict",
    "load_merged_ir_dict",
    "validate_ir",
    "BASE_IR_FILENAME",
    "IRValidationError",
    # plugin existence
    "AvailablePlugins",
    "check_plugins_exist",
    "PluginNotFoundError",
    # models
    "Pipeline",
    "Project",
    "Cloud",
    "Environment",
    "Stage",
    "Approvals",
    "Thresholds",
    "Notifications",
    "Promotion",
    "Triggers",
    # enums
    "CiVendor",
    "TargetPlatform",
    "CloudProvider",
    "CloudAuth",
    "GateMode",
    "Protection",
    "WhenMode",
    "ScanType",
]
