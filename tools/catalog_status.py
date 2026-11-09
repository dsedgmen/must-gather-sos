#!/usr/bin/env python3
"""Report the state of each shipped catalog and its upstream coverage.

Two views, both written into the README markers:

1. **Shipped catalogs** — one row per catalog we commit (where it came
   from, how many plugins, whether its sync_ref still matches).
2. **Upstream coverage** — matrix of catalog × tracked ref. Cells say how
   that upstream version relates to the shipped catalog:
     match     identical to shipped
     older     upstream is behind shipped (expected for past releases)
     newer     upstream has moved on; run ``make sync-catalogs``
     diverged  changes in both directions
     manual    hand-written; not compared
     error     clone/parse failed

``make catalog-check`` fails only when a sync_ref is not ``match``, or any
cell is ``error`` — not when past releases are simply ``older``.

Usage:
    tools/catalog_status.py
    tools/catalog_status.py --write-readme
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
from urllib.parse import urlparse

from catalog_registry import RegistryError, load_registry
from sync_catalog import build_catalog, fetch_source

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
README = os.path.join(ROOT, "README.md")

MARKER_START = "<!-- catalog-status:start -->"
MARKER_END = "<!-- catalog-status:end -->"

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
    """How *ephemeral* (one upstream ref) relates to the shipped catalog.

    Returns (status, notes) with status in match|older|newer|diverged.
    """
    a = _canonical(shipped)
    b = _canonical(ephemeral)
    if a == b:
        return "match", ""

    notes = []
    shipped_only = False   # things shipped has that upstream lacks
    upstream_only = False  # things upstream has that shipped lacks

    shipped_plugins = set(a["plugins"])
    ephemeral_plugins = set(b["plugins"])
    missing = sorted(shipped_plugins - ephemeral_plugins)
    added = sorted(ephemeral_plugins - shipped_plugins)
    if missing:
        shipped_only = True
        notes.append(
            f"upstream lacks {len(missing)} "
            f"{_plural(len(missing), 'plugin')} ({_sample(missing)})"
        )
    if added:
        upstream_only = True
        notes.append(
            f"upstream adds {len(added)} "
            f"{_plural(len(added), 'plugin')} ({_sample(added)})"
        )

    since_weaker = []   # upstream weaker than shipped
    since_stronger = []
    for name in sorted(shipped_plugins & ephemeral_plugins):
        s_since = a["plugins"][name]["since"]
        e_since = b["plugins"][name]["since"]
        if s_since == e_since:
            continue
        s_rank = SINCE_RANK.get(s_since, 0)
        e_rank = SINCE_RANK.get(e_since, 0)
        if s_rank > e_rank:
            shipped_only = True
            since_weaker.append(name)
        else:
            upstream_only = True
            since_stronger.append(name)
    if since_weaker:
        notes.append(f"upstream --since weaker: {_sample(since_weaker)}")
    if since_stronger:
        notes.append(f"upstream --since stronger: {_sample(since_stronger)}")

    core_removed = 0
    core_added = 0
    for key in ("named_resources", "group_resources", "all_ns_resources"):
        s_set = set(a["core"][key])
        e_set = set(b["core"][key])
        core_removed += len(s_set - e_set)
        core_added += len(e_set - s_set)
    if core_removed:
        shipped_only = True
        notes.append(f"upstream lacks {core_removed} core resources")
    if core_added:
        upstream_only = True
        notes.append(f"upstream adds {core_added} core resources")

    other = []
    for name in sorted(shipped_plugins & ephemeral_plugins):
        sp, ep = a["plugins"][name], b["plugins"][name]
        for field in ("default", "phase", "args", "superset", "requires_option"):
            if sp[field] != ep[field]:
                other.append(name)
                break
    if other and not since_weaker and not since_stronger:
        # Meta-only churn: treat as diverged so it is visible, not silent.
        shipped_only = True
        upstream_only = True
        notes.append(f"plugin meta differs: {_sample(other)}")

    if shipped_only and upstream_only:
        status = "diverged"
    elif shipped_only:
        status = "older"      # upstream is behind what we ship
    elif upstream_only:
        status = "newer"      # upstream has moved on
    else:
        status = "diverged"
    return status, "; ".join(notes)


def _sample(names, limit=3):
    names = list(names)
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" +{len(names) - limit} more"


def _plural(n, word):
    return word if n == 1 else word + "s"


def _short_repo(url):
    """https://github.com/openshift/must-gather.git → openshift/must-gather"""
    path = urlparse(url).path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path or url


def _short_ref(ref):
    if ref.startswith("release-"):
        return ref[len("release-"):]
    return ref


def _src_for(entry, ref, src_map):
    if not src_map:
        return None
    return src_map.get(f"{entry['name']}={ref}") or src_map.get(entry["name"])


