"""Load tools/catalogs.yaml — the registry of shipped catalogs and refs."""

from __future__ import annotations

import os
import re

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
DEFAULT_REGISTRY = os.path.join(HERE, "catalogs.yaml")
DATA_DIR = os.path.join(ROOT, "must_gather_sos", "data")

VALID_SYNC = {"openshift-layout", "manual"}

_RELEASE_REF = re.compile(r"^release-(\d+\.\d+)$")


class RegistryError(ValueError):
    pass


def version_label(ref):
    """Human/version key for a git ref: 'main', '4.16', …"""
    if not ref:
        return None
    m = _RELEASE_REF.match(ref)
    if m:
        return m.group(1)
    if ref in ("main", "master"):
        return "main"
    return ref


def catalog_name_for(family, ref):
    """Shipped catalog name (and JSON stem) for *family* at *ref*."""
    label = version_label(ref)
    if label in (None, "main"):
        return family
    return f"{family}-{label}"


def out_path_for(family, ref):
    return os.path.join(DATA_DIR, catalog_name_for(family, ref) + ".json")


def _expand_versioned(raw, path, index):
    """Expand a family entry with ``versions`` into one entry per ref."""
    family = raw.get("name")
    if not family:
        raise RegistryError(f"{path}: catalogs[{index}] has no name")
    refs = raw.get("versions") or []
    if not refs:
        raise RegistryError(
            f"{path}: catalog {family!r} has no versions "
            "(list git refs to ship, e.g. main, release-4.18)"
        )
    if not isinstance(refs, list):
        raise RegistryError(f"{path}: catalog {family!r} versions must be a list")

    payload_ref = raw.get("payload_ref") or "main"
    claims_payload = bool(raw.get("payload"))
    image_match = raw.get("image_match") or []
    if not isinstance(image_match, list):
        raise RegistryError(f"{path}: catalog {family!r} image_match must be a list")

    entries = []
    for ref in refs:
        if not isinstance(ref, str) or not ref:
            raise RegistryError(
                f"{path}: catalog {family!r} has a bad versions item {ref!r}"
            )
        name = catalog_name_for(family, ref)
        label = version_label(ref)
        entries.append({
            "name": name,
            "family": family,
            "sync": raw["sync"],
            "repo": raw["repo"],
            "sync_ref": ref,
            "check_refs": [ref],
            "out": out_path_for(family, ref),
            "payload": claims_payload and ref == payload_ref,
            "image_match": list(image_match),
            "version": None if label == "main" else label,
        })
    return entries


def load_registry(path=None):
    """Return the list of catalog entries from *path* (default catalogs.yaml).

    Versioned families expand to one entry per shipped ref. Each entry has at
    least ``name``, ``sync``, ``repo``, ``out``, and (for openshift-layout)
    ``family``, ``sync_ref``, ``version``.
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

        sync = raw.get("sync")
        if sync not in VALID_SYNC:
            raise RegistryError(
                f"{path}: catalogs[{i}] has unknown sync {sync!r} "
                f"(want one of {sorted(VALID_SYNC)})"
            )
        if not raw.get("repo"):
            raise RegistryError(f"{path}: catalogs[{i}] has no repo")

        if sync == "manual":
            expanded = [_manual_entry(raw, path, i)]
        elif "versions" in raw or "out" not in raw:
            expanded = _expand_versioned(raw, path, i)
        else:
            # Legacy single-out entry (tests / older fixtures).
            expanded = [_legacy_entry(raw, path, i)]

        for entry in expanded:
            if entry["name"] in seen:
                raise RegistryError(
                    f"{path}: duplicate catalog name {entry['name']!r}"
                )
            seen.add(entry["name"])
            entries.append(entry)
    return entries


def _manual_entry(raw, path, index):
    name = raw.get("name")
    if not name:
        raise RegistryError(f"{path}: catalogs[{index}] has no name")
    if not raw.get("out"):
        raise RegistryError(f"{path}: catalog {name!r} has no out")
    out = raw["out"]
    if not os.path.isabs(out):
        out = os.path.normpath(os.path.join(ROOT, out))
    return {
        "name": name,
        "family": name,
        "sync": "manual",
        "repo": raw["repo"],
        "sync_ref": raw.get("sync_ref") or "main",
        "check_refs": list(raw.get("check_refs") or []),
        "out": out,
        "payload": bool(raw.get("payload", False)),
        "image_match": list(raw.get("image_match") or []),
        "version": raw.get("version"),
    }


def _legacy_entry(raw, path, index):
    """Single-out openshift-layout entry (used by offline fixture tests)."""
    name = raw.get("name")
    if not name:
        raise RegistryError(f"{path}: catalogs[{index}] has no name")
    out = raw["out"]
    if not os.path.isabs(out):
        out = os.path.normpath(os.path.join(ROOT, out))
    ref = raw.get("sync_ref") or "main"
    return {
        "name": name,
        "family": raw.get("family") or name,
        "sync": raw["sync"],
        "repo": raw["repo"],
        "sync_ref": ref,
        "check_refs": list(raw.get("check_refs") or [ref]),
        "out": out,
        "payload": bool(raw.get("payload", False)),
        "image_match": list(raw.get("image_match") or []),
        "version": raw.get("version") if "version" in raw else (
            version_label(ref) if version_label(ref) != "main" else None
        ),
    }


def by_name(entries, name):
    """Return the entry named *name*, or raise RegistryError."""
    for entry in entries:
        if entry["name"] == name:
            return entry
    known = ", ".join(e["name"] for e in entries) or "(none)"
    raise RegistryError(f"unknown catalog {name!r}. Known: {known}")


def families(entries):
    """Unique family names in registry order."""
    seen = []
    for entry in entries:
        fam = entry.get("family") or entry["name"]
        if fam not in seen:
            seen.append(fam)
    return seen
