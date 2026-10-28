"""The generated-file manifest — ``.cicd/generated.manifest.json``.

The manifest tracks *every* file the generator owns so a later run can regenerate
cleanly and remove stale files on an axis swap. It is the generator's memory of
"what I previously wrote here".

Determinism
-----------
Identical IR + repo must produce byte-identical output — including the manifest.
So the manifest serialization is deliberately stable:

 * entries are sorted by path,
 * JSON keys are sorted, with fixed separators,
 * a single trailing newline,
 * **no wall-clock timestamps** are embedded anywhere in the manifest.

The axis metadata (``ci_vendor`` / ``target_platform``) IS recorded because it is
a deterministic function of the IR, not of the clock. A per-file content hash
(sha256 over UTF-8 bytes) lets a later run detect drift / confirm ownership
without re-reading plugin output.

The manifest is data only; writing it to disk is the generator's job
(:mod:`cicdgen.core.generate`). This module just builds it, serializes it
deterministically, and parses it back.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from cicdgen.plugins.base import GeneratedFile

#: Repo-relative location of the manifest.
MANIFEST_RELPATH = ".cicd/generated.manifest.json"

#: Bumped only if the manifest shape changes incompatibly.
MANIFEST_VERSION = 1


def content_hash(content: str) -> str:
    """Deterministic sha256 (hex) of a file's UTF-8 content.

 Used both to record what the generator wrote and to detect later drift. It
 is a pure function of ``content`` — no clock, no path — so it never breaks
 the byte-identical determinism contract.
 """
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ManifestEntry:
    """One file the generator owns.

 ``path`` is a POSIX-style repo-relative path; ``hash`` is :func:`content_hash`
 of the content that was written; ``executable`` records the +x bit the plugin
 requested so a clean regenerate can reproduce it.
 """

    path: str
    hash: str
    executable: bool = False

    def to_json(self) -> dict[str, Any]:
        """Serialize to a plain dict (keys sorted at dump time)."""
        return {"path": self.path, "hash": self.hash, "executable": self.executable}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ManifestEntry:
        return cls(
            path=str(data["path"]),
            hash=str(data["hash"]),
            executable=bool(data.get("executable", False)),
        )

    @classmethod
    def from_generated(cls, f: GeneratedFile) -> ManifestEntry:
        """Build an entry from a plugin's :class:`GeneratedFile`."""
        return cls(path=f.path, hash=content_hash(f.content), executable=f.executable)


@dataclass(frozen=True)
class Manifest:
    """The full manifest — the set of files the generator owns for one repo.

 Deterministic by construction::meth:`entries` always returns entries sorted
 by path, and :meth:`to_json_str` sorts keys + emits a single trailing newline
 with no embedded timestamp.
 """

    ci_vendor: str
    target_platform: str
    entries: tuple[ManifestEntry, ...]
    version: int = MANIFEST_VERSION

    @classmethod
    def build(
        cls,
        *,
        ci_vendor: str,
        target_platform: str,
        files: Iterable[GeneratedFile],
    ) -> Manifest:
        """Build a manifest from the generated files, sorted + de-duped by path.

 If a path appears more than once (it should not after dispatch de-dup),
 the last occurrence wins, matching ``DispatchResult.all_files`` semantics.
 """
        by_path: dict[str, ManifestEntry] = {}
        for f in files:
            by_path[f.path] = ManifestEntry.from_generated(f)
        ordered = tuple(by_path[p] for p in sorted(by_path))
        return cls(
            ci_vendor=ci_vendor,
            target_platform=target_platform,
            entries=ordered,
        )

    @property
    def paths(self) -> tuple[str, ...]:
        """Every owned path, sorted."""
        return tuple(e.path for e in self.entries)

    def to_json(self) -> dict[str, Any]:
        """A plain-dict view. No timestamps — determinism contract."""
        return {
            "version": self.version,
            "ci_vendor": self.ci_vendor,
            "target_platform": self.target_platform,
            # entries already sorted by build; sort defensively anyway.
            "files": [e.to_json() for e in sorted(self.entries, key=lambda e: e.path)],
        }

    def to_json_str(self) -> str:
        """Deterministic serialization: sorted keys, fixed separators, trailing \\n."""
        return (
            json.dumps(
                self.to_json(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        )

    @classmethod
    def from_json_str(cls, text: str) -> Manifest:
        """Parse a manifest previously written by :meth:`to_json_str`.

 Tolerates an absent/empty file by raising ``ValueError`` so callers can
 treat "no prior manifest" distinctly (see :func:`load_manifest`).
 """
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("manifest is not a JSON object")
        entries = tuple(
            ManifestEntry.from_json(e) for e in data.get("files", [])
        )
        return cls(
            ci_vendor=str(data.get("ci_vendor", "")),
            target_platform=str(data.get("target_platform", "")),
            entries=tuple(sorted(entries, key=lambda e: e.path)),
            version=int(data.get("version", MANIFEST_VERSION)),
        )
