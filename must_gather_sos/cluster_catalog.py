"""Discover must-gather images on a live cluster and write a local catalog dir.

Source of truth is the cluster: CSV annotations / relatedImages and the
payload ImageStream. Known image families reuse shipped plugin metadata;
unknown families can be filled experimentally by inspecting the image
(see ``image_inspect``).
"""

from __future__ import annotations

import copy
import datetime
import json
import os
import re

from . import UserError
from . import catalog as catalog_mod
from . import runner

DEFAULT_DEST = "must-gather-sos.catalog"

MUST_GATHER_ANNOTATION = "operators.openshift.io/must-gather-image"

_NOT_PLUGIN_SCRIPTS = frozenset({
    "gather_service_logs_util",
    "gather_ppc_single_node",
})


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _oc_json(oc, argv):
    """Run oc and parse JSON stdout. Raises UserError on failure."""
    raw = runner.probe(oc, argv, timeout=120)
    if raw is None:
        raise UserError(f"oc {' '.join(argv)} failed")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UserError(f"oc {' '.join(argv)} returned invalid JSON: {exc}") from None


def payload_image(oc="oc"):
    """Resolved payload must-gather image from the openshift ImageStream, or None."""
    doc = runner.probe(oc, [
        "get", "imagestream", "must-gather", "-n", "openshift", "-o", "json",
    ], timeout=60)
    if not doc:
        return None
    try:
        data = json.loads(doc)
    except json.JSONDecodeError:
        return None
    for tag in (data.get("status") or {}).get("tags") or []:
        if tag.get("tag") != "latest":
            continue
        items = tag.get("items") or []
        if items and items[0].get("dockerImageReference"):
            return items[0]["dockerImageReference"]
    return (data.get("status") or {}).get("dockerImageRepository") or None


def discover_must_gathers(oc="oc"):
    """Return ordered unique must-gather entries from the live cluster.

    Each entry is a dict with keys: image, source, from, namespace.
    """
    entries = []
    seen = set()

    def add(image, source, from_ref, namespace):
        if not image or image in seen:
            return
        seen.add(image)
        entries.append({
            "image": image,
            "source": source,
            "from": from_ref,
            "namespace": namespace,
        })

    version = runner.cluster_version(oc)
    payload = payload_image(oc)
    if payload:
        add(payload, "payload", "openshift/must-gather", "openshift")
    else:
        # Still record a synthetic payload so the shipped openshift catalog
        # can be selected from the cluster version.
        add(
            catalog_mod.PAYLOAD_IMAGE_REF + (f":{version}" if version else ""),
            "payload",
            "clusterversion",
            "openshift",
        )

    csv_doc = _oc_json(oc, ["get", "csv", "-A", "-o", "json"])
    for item in csv_doc.get("items") or []:
        md = item.get("metadata") or {}
        ns = md.get("namespace") or ""
        name = md.get("name") or ""
        from_ref = f"{ns}/{name}" if ns else name
        ann = md.get("annotations") or {}
        annotated = ann.get(MUST_GATHER_ANNOTATION)
        if annotated:
            add(annotated, "annotation", from_ref, ns)

        for related in (item.get("spec") or {}).get("relatedImages") or []:
            rname = (related.get("name") or "")
            rimage = (related.get("image") or "")
            blob = f"{rname} {rimage}".lower()
            if "must-gather" in blob or "mustgather" in blob:
                add(rimage, "relatedImages", from_ref, ns)

    return entries, version


def match_shipped(image, cluster_version=None, source=None):
    """Return a Catalog for *image*, or None when no shipped catalog matches.

    Payload ImageStream digests often look like ``ocp-v4.0-art-dev@sha256:…``
    and do not match the openshift family's path patterns, so *source*
    ``payload`` selects via ``catalog_for_payload`` instead.
    """
    try:
        if source == "payload":
            return catalog_mod.catalog_for_payload(cluster_version)
        return catalog_mod.catalog_for_image(image, cluster_version=cluster_version)
    except UserError:
        return None


