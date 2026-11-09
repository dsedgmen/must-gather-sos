"""Load tools/catalogs.yaml — the registry of shipped catalogs and refs."""

from __future__ import annotations

import os

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
DEFAULT_REGISTRY = os.path.join(HERE, "catalogs.yaml")

VALID_SYNC = {"openshift-layout", "manual"}


class RegistryError(ValueError):
    pass


def load_registry(path=None):
    """Return the list of catalog entries from *path* (default catalogs.yaml).

    Each entry is a dict with at least ``name``, ``sync``, ``repo``, ``out``.
    ``out`` is made absolute relative to the repo root. Unknown sync methods
    and missing required fields raise RegistryError.
    """
    path = path or DEFAULT_REGISTRY
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict) or not isinstance(doc.get("catalogs"), list):
        raise RegistryError(f"{path}: expected a mapping with a 'catalogs' list")

    entries = []
    seen = set()
    for i, raw in enumerate(doc["catalogs"]):
        if not isinstance(raw, dict):
            raise RegistryError(f"{path}: catalogs[{i}] is not a mapping")
        entry = dict(raw)
        name = entry.get("name")
        if not name:
            raise RegistryError(f"{path}: catalogs[{i}] has no name")
        if name in seen:
            raise RegistryError(f"{path}: duplicate catalog name {name!r}")
        seen.add(name)

        sync = entry.get("sync")
        if sync not in VALID_SYNC:
            raise RegistryError(
                f"{path}: catalog {name!r} has unknown sync {sync!r} "
                f"(want one of {sorted(VALID_SYNC)})"
            )
        if not entry.get("repo"):
            raise RegistryError(f"{path}: catalog {name!r} has no repo")
        if not entry.get("out"):
            raise RegistryError(f"{path}: catalog {name!r} has no out")

        out = entry["out"]
        if not os.path.isabs(out):
            entry["out"] = os.path.normpath(os.path.join(ROOT, out))

        entry.setdefault("sync_ref", "main")
        entry.setdefault("check_refs", [])
        entry.setdefault("payload", False)
        entry.setdefault("image_match", [])
        if not isinstance(entry["check_refs"], list):
            raise RegistryError(f"{path}: catalog {name!r} check_refs must be a list")
        if not isinstance(entry["image_match"], list):
            raise RegistryError(f"{path}: catalog {name!r} image_match must be a list")

        entries.append(entry)
    return entries


def by_name(entries, name):
    """Return the entry named *name*, or raise RegistryError."""
    for entry in entries:
        if entry["name"] == name:
            return entry
    known = ", ".join(e["name"] for e in entries) or "(none)"
    raise RegistryError(f"unknown catalog {name!r}. Known: {known}")
