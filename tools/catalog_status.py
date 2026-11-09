#!/usr/bin/env python3
"""Test must-gather-sos against each tracked must-gather version and report.

For every openshift-layout catalog in ``tools/catalogs.yaml``, each
``check_refs`` entry is cloned, turned into a catalog, and smoke-tested:
generate the in-pod script and run it through oc's heredoc mangling against a
fake image built from that version's ``common.sh`` + stub gather plugins.

The README / CI table answers: **does our script still work with this
must-gather version?**

Statuses per cell/row:
  pass     smoke test succeeded
  fail     smoke test failed
  skip     manual catalog (not auto-tested here)
  error    clone/parse failed

Usage:
    tools/catalog_status.py
    tools/catalog_status.py --write-readme
    tools/catalog_status.py --src-map openshift=main=/path/to/checkout
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
from script_compat import common_sh_for, smoke_test_from_doc
from sync_catalog import fetch_source

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
README = os.path.join(ROOT, "README.md")

MARKER_START = "<!-- catalog-status:start -->"
MARKER_END = "<!-- catalog-status:end -->"

COPR_CHROOTS = [
    ("el9", "epel-9-x86_64"),
    ("el10", "epel-10-x86_64"),
    ("f43", "fedora-43-x86_64"),
    ("f44", "fedora-44-x86_64"),
    ("f45", "fedora-45-x86_64"),
]


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _short_repo(url):
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


def check_shipped(entry, src_map=None):
    """Smoke-test the shipped catalog for one registry entry."""
    family = entry.get("family") or entry["name"]
    ref = entry.get("sync_ref") or "main"
    if entry["sync"] == "manual":
        label = "shipped"
    else:
        label = entry.get("version") or _short_ref(ref)

    result = {
        "catalog": family,
        "name": entry["name"],
        "ref": ref,
        "label": label,
        "is_sync_ref": True,
        "status": "error",
        "plugins": 0,
        "commit": "",
        "notes": "",
    }

    try:
        shipped = _load_json(entry["out"])
    except (OSError, ValueError) as exc:
        result["notes"] = f"cannot read shipped catalog: {exc}"
        return result

    result["plugins"] = len(shipped.get("plugins") or {})
    result["commit"] = ((shipped.get("source") or {}).get("commit") or "")[:12]

    if entry["sync"] == "manual":
        ok, detail = smoke_test_from_doc(shipped, common_sh=None)
        result["status"] = "pass" if ok else "fail"
        result["notes"] = detail
        return result

    src = _src_for(entry, ref, src_map)
    cleanup = None
    try:
        src_dir, _commit, cleanup = fetch_source(ref, src, repo=entry["repo"])
        ok, detail = smoke_test_from_doc(
            shipped, common_sh=common_sh_for(src_dir),
        )
        result["status"] = "pass" if ok else "fail"
        result["notes"] = detail
    except (SystemExit, subprocess.CalledProcessError, OSError, ValueError) as exc:
        msg = getattr(exc, "stderr", None) or str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            msg = exc.stderr.strip().splitlines()[-1]
        result["notes"] = f"error: {msg}"[:120]
        result["status"] = "error"
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    return result


def collect_status(entries, src_map=None):
    """Return (family_rows, compat_results) for the README matrix."""
    compat = [check_shipped(entry, src_map=src_map) for entry in entries]

    family_rows = []
    seen = set()
    for entry, result in zip(entries, compat):
        family = entry.get("family") or entry["name"]
        if family in seen:
            continue
        seen.add(family)
        # Prefer the payload/main catalog's plugin count for the row label.
        plugins = result["plugins"]
        for other, other_result in zip(entries, compat):
            if (other.get("family") or other["name"]) != family:
                continue
            if other.get("payload") or not other.get("version"):
                plugins = other_result["plugins"]
                break
        family_rows.append({
            "catalog": family,
            "sync": entry.get("sync", "?"),
            "upstream": _short_repo(entry["repo"]),
            "plugins": plugins,
            "status": result["status"],
            "notes": result["notes"],
        })

    return family_rows, compat


# shields.io static badges for the README matrix (same look as the CI/COPR
# badges above). Kept as plain markdown so GitHub renders them without JS.
_STATUS_BADGE = {
    "pass": (
        "![pass](https://img.shields.io/badge/pass-2ea44f"
        "?style=flat-square)"
    ),
    "fail": (
        "![fail](https://img.shields.io/badge/fail-d73a4a"
        "?style=flat-square)"
    ),
    "error": (
        "![error](https://img.shields.io/badge/error-e36209"
        "?style=flat-square)"
    ),
}
def _status_badge(status):
    """Badge for a real result; plain em-dash when that catalog has no check."""
    if status is None:
        return "—"
    return _STATUS_BADGE.get(status, status)


def _md_table(headers, body):
    """Markdown table without column padding (badge URLs dominate width)."""
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(cells) + " |" for cells in body)
    return "\n".join(lines)


def render_matrix(shipped_rows, compat):
    """Catalog × version matrix of status badges for the all-plugins smoke test."""
    labels = []
    for r in compat:
        if r["label"] not in labels:
            labels.append(r["label"])
    by_key = {(r["catalog"], r["label"]): r for r in compat}
    plugins = {r["catalog"]: r["plugins"] for r in shipped_rows}

    if not labels:
        return "_No versions tested._"

    headers = ["Catalog"] + labels
    body = []
    for row in shipped_rows:
        name = row["catalog"]
        cells = [f"**{name}** ({plugins.get(name, 0)})"]
        for label in labels:
            result = by_key.get((name, label))
            cells.append(_status_badge(result["status"] if result else None))
        body.append(cells)
    return _md_table(headers, body)


def render_failures(compat):
    bad = [r for r in compat if r["status"] in ("fail", "error")]
    if not bad:
        return ""
    lines = ["", "Failures:", ""]
    for r in bad:
        note = (r.get("notes") or "").replace("|", "\\|")[:120]
        name = r.get("name") or r["catalog"]
        lines.append(f"- `{name}` ({r['label']}): {note}")
    lines.append("")
    return "\n".join(lines)


def render_report(shipped_rows, compat):
    parts = [
        "#### Script compatibility",
        "",
        "Each cell is the shipped catalog for that image version: full "
        "generate+run of every plugin (`pass` / `fail` / `error`). "
        "Runtime picks the catalog from the `--image` tag.",
        "",
        render_matrix(shipped_rows, compat),
        render_failures(compat),
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


def copr_badge(label, chroot):
    """shields.io badge for one COPR chroot's latest monitor state."""
    from urllib.parse import quote
    monitor = (
        "https://copr.fedorainfracloud.org/api_3/monitor"
        "?ownername=dsedgmen&projectname=must-gather-sos"
    )
    query = f'$.packages[0].chroots["{chroot}"].state'
    img = (
        "https://img.shields.io/badge/dynamic/json?"
        f"url={quote(monitor, safe='')}&"
        f"query={quote(query, safe='')}&"
        f"label={quote(label, safe='')}"
    )
    link = (
        f"https://download.copr.fedorainfracloud.org/results/"
        f"dsedgmen/must-gather-sos/{chroot}/"
    )
    return f"[![{label}]({img})]({link})"


def render_copr_row():
    return " · ".join(copr_badge(label, ch) for label, ch in COPR_CHROOTS)


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
    ap.add_argument("--registry", default=None)
    ap.add_argument("--write-readme", action="store_true")
    ap.add_argument("--readme", default=README)
    ap.add_argument("--src-map", action="append", default=[],
                    help="reuse a local checkout: name=/path or name=ref=/path")
    args = ap.parse_args(argv)

    try:
        entries = load_registry(args.registry)
    except (OSError, RegistryError) as exc:
        print(f"catalog-status: {exc}", file=sys.stderr)
        return 2

    src_map = _parse_src_map(args.src_map)
    shipped_rows, compat = collect_status(entries, src_map=src_map)
    report = render_report(shipped_rows, compat)
    sys.stdout.write(report)
    if not report.endswith("\n"):
        sys.stdout.write("\n")

    if args.write_readme:
        write_readme(report, readme_path=args.readme)
        print(f"updated {args.readme}", file=sys.stderr)

    bad = [r for r in compat if r["status"] in ("fail", "error")]
    if bad:
        summary = ", ".join(f"{r['catalog']}@{r['label']}={r['status']}" for r in bad)
        print(f"catalog-check failed: {summary}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