def check_ref(entry, ref, shipped, src_map=None):
    """Compare one upstream ref to *shipped*. Return a result dict."""
    result = {
        "catalog": entry["name"],
        "ref": ref,
        "is_sync_ref": ref == entry.get("sync_ref"),
        "status": "error",
        "plugins": len((shipped.get("plugins") or {})),
        "commit": "",
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
        result.update({
            "status": status,
            "plugins": len(ephemeral.get("plugins") or {}),
            "commit": commit,
            "notes": notes,
        })
    except (SystemExit, subprocess.CalledProcessError, OSError, ValueError) as exc:
        msg = getattr(exc, "stderr", None) or str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            msg = exc.stderr.strip().splitlines()[-1]
        result["notes"] = f"error: {msg}"[:120]
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    return result


def collect_status(entries, src_map=None):
    """Return (shipped_rows, coverage_results).

    shipped_rows: one dict per catalog for the state table.
    coverage_results: list of check_ref results (auto catalogs only).
    """
    shipped_rows = []
    coverage = []

    for entry in entries:
        try:
            shipped = _load_json(entry["out"])
        except (OSError, ValueError) as exc:
            shipped_rows.append({
                "catalog": entry["name"],
                "sync": entry["sync"],
                "upstream": _short_repo(entry["repo"]),
                "ref": entry.get("sync_ref") or "—",
                "commit": "",
                "synced": "",
                "plugins": 0,
                "status": "error",
                "notes": f"cannot read shipped catalog: {exc}",
            })
            continue

        source = shipped.get("source") or {}
        if entry["sync"] == "manual":
            shipped_rows.append({
                "catalog": entry["name"],
                "sync": "manual",
                "upstream": _short_repo(entry["repo"]),
                "ref": source.get("ref") or "—",
                "commit": (source.get("commit") or "")[:12],
                "synced": (source.get("synced") or "")[:10],
                "plugins": len(shipped.get("plugins") or {}),
                "status": "manual",
                "notes": "",
            })
            continue

        refs = list(entry.get("check_refs") or [])
        sync_ref = entry.get("sync_ref")
        if sync_ref and sync_ref not in refs:
            refs.insert(0, sync_ref)

        sync_result = None
        for ref in refs:
            result = check_ref(entry, ref, shipped, src_map=src_map)
            coverage.append(result)
            if result["is_sync_ref"]:
                sync_result = result

        if sync_result is None:
            status, commit, notes = "error", "", "no sync_ref checked"
            plugins = len(shipped.get("plugins") or {})
        else:
            status = sync_result["status"]
            commit = (sync_result["commit"] or source.get("commit") or "")[:12]
            notes = sync_result["notes"]
            plugins = len(shipped.get("plugins") or {})

        shipped_rows.append({
            "catalog": entry["name"],
            "sync": "auto",
            "upstream": _short_repo(entry["repo"]),
            "ref": sync_ref or "—",
            "commit": commit,
            "synced": (source.get("synced") or "")[:10],
            "plugins": plugins,
            "status": status,
            "notes": notes,
        })

    return shipped_rows, coverage


def _md_table(headers, body):
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
    return "\n".join(lines)


def render_shipped_table(shipped_rows):
    headers = ["Catalog", "Sync", "Upstream", "Ref", "Commit", "Synced",
               "Plugins", "Status"]
    body = []
    for row in shipped_rows:
        body.append([
            row["catalog"],
            row["sync"],
            row["upstream"],
            row["ref"],
            row["commit"] or "—",
            row["synced"] or "—",
            str(row["plugins"]),
            row["status"],
        ])
    return _md_table(headers, body)


def render_coverage_matrix(shipped_rows, coverage):
    """Compact catalog × ref matrix. Manual catalogs are marked once."""
    refs = []
    for r in coverage:
        if r["ref"] not in refs:
            refs.append(r["ref"])

    catalogs = [r["catalog"] for r in shipped_rows]
    by_key = {(r["catalog"], r["ref"]): r for r in coverage}
    manual = {r["catalog"] for r in shipped_rows if r["sync"] == "manual"}

    if not refs and not manual:
        return "_No upstream refs tracked._"

    if refs:
        headers = ["Catalog"] + [_short_ref(ref) for ref in refs]
    else:
        headers = ["Catalog", "Coverage"]

    body = []
    for name in catalogs:
        if name in manual:
            if refs:
                body.append([name] + ["manual" if i == 0 else "—"
                                      for i in range(len(refs))])
            else:
                body.append([name, "manual"])
            continue
        cells = [name]
        for ref in refs:
            result = by_key.get((name, ref))
            cells.append(result["status"] if result else "—")
        body.append(cells)

    return _md_table(headers, body)


def render_report(shipped_rows, coverage):
    parts = [
        "#### Shipped catalogs",
        "",
        render_shipped_table(shipped_rows),
        "",
        "#### Upstream coverage",
        "",
        "How each tracked upstream ref relates to the shipped catalog: "
        "`match` identical, `older` upstream is behind (typical for past "
        "releases), `newer` upstream has moved on (re-sync), `diverged` "
        "changes both ways, `error` check failed.",
        "",
        render_coverage_matrix(shipped_rows, coverage),
        "",
    ]
    return "\n".join(parts)


def write_readme(report, readme_path=README):
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
    replacement = f"{MARKER_START}\n\n{report.rstrip()}\n\n{MARKER_END}"
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
    shipped_rows, coverage = collect_status(entries, src_map=src_map)
    report = render_report(shipped_rows, coverage)
    sys.stdout.write(report)
    if not report.endswith("\n"):
        sys.stdout.write("\n")

    if args.write_readme:
        write_readme(report, readme_path=args.readme)
        print(f"updated {args.readme}", file=sys.stderr)

    # Fail when sync_ref is not a clean match, or any check errored.
    bad = []
    for row in shipped_rows:
        if row["status"] in ("newer", "older", "diverged", "error"):
            bad.append(f"{row['catalog']} sync_ref={row['status']}")
    for result in coverage:
        if result["status"] == "error":
            bad.append(f"{result['catalog']}@{result['ref']}=error")
    if bad:
        print("catalog-check failed: " + "; ".join(bad), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