_RHEL_SUFFIX = re.compile(r"-rhel\d+$", re.IGNORECASE)


def _stem_from_image(image):
    """Catalog filename stem from an image repository path."""
    path, _tag = catalog_mod.split_image_ref(image)
    stem = path.rsplit("/", 1)[-1] if path else "must-gather"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-") or "must-gather"
    stem = _RHEL_SUFFIX.sub("", stem) or stem
    return stem.lower()


def _match_patterns_from_image(image):
    stem = _stem_from_image(image)
    patterns = [stem]
    # End-anchor the full stem so siblings do not claim each other.
    patterns.append(stem + "$")
    return patterns


def write_shipped_catalog(catalog, image, dest_dir):
    """Copy a shipped catalog JSON into *dest_dir* with image.default set.

    Returns the written filename stem.
    """
    with open(catalog.path, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc = copy.deepcopy(doc)
    doc.setdefault("image", {})
    doc["image"]["default"] = image
    if not doc.get("name"):
        doc["name"] = catalog.name
    stem = catalog.name
    path = os.path.join(dest_dir, f"{stem}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    return stem


def generate_cluster_catalog(dest_dir=None, oc="oc", experimental_inspect=False,
                             out=None):
    """Discover, match (and optionally inspect), write local catalogs.

    Returns the index dict that was written.
    """
    import sys
    out = out or sys.stdout
    dest_dir = dest_dir or DEFAULT_DEST

    runner.which_oc(oc)
    runner.check_cluster(oc)

    entries, version = discover_must_gathers(oc)
    os.makedirs(dest_dir, exist_ok=True)

    index = {
        "cluster_version": version,
        "generated_at": _utcnow().isoformat(),
        "dest_dir": os.path.abspath(dest_dir),
        "must_gathers": [],
    }

    shipped_n = unmatched_n = generated_n = error_n = 0

    for entry in entries:
        image = entry["image"]
        row = {
            "image": image,
            "source": entry["source"],
            "from": entry["from"],
            "catalog": None,
            "generated": "unmatched",
        }

        catalog = match_shipped(
            image, cluster_version=version, source=entry["source"],
        )
        if catalog is not None:
            stem = write_shipped_catalog(catalog, image, dest_dir)
            row["catalog"] = stem
            row["generated"] = "shipped"
            shipped_n += 1
            index["must_gathers"].append(row)
            print(f"  shipped  {stem}  <- {image}", file=out)
            continue

        if not experimental_inspect:
            unmatched_n += 1
            index["must_gathers"].append(row)
            print(f"  unmatched  {image}", file=out)
            continue

        # Experimental path: inspect the image on the cluster and generate.
        from . import image_inspect
        try:
            stem = image_inspect.inspect_and_write(
                image,
                namespace=entry.get("namespace") or "default",
                dest_dir=dest_dir,
                oc=oc,
            )
            row["catalog"] = stem
            row["generated"] = "inspected-experimental"
            generated_n += 1
            print(f"  experimental: generated  {stem}  <- {image}", file=out)
        except UserError as exc:
            row["generated"] = "error"
            row["error"] = str(exc)
            error_n += 1
            print(f"  experimental: error  {image}: {exc}", file=out)
        index["must_gathers"].append(row)

    index_path = os.path.join(dest_dir, "index.json")
    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)
        fh.write("\n")

    print(file=out)
    print(
        f"wrote {index_path}: {shipped_n} shipped, {unmatched_n} unmatched"
        + (f", {generated_n} experimental, {error_n} error" if experimental_inspect
           else ""),
        file=out,
    )
    if unmatched_n and not experimental_inspect:
        print(
            "hint: pass --experimental-inspect-catalogs to auto-generate "
            "plugin catalogs for unmatched images",
            file=out,
        )
    return index
