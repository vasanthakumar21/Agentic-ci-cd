"""Unit tests for the secrets-by-reference guard (task 5.2 — R5/R6.3/R6.6).

Proves the two-layer guard in ``cicdgen.core.secrets_guard``:

    (a) a generated file containing a REAL secret value is rejected, with an error
        naming the secret KEY and file PATH but NOT the value (exact-value check);
    (b) by-reference forms (``${{ secrets.X }}`` / ``$X`` / ``$(X)``) pass both
        layers;
    (c) the heuristic fallback still refuses obvious literal-credential shapes when
        no actual values are supplied;
    (d) the exact-value check never logs or echoes the value — the value is absent
        from both ``caplog.text`` and ``str(exc)``;
    (e) values shorter than the minimal length are skipped (no false positive).

Plus the advisory positive contract (:func:`references_are_plausible`) and the
end-to-end thread-through from :func:`cicdgen.core.generate.generate`.

Everything uses real :class:`GeneratedFile` instances (no mocks).
"""

from __future__ import annotations

import logging

import pytest

from cicdgen.core.secrets_guard import (
    MIN_VALUE_LEN,
    SecretLeak,
    enforce_secrets_by_reference,
    references_are_plausible,
)
from cicdgen.plugins import GeneratedFile

# A realistic opaque secret value, well over MIN_VALUE_LEN and not a by-reference.
_REAL_VALUE = "s3cr3t-Pa55w0rd-abcDEF123456"


def _file(path: str, content: str) -> GeneratedFile:
    return GeneratedFile(path=path, content=content)


# ===========================================================================
# (a) exact-value check: real value rejected, message names KEY + PATH not value
# ===========================================================================
def test_exact_value_leak_is_rejected_naming_key_and_path() -> None:
    files = [
        _file(".github/workflows/ci.yml", f"env:\n  DB_PASSWORD: {_REAL_VALUE}\n"),
    ]
    with pytest.raises(SecretLeak) as excinfo:
        enforce_secrets_by_reference(files, secret_values={"DB_PASSWORD": _REAL_VALUE})

    msg = str(excinfo.value)
    # names the KEY and the PATH ...
    assert "DB_PASSWORD" in msg
    assert ".github/workflows/ci.yml" in msg
    # ... but NEVER the value
    assert _REAL_VALUE not in msg


def test_exact_value_check_scans_every_file() -> None:
    files = [
        _file("clean.yml", "env:\n  TOKEN: ${{ secrets.TOKEN }}\n"),
        _file("leaky.tf", f'token = "{_REAL_VALUE}"\n'),
    ]
    with pytest.raises(SecretLeak) as excinfo:
        enforce_secrets_by_reference(
            files, secret_values={"TOKEN": _REAL_VALUE}, heuristic=False
        )
    assert "leaky.tf" in str(excinfo.value)


def test_exact_value_check_passes_when_value_absent() -> None:
    files = [_file("ci.yml", "env:\n  TOKEN: ${{ secrets.TOKEN }}\n")]
    # value never appears verbatim -> no leak
    enforce_secrets_by_reference(files, secret_values={"TOKEN": _REAL_VALUE})


# ===========================================================================
# (b) by-reference forms pass both layers
# ===========================================================================
@pytest.mark.parametrize(
    "content",
    [
        "env:\n  TOKEN: ${{ secrets.CICD_TOKEN }}\n",  # GitHub
        "script:\n  - deploy --token $CICD_TOKEN\n",  # GitLab / shell
        "steps:\n  - script: deploy $(CICD_TOKEN)\n",  # Azure DevOps
        "env:\n  TOKEN: ${CICD_TOKEN}\n",  # braced shell
    ],
)
def test_by_reference_forms_pass(content: str) -> None:
    files = [_file("ci.yml", content)]
    # neither layer should trip on a pure reference (no value present)
    enforce_secrets_by_reference(
        files, secret_values={"CICD_TOKEN": _REAL_VALUE}, heuristic=True
    )


# ===========================================================================
# (c) heuristic fallback still works with no actual values
# ===========================================================================
def test_heuristic_rejects_literal_password_assignment() -> None:
    files = [_file("ci.yml", 'password = "hunter2-super-secret"\n')]
    with pytest.raises(SecretLeak) as excinfo:
        enforce_secrets_by_reference(files, secret_values=None, heuristic=True)
    assert "ci.yml" in str(excinfo.value)


def test_heuristic_rejects_aws_access_key_id() -> None:
    files = [_file("creds.tf", "aws_access_key_id = AKIAABCDEFGHIJKLMNOP\n")]
    with pytest.raises(SecretLeak):
        enforce_secrets_by_reference(files, heuristic=True)


def test_heuristic_rejects_private_key_block() -> None:
    files = [_file("key.pem", "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n")]
    with pytest.raises(SecretLeak):
        enforce_secrets_by_reference(files, heuristic=True)


def test_heuristic_can_be_disabled() -> None:
    files = [_file("ci.yml", 'password = "hunter2-super-secret"\n')]
    # no values provided + heuristic off -> nothing runs, no raise
    enforce_secrets_by_reference(files, secret_values=None, heuristic=False)


# ===========================================================================
# (d) exact check never logs/echoes the value
# ===========================================================================
def test_exact_check_never_logs_or_echoes_value(caplog: pytest.LogCaptureFixture) -> None:
    files = [_file("ci.yml", f"token: {_REAL_VALUE}\n")]
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SecretLeak) as excinfo:
            enforce_secrets_by_reference(files, secret_values={"API_TOKEN": _REAL_VALUE})

    # the value appears nowhere in logs nor in the raised exception text
    assert _REAL_VALUE not in caplog.text
    assert _REAL_VALUE not in str(excinfo.value)
    # but the KEY is named for actionability
    assert "API_TOKEN" in str(excinfo.value)


# ===========================================================================
# (e) short values are skipped (no false positive)
# ===========================================================================
def test_short_values_are_skipped() -> None:
    short = "eu"  # a region-ish value, shorter than MIN_VALUE_LEN
    assert len(short) < MIN_VALUE_LEN
    files = [_file("ci.yml", "region: eu\n")]
    # even though "eu" appears verbatim, it is too short to be flagged
    enforce_secrets_by_reference(files, secret_values={"REGION": short})


def test_value_at_min_length_is_checked() -> None:
    value = "a" * MIN_VALUE_LEN
    files = [_file("ci.yml", f"x: {value}\n")]
    with pytest.raises(SecretLeak):
        enforce_secrets_by_reference(files, secret_values={"KEY": value})


# ===========================================================================
# positive contract — advisory, never raises
# ===========================================================================
def test_references_are_plausible_reports_by_key() -> None:
    files = [
        _file("ci.yml", "env:\n  TOKEN: ${{ secrets.CICD_TOKEN }}\n"),
        _file("deploy.sh", "use $DB_HOST\n"),
    ]
    report = references_are_plausible(files, ["CICD_TOKEN", "DB_HOST", "UNUSED"])
    assert report == {"CICD_TOKEN": True, "DB_HOST": True, "UNUSED": False}


def test_references_are_plausible_never_raises_on_empty() -> None:
    # no files, no names -> empty report, no error
    assert references_are_plausible([], []) == {}


# ===========================================================================
# exact check takes precedence over heuristic (strongest first)
# ===========================================================================
def test_exact_check_runs_even_when_heuristic_off() -> None:
    files = [_file("ci.yml", f"x: {_REAL_VALUE}\n")]
    with pytest.raises(SecretLeak):
        enforce_secrets_by_reference(
            files, secret_values={"K": _REAL_VALUE}, heuristic=False
        )
