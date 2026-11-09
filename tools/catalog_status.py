#!/usr/bin/env python3
"""Report how each shipped catalog sits against its tracked upstream refs.

For every openshift-layout entry in ``tools/catalogs.yaml``, each ``check_refs``
value is cloned, parsed with the same builder as sync, and diffed against the
committed catalog JSON. Manual catalogs get a single ``manual`` row.

Statuses:
  current      identical to the shipped catalog (ignoring source.synced)
  compatible   shipped catalog is still safe against that image (only
               additive upstream changes: extra plugins / core resources,
               or stronger --since)
  drift        shipped catalog would mis-drive a gather on that image
               (missing plugin, weaker --since, or removed core resource)
  error        clone/parse failed
  manual       hand-written catalog; not auto-compared

Usage:
    tools/catalog_status.py                  # print table; exit 1 on drift/error
    tools/catalog_status.py --write-readme   # also refresh the README markers
    tools/catalog_status.py --src-map openshift=/path/to/checkout
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

from catalog_registry import RegistryError, load_registry
from sync_catalog import build_catalog, fetch_source

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
README = os.path.join(ROOT, "README.md")

MARKER_START = "<!-- catalog-status:start -->"
MARKER_END = "<!-- catalog-status:end -->"

# Higher index = more of the --since window actually takes effect.
SINCE_RANK = {
    "none": 0,
    "passed-through": 1,
    "pod-logs": 2,
    "journal": 2,
    "files": 3,
    "both": 4,
}


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _core_lists(doc):
    core = doc.get("core") or {}
    return {
        "named_resources": list(core.get("named_resources") or []),
        "group_resources": list(core.get("group_resources") or []),
        "all_ns_resources": list(core.get("all_ns_resources") or []),
    }


def _canonical(doc):
    """Comparable slice of a catalog: drop volatile source.synced."""
    out = {
        "name": doc.get("name"),
        "image": {
            "match": list((doc.get("image") or {}).get("match") or []),
            "payload": bool((doc.get("image") or {}).get("payload")),
        },
        "core": _core_lists(doc),
        "plugins": {},
    }
    for name, plugin in (doc.get("plugins") or {}).items():
        out["plugins"][name] = {
            "default": bool(plugin.get("default")),
            "phase": plugin.get("phase"),
            "since": plugin.get("since", "none"),
            "script": plugin.get("script"),
            "args": list(plugin.get("args") or []),
            "superset": bool(plugin.get("superset")),
            "requires_option": bool(plugin.get("requires_option")),
        }
    return out


def classify_drift(shipped, ephemeral):
    """Compare shipped catalog to one built from an upstream ref.

    Returns (status, notes) where status is current|compatible|drift.
    """
    a = _canonical(shipped)
    b = _canonical(ephemeral)
    if a == b:
        return "current", ""

    notes = []
    blocking = False

    shipped_plugins = set(a["plugins"])
    ephemeral_plugins = set(b["plugins"])
    missing = sorted(shipped_plugins - ephemeral_plugins)
    added = sorted(ephemeral_plugins - shipped_plugins)
    if missing:
        blocking = True
        notes.append(f"-{len(missing)} {_plural(len(missing), 'plugin')} ({_sample(missing)})")
    if added:
        notes.append(f"+{len(added)} {_plural(len(added), 'plugin')} ({_sample(added)})")

    since_down = []
    since_up = []
    for name in sorted(shipped_plugins & ephemeral_plugins):
        s_since = a["plugins"][name]["since"]
        e_since = b["plugins"][name]["since"]
        if s_since == e_since:
            continue
        s_rank = SINCE_RANK.get(s_since, 0)
        e_rank = SINCE_RANK.get(e_since, 0)
        # Shipped claims more capability than the image has: unsafe.
        if s_rank > e_rank:
            blocking = True
            since_down.append(name)
        else:
            since_up.append(name)
    if since_down:
        notes.append(f"since weaker: {_sample(since_down)}")
    elif since_up:
        notes.append(f"since stronger: {_sample(since_up)}")

    core_removed = 0
    core_added = 0
    for key in ("named_resources", "group_resources", "all_ns_resources"):
        s_set = set(a["core"][key])
        e_set = set(b["core"][key])
        gone = s_set - e_set
        new = e_set - s_set
        if gone:
            blocking = True
            core_removed += len(gone)
        core_added += len(new)
    if core_removed:
        notes.append(f"core -{core_removed} resources")
    elif core_added:
        notes.append(f"core +{core_added} resources")

    # Other plugin field changes (phase/default/args) are informative; they
    # do not by themselves make the shipped catalog unsafe.
    other = []
    for name in sorted(shipped_plugins & ephemeral_plugins):
        sp, ep = a["plugins"][name], b["plugins"][name]
        for field in ("default", "phase", "args", "superset", "requires_option"):
            if sp[field] != ep[field]:
                other.append(name)
                break
    if other and not since_down and not since_up:
        notes.append(f"meta changed: {_sample(other)}")

    status = "drift" if blocking else "compatible"
    return status, "; ".join(notes)


def _sample(names, limit=3):
    names = list(names)
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" +{len(names) - limit} more"


def _plural(n, word):
    return word if n == 1 else word + "s"


def _src_for(entry, ref, src_map):
    """Resolve an optional local checkout for this entry/ref."""
    if not src_map:
        return None
    # Exact "name=ref" first, then bare name (applies to every ref).
    return src_map.get(f"{entry['name']}={ref}") or src_map.get(entry["name"])


def check_ref(entry, ref, shipped, src_map=None):
    """Build one upstream ref and classify it against *shipped*. Return row."""
    row = {
        "catalog": entry["name"],
        "ref": ref,
        "shipped": ref == entry.get("sync_ref"),
        "status": "error",
        "plugins": len(shipped.get("plugins") or {}),
        "commit": (shipped.get("source") or {}).get("commit", ""),
        "notes": "",
    }
    src = _src_for(entry, ref, src_map)
    cleanup = None
    try:
        src_dir, commit, cleanup = fetch_source(ref, src, repo=entry["repo"])
        ephemeral = build_catalog(
            src_dir, ref, commit,
            name=entry["name"],
            payload=bool(entry.get("payload")),
            repo=entry["repo"],
            image_match=entry.get("image_match"),
        )
        status, notes = classify_drift(shipped, ephemeral)
        row.update({
            "status": status,
            "plugins": len(ephemeral.get("plugins") or {}),
            "commit": commit,
            "notes": notes,
        })
    except (SystemExit, subprocess.CalledProcessError, OSError, ValueError) as exc:
        msg = getattr(exc, "stderr", None) or str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            msg = exc.stderr.strip().splitlines()[-1]
        row["notes"] = f"error: {msg}"[:120]
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    return row


def manual_row(entry, shipped):
    source = shipped.get("source") or {}
    return {
        "catalog": entry["name"],
        "ref": source.get("ref") or "—",
        "shipped": True,
        "status": "manual",
        "plugins": len(shipped.get("plugins") or {}),
        "commit": source.get("commit") or "",
        "notes": "hand-written; not auto-synced",
    }


def collect_rows(entries, src_map=None):
    rows = []
    for entry in entries:
        try:
            shipped = _load_json(entry["out"])
        except (OSError, ValueError) as exc:
            rows.append({
                "catalog": entry["name"],
                "ref": entry.get("sync_ref") or "—",
                "shipped": True,
                "status": "error",
                "plugins": 0,
                "commit": "",
                "notes": f"cannot read shipped catalog: {exc}",
            })
            continue

        if entry["sync"] == "manual":
            rows.append(manual_row(entry, shipped))
            continue

        refs = list(entry.get("check_refs") or [])
        sync_ref = entry.get("sync_ref")
        if sync_ref and sync_ref not in refs:
            refs.insert(0, sync_ref)
        for ref in refs:
            rows.append(check_ref(entry, ref, shipped, src_map=src_map))
    return rows


def render_table(rows):
    headers = ["Catalog", "Upstream ref", "Shipped", "Status", "Plugins", "Notes"]
    body = []
    for row in rows:
        body.append([
            row["catalog"],
            row["ref"],
            "yes" if row["shipped"] else "",
            row["status"],
            str(row["plugins"]),
            row["notes"].replace("|", "\\|"),
        ])
    widths = [len(h) for h in headers]
    for cells in body:
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells):
        return "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    lines = [
        fmt(headers),
        "| " + " | ".join("-" * w for w in widths) + " |",
    ]
    lines.extend(fmt(cells) for cells in body)
    return "\n".join(lines) + "\n"


def write_readme(table, readme_path=README):
    """Replace the catalog-status marker region in README.md."""
    with open(readme_path, encoding="utf-8") as fh:
        text = fh.read()
    if MARKER_START not in text or MARKER_END not in text:
        raise SystemExit(
            f"{readme_path}: missing {MARKER_START} / {MARKER_END} markers"
        )
    pattern = re.compile(
        re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END),
        re.DOTALL,
    )
    replacement = f"{MARKER_START}\n\n{table.rstrip()}\n\n{MARKER_END}"
    new_text, n = pattern.subn(replacement, text, count=1)
    if n != 1:
        raise SystemExit(f"{readme_path}: failed to rewrite catalog-status region")
    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write(new_text)


def _parse_src_map(values):
    out = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"--src-map expects name[=ref]=path, got {item!r}")
        key, path = item.split("=", 1)
        # Allow name=ref=path by re-splitting once more when path still has =.
        if "=" in path and not os.path.exists(path):
            ref, path = path.split("=", 1)
            key = f"{key}={ref}"
        out[key] = path
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--registry", default=None,
                    help="path to catalogs.yaml")
    ap.add_argument("--write-readme", action="store_true",
                    help=f"refresh the {MARKER_START} region in README.md")
    ap.add_argument("--readme", default=README,
                    help="README path for --write-readme")
    ap.add_argument("--src-map", action="append", default=[],
                    help="reuse a local checkout: name=/path or name=ref=/path "
                         "(repeatable); skips cloning for that entry")
    args = ap.parse_args(argv)

    try:
        entries = load_registry(args.registry)
    except (OSError, RegistryError) as exc:
        print(f"catalog-status: {exc}", file=sys.stderr)
        return 2

    src_map = _parse_src_map(args.src_map)
    rows = collect_rows(entries, src_map=src_map)
    table = render_table(rows)
    sys.stdout.write(table)

    if args.write_readme:
        write_readme(table, readme_path=args.readme)
        print(f"updated {args.readme}", file=sys.stderr)

    bad = [r for r in rows if r["status"] in ("drift", "error")]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
