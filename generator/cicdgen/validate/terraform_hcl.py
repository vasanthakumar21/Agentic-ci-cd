"""Structural validation for generated Terraform (HCL) — task 8.2 (R3.3/R4.2/R7.1).

`terraform plan` proper needs the terraform binary + a (throwaway) AWS account, which
R9 keeps out of this environment. This module is the local stand-in: a lightweight,
dependency-free *structural* HCL checker that catches the things that would make a
`terraform plan` fail to even parse, plus the project-specific invariants the aws-ecs
module must satisfy (expected resources / variables / outputs, and no committed account
specifics or secrets).

It deliberately does NOT shell out to terraform and does NOT re-implement the HCL
grammar. It tokenises just enough to prove:

    * braces ``{}`` and brackets ``[]`` are balanced;
    * a top-level ``terraform { ... }`` block with ``required_providers`` exists;
    * a ``provider "aws"`` block exists;
    * every top-level block is one of the known HCL block types and is well-formed
      (a type keyword, the right number of quoted labels, then a ``{``);
    * no line is obviously malformed (e.g. an unterminated quote).

Then higher-level assertions (``assert_expected_resources`` / ``assert_no_account_specifics``)
encode the aws-ecs contract from design §8b and the R6 "secrets by reference" rule.

The checker is conservative: a pass does not *guarantee* `terraform plan` succeeds in a
live account (only a real plan can), but a failure here means the HCL is structurally
broken and a plan could never succeed. The companion runbook documents the real
`terraform init/plan/apply` steps for an isolated account.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Top-level HCL block types the aws-ecs module uses. Each maps to how many quoted
#: labels it carries before the body ``{`` (used to validate block headers).
#:   terraform {}                         -> 0 labels
#:   provider "aws" {}                    -> 1 label
#:   locals {}                            -> 0 labels
#:   resource "type" "name" {}            -> 2 labels
#:   data "type" "name" {}                -> 2 labels
#:   variable "name" {}                   -> 1 label
#:   output "name" {}                     -> 1 label
#:   module "name" {}                     -> 1 label
_BLOCK_LABEL_COUNTS: dict[str, tuple[int, ...]] = {
    "terraform": (0,),
    "provider": (1,),
    "locals": (0,),
    "resource": (2,),
    "data": (2,),
    "variable": (1,),
    "output": (1,),
    "module": (1,),
}


class TerraformHclValidationError(AssertionError):
    """Raised when HCL fails structural validation."""


@dataclass
class HclBlock:
    """A parsed top-level HCL block header (type + quoted labels)."""

    type: str
    labels: tuple[str, ...]


@dataclass
class HclSummary:
    """What the structural scan found — consumed by the higher-level assertions."""

    blocks: list[HclBlock] = field(default_factory=list)

    def resources(self) -> set[str]:
        """Resource *types* declared (e.g. ``aws_ecr_repository``)."""
        return {b.labels[0] for b in self.blocks if b.type == "resource" and b.labels}

    def data_sources(self) -> set[str]:
        return {b.labels[0] for b in self.blocks if b.type == "data" and b.labels}

    def variables(self) -> set[str]:
        return {b.labels[0] for b in self.blocks if b.type == "variable" and b.labels}

    def outputs(self) -> set[str]:
        return {b.labels[0] for b in self.blocks if b.type == "output" and b.labels}

    def providers(self) -> set[str]:
        return {b.labels[0] for b in self.blocks if b.type == "provider" and b.labels}

    def has_terraform_block(self) -> bool:
        return any(b.type == "terraform" for b in self.blocks)


# ---------------------------------------------------------------------------
# Low-level: strip comments / strings so brace counting isn't fooled by them
# ---------------------------------------------------------------------------
def _strip_comments(hcl: str) -> str:
    """Remove ``#`` and ``//`` line comments and ``/* */`` block comments.

    Comments can legally contain unbalanced braces / brackets (the aws-ecs header
    comments do), so they must be removed before counting delimiters. String
    contents are handled separately by :func:`_scan_delimiters`.
    """
    # block comments first
    hcl = re.sub(r"/\*.*?\*/", "", hcl, flags=re.DOTALL)
    out_lines = []
    for line in hcl.splitlines():
        # a '#' or '//' starts a comment unless inside a string; we approximate by
        # cutting at the first marker that is not inside a quoted span on the line.
        out_lines.append(_strip_line_comment(line))
    return "\n".join(out_lines)


def _strip_line_comment(line: str) -> str:
    in_str = False
    escaped = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "#":
                return line[:i]
            elif ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                return line[:i]
        i += 1
    return line


def _scan_delimiters(hcl: str) -> tuple[int, int, int, int]:
    """Count braces/brackets outside of strings. Returns (open{, close}, open[, close])."""
    in_str = False
    escaped = False
    curly_open = curly_close = square_open = square_close = 0
    # `${...}` interpolation and heredocs would complicate this; the generated HCL
    # uses `${...}` inside strings only, which we skip because we ignore string bodies.
    for ch in hcl:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            curly_open += 1
        elif ch == "}":
            curly_close += 1
        elif ch == "[":
            square_open += 1
        elif ch == "]":
            square_close += 1
    return curly_open, curly_close, square_open, square_close


# ---------------------------------------------------------------------------
# Block-header parsing (top-level blocks only)
# ---------------------------------------------------------------------------
_HEADER_RE = re.compile(
    r"""^\s*
        (?P<type>[A-Za-z_][A-Za-z0-9_]*)      # block type keyword
        (?P<rest>(?:\s+"(?:[^"\\]|\\.)*")*)   # zero or more quoted labels
        \s*\{\s*\}?\s*$                        # opening brace (optionally an
                                               # immediate close for empty blocks)
    """,
    re.VERBOSE,
)
_LABEL_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _parse_top_level_blocks(hcl: str) -> list[HclBlock]:
    """Parse top-level block headers by tracking brace depth over comment-free HCL.

    A *top-level* block is one that opens at depth 0. We record its type + labels,
    then skip its body by depth tracking. This is enough to enumerate resources /
    variables / outputs / providers for the contract assertions.
    """
    blocks: list[HclBlock] = []
    depth = 0
    in_str = False
    escaped = False
    line_start = 0
    text = hcl
    pending_header: str | None = None

    # We walk line by line but track string/brace state across lines.
    for raw_line in text.splitlines():
        # Determine whether this line *starts* a top-level block (depth 0 before it).
        stripped = raw_line.strip()
        starts_block_here = (
            depth == 0
            and not in_str
            and stripped
            and (stripped.endswith("{") or stripped.endswith("{}"))
        )
        if starts_block_here:
            m = _HEADER_RE.match(raw_line)
            if m:
                btype = m.group("type")
                labels = tuple(_LABEL_RE.findall(m.group("rest") or ""))
                blocks.append(HclBlock(type=btype, labels=labels))

        # Update depth/string state char by char for this line.
        i = 0
        while i < len(raw_line):
            ch = raw_line[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
            i += 1

    # mark unused locals to satisfy linters without changing behaviour
    _ = (line_start, pending_header)
    return blocks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def validate_hcl_structure(hcl: str, *, filename: str = "<hcl>") -> HclSummary:
    """Structurally validate one HCL document. Returns a summary or raises.

    Checks (any failure raises :class:`TerraformHclValidationError`):
        * non-empty;
        * balanced ``{}`` and ``[]`` (ignoring comments and string contents);
        * no unterminated quoted string;
        * every top-level block uses a known type with the right number of labels.
    """
    if not hcl.strip():
        raise TerraformHclValidationError(f"{filename}: empty HCL document")

    cleaned = _strip_comments(hcl)

    curly_open, curly_close, square_open, square_close = _scan_delimiters(cleaned)
    if curly_open != curly_close:
        raise TerraformHclValidationError(
            f"{filename}: unbalanced braces — {curly_open} '{{' vs {curly_close} '}}'"
        )
    if square_open != square_close:
        raise TerraformHclValidationError(
            f"{filename}: unbalanced brackets — {square_open} '[' vs {square_close} ']'"
        )

    # Unterminated string detection: an odd parity of unescaped quotes per the
    # whole doc (strings don't span lines in this generated HCL except via `<<`).
    if _has_unterminated_string(cleaned):
        raise TerraformHclValidationError(f"{filename}: unterminated quoted string")

    blocks = _parse_top_level_blocks(cleaned)
    for b in blocks:
        allowed = _BLOCK_LABEL_COUNTS.get(b.type)
        if allowed is None:
            raise TerraformHclValidationError(
                f"{filename}: unknown top-level block type {b.type!r}"
            )
        if len(b.labels) not in allowed:
            raise TerraformHclValidationError(
                f"{filename}: block {b.type!r} has {len(b.labels)} label(s), "
                f"expected one of {allowed}"
            )
        for label in b.labels:
            if not label.strip():
                raise TerraformHclValidationError(
                    f"{filename}: block {b.type!r} has an empty label"
                )

    return HclSummary(blocks=blocks)


def _has_unterminated_string(cleaned: str) -> bool:
    """True if a line opens a quoted string it never closes (naive, line-scoped)."""
    for line in cleaned.splitlines():
        in_str = False
        escaped = False
        for ch in line:
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
        if in_str:
            return True
    return False


def validate_terraform_module(files: dict[str, str]) -> HclSummary:
    """Validate a whole Terraform module (``name -> content``) and merge the summary.

    Runs :func:`validate_hcl_structure` on every ``.tf`` file (``.tfvars`` files are
    HCL-valued assignments, checked for balance/strings but not block types), then
    asserts the module-level requirements: a ``terraform`` block with
    ``required_providers`` and a ``provider "aws"`` block must exist somewhere.
    """
    merged = HclSummary()
    saw_required_providers = False

    for name, content in sorted(files.items()):
        if name.endswith(".tfvars") or name.endswith(".tfvars.example"):
            # assignments only — just balance + string checks, no block grammar
            cleaned = _strip_comments(content)
            co, cc, so, sc = _scan_delimiters(cleaned)
            if co != cc or so != sc:
                raise TerraformHclValidationError(f"{name}: unbalanced delimiters")
            if _has_unterminated_string(cleaned):
                raise TerraformHclValidationError(f"{name}: unterminated quoted string")
            continue

        summary = validate_hcl_structure(content, filename=name)
        merged.blocks.extend(summary.blocks)
        if "required_providers" in content:
            saw_required_providers = True

    if not merged.has_terraform_block():
        raise TerraformHclValidationError(
            "module: no top-level `terraform { ... }` block found"
        )
    if not saw_required_providers:
        raise TerraformHclValidationError(
            "module: `terraform` block has no `required_providers`"
        )
    if "aws" not in merged.providers():
        raise TerraformHclValidationError('module: no `provider "aws"` block found')

    return merged


# Required resources / variables / outputs for the aws-ecs module (design §8b).
EXPECTED_RESOURCES: frozenset[str] = frozenset(
    {
        "aws_ecr_repository",
        "aws_ecs_cluster",
        "aws_ecs_task_definition",
        "aws_ecs_service",
        "aws_iam_role",
        "aws_iam_openid_connect_provider",
    }
)
EXPECTED_VARIABLES: frozenset[str] = frozenset(
    {"subnet_ids", "security_group_ids", "oidc_provider_url", "oidc_subject"}
)
EXPECTED_OUTPUTS: frozenset[str] = frozenset(
    {
        "ecr_repository_url",
        "ecs_cluster_name",
        "ecs_service_name",
        "ci_deploy_role_arn",
    }
)


def assert_expected_resources(summary: HclSummary) -> None:
    """Assert the aws-ecs module declares the resources/vars/outputs §8b requires."""
    missing_res = EXPECTED_RESOURCES - summary.resources()
    if missing_res:
        raise TerraformHclValidationError(
            f"module missing expected resources: {sorted(missing_res)}"
        )
    missing_vars = EXPECTED_VARIABLES - summary.variables()
    if missing_vars:
        raise TerraformHclValidationError(
            f"module missing expected variables: {sorted(missing_vars)}"
        )
    missing_out = EXPECTED_OUTPUTS - summary.outputs()
    if missing_out:
        raise TerraformHclValidationError(
            f"module missing expected outputs: {sorted(missing_out)}"
        )
    # execution role + ci deploy role => at least two aws_iam_role blocks
    iam_role_blocks = [
        b for b in summary.blocks if b.type == "resource" and b.labels[:1] == ("aws_iam_role",)
    ]
    if len(iam_role_blocks) < 2:
        raise TerraformHclValidationError(
            "module should declare at least two aws_iam_role blocks "
            "(ECS execution role + CI deploy role)"
        )
    # account id must be resolved at apply time, not hard-coded
    if "aws_caller_identity" not in summary.data_sources():
        raise TerraformHclValidationError(
            "module should read the account id via data.aws_caller_identity, not a literal"
        )


# Patterns for committed account specifics / secrets that must NOT appear (R6).
_AWS_ACCESS_KEY_RE = re.compile(r"AKIA[0-9A-Z]{16}")
_IAM_ACCOUNT_ARN_RE = re.compile(r"arn:aws:iam::\d{12}:(role|user)/")
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(secret_key|aws_secret_access_key|password)\s*=\s*\"[^\"]+\""
)
#: A literal 12-digit account id *token* (not part of a version like 5.0 or a port).
_BARE_ACCOUNT_ID_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")


def assert_no_account_specifics(files: dict[str, str]) -> None:
    """Assert no committed secrets or literal account specifics (R6).

    * No AWS access-key id shapes (``AKIA…``).
    * No literal IAM role/user ARNs with a 12-digit account id.
    * No ``secret_key``/``password`` literal assignments.
    * No bare 12-digit account-id token anywhere (the account id must come from
      ``data.aws_caller_identity``).
    """
    for name, content in files.items():
        if _AWS_ACCESS_KEY_RE.search(content):
            raise TerraformHclValidationError(f"{name}: contains an AWS access-key id")
        if _IAM_ACCOUNT_ARN_RE.search(content):
            raise TerraformHclValidationError(
                f"{name}: contains a literal IAM ARN with an account id"
            )
        if _SECRET_ASSIGN_RE.search(content):
            raise TerraformHclValidationError(
                f"{name}: contains a literal secret/password assignment"
            )
        for m in _BARE_ACCOUNT_ID_RE.finditer(content):
            raise TerraformHclValidationError(
                f"{name}: contains a bare 12-digit account-id token "
                f"{m.group(0)!r} — account specifics must be variables/data sources"
            )
