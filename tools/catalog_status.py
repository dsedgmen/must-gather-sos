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
from sync_catalog import build_catalog, fetch_source

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


def check_version(entry, ref, src_map=None):
    """Clone/build/smoke-test one upstream ref. Return a result dict."""
    result = {
        "catalog": entry["name"],
        "ref": ref,
        "label": _short_ref(ref),
        "is_sync_ref": ref == entry.get("sync_ref"),
        "status": "error",
        "plugins": 0,
        "commit": "",
        "notes": "",
    }
    src = _src_for(entry, ref, src_map)
    cleanup = None
    try:
        src_dir, commit, cleanup = fetch_source(ref, src, repo=entry["repo"])
        doc = build_catalog(
            src_dir, ref, commit,
            name=entry["name"],
            payload=bool(entry.get("payload")),
            repo=entry["repo"],
            image_match=entry.get("image_match"),
        )
        result["plugins"] = len(doc.get("plugins") or {})
        result["commit"] = commit
        ok, detail = smoke_test_from_doc(doc, common_sh=common_sh_for(src_dir))
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
    """Return (shipped_rows, compat_results)."""
    shipped_rows = []
    compat = []

    for entry in entries:
        try:
            shipped = _load_json(entry["out"])
        except (OSError, ValueError) as exc:
            shipped_rows.append({
                "catalog": entry["name"],
                "sync": entry.get("sync", "?"),
                "upstream": _short_repo(entry["repo"]),
                "ref": entry.get("sync_ref") or "—",
                "plugins": 0,
                "status": "error",
                "notes": f"cannot read shipped catalog: {exc}",
            })
            continue

        source = shipped.get("source") or {}
        if entry["sync"] == "manual":
            # Smoke-test the shipped manual catalog once (no upstream clone).
            ok, detail = smoke_test_from_doc(shipped, common_sh=None)
            # ACM needs gather_utils; without a real image tree the smoke may
            # still generate+run if stubs cover /usr/bin scripts. Function
            # plugins are skipped in the sandbox.
            status = "pass" if ok else "fail"
            shipped_rows.append({
                "catalog": entry["name"],
                "sync": "manual",
                "upstream": _short_repo(entry["repo"]),
                "ref": source.get("ref") or "—",
                "plugins": len(shipped.get("plugins") or {}),
                "status": status,
                "notes": detail,
            })
            compat.append({
                "catalog": entry["name"],
                "ref": source.get("ref") or "shipped",
                "label": "shipped",
                "is_sync_ref": True,
                "status": status,
                "plugins": len(shipped.get("plugins") or {}),
                "commit": (source.get("commit") or "")[:12],
                "notes": detail,
            })
            continue

        refs = list(entry.get("check_refs") or [])
        sync_ref = entry.get("sync_ref")
        if sync_ref and sync_ref not in refs:
            refs.insert(0, sync_ref)

        sync_result = None
        for ref in refs:
            result = check_version(entry, ref, src_map=src_map)
            compat.append(result)
            if result["is_sync_ref"]:
                sync_result = result

        shipped_rows.append({
            "catalog": entry["name"],
            "sync": "auto",
            "upstream": _short_repo(entry["repo"]),
            "ref": sync_ref or "—",
            "plugins": len(shipped.get("plugins") or {}),
            "status": (sync_result or {}).get("status", "error"),
            "notes": (sync_result or {}).get("notes", ""),
        })

    return shipped_rows, compat


def _md_table(headers, body):
    widths = [len(h) for h in headers]
    for cells in body:
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells):
        return "| " + " | ".join(
            c.ljust(widths[i]) for i, c in enumerate(cells)
        ) + " |"

    lines = [
        fmt(headers),
        "| " + " | ".join("-" * w for w in widths) + " |",
    ]
    lines.extend(fmt(cells) for cells in body)
    return "\n".join(lines)


def render_matrix(shipped_rows, compat):
    """Catalog × version matrix: pass/fail for a full all-plugins smoke test."""
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
        cells = [f"{name} ({plugins.get(name, 0)})"]
        for label in labels:
            result = by_key.get((name, label))
            cells.append(result["status"] if result else "—")
        body.append(cells)
    return _md_table(headers, body)


def render_failures(compat):
    bad = [r for r in compat if r["status"] in ("fail", "error")]
    if not bad:
        return ""
    lines = ["", "Failures:", ""]
    for r in bad:
        note = (r.get("notes") or "").replace("|", "\\|")[:120]
        lines.append(f"- `{r['catalog']}@{r['label']}`: {note}")
    lines.append("")
    return "\n".join(lines)


def render_report(shipped_rows, compat):
    parts = [
        "#### Script compatibility",
        "",
        "Full generate+run of **every** plugin against each tracked "
        "must-gather version (`pass` / `fail` / `error`).",
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
