"""Write a sos-report-style manifest.json describing the collection."""

from __future__ import annotations

import datetime
import json
import os
import sys

from . import __version__
from .runner import probe

MANIFEST_NAME = "manifest.json"
SCRIPT_NAME = "must-gather-sos-script.sh"
PLUGIN_LOG_NAME = "must-gather-sos-plugins.log"


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def read_plugin_log(output_dir):
    """Parse the per-plugin 'name rc seconds' log the in-pod script wrote."""
    path = os.path.join(output_dir, PLUGIN_LOG_NAME)
    results = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) != 3:
                    continue
                name, rc, seconds = fields
                try:
                    results.append(
                        {"name": name, "rc": int(rc), "seconds": int(seconds)}
                    )
                except ValueError:
                    continue
    except OSError:
        return []
    return sorted(results, key=lambda item: item["name"])


def cluster_facts(oc):
    """Best-effort cluster identification. Never fatal."""
    facts = {}
    user = probe(oc, ["whoami"])
    if user:
        facts["user"] = user
    server = probe(oc, ["whoami", "--show-server"])
    if server:
        facts["server"] = server
    version = probe(
        oc,
        ["get", "clusterversion", "version",
         "-o", "jsonpath={.status.desired.version}"],
    )
    if version:
        facts["openshift_version"] = version
    oc_version = probe(oc, ["version", "--client", "-o", "json"])
    if oc_version:
        try:
            facts["oc_version"] = json.loads(oc_version)["clientVersion"]["gitVersion"]
        except (ValueError, KeyError, TypeError):
            pass
    return facts


def build(catalog, selection, args, started, finished, exit_code, output_dir, oc):
    return {
        "tool": {"name": "must-gather-sos", "version": __version__},
        "catalog": {
            "ref": catalog.source.get("ref"),
            "commit": catalog.source.get("commit"),
            "synced": catalog.source.get("synced"),
            "path": catalog.path,
        },
        "case_id": args.case_id,
        "invocation": {
            "argv": sys.argv,
            "cwd": os.getcwd(),
        },
        "selection": {
            "plugins": list(selection.names),
            "options": {
                plugin: dict(options)
                for plugin, options in sorted(selection.options.items())
            },
            "requested": {
                "only": args.only_plugins,
                "skip": args.skip_plugins,
                "enable": args.enable_plugins,
                "all": bool(args.all_plugins),
            },
            "skipped": sorted(set(catalog.names) - set(selection.names)),
        },
        "collection": {
            "since": args.since,
            "since_time": args.since_time,
            "reduce_logs": args.reduce_logs,
            "image": args.image,
            "timeout": args.timeout,
        },
        "cluster": cluster_facts(oc),
        "timing": {
            "started": started.isoformat(),
            "finished": finished.isoformat(),
            "duration_seconds": int((finished - started).total_seconds()),
        },
        "results": {
            "exit_code": exit_code,
            "plugins": read_plugin_log(output_dir),
        },
    }


def write(manifest, script_text, output_dir):
    """Write manifest.json and a copy of the in-pod script. Returns the paths."""
    written = []
    manifest_path = os.path.join(output_dir, MANIFEST_NAME)
    try:
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
        written.append(manifest_path)

        script_path = os.path.join(output_dir, SCRIPT_NAME)
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(script_text)
        written.append(script_path)
    except OSError as exc:
        print(f"warning: could not write manifest: {exc}", file=sys.stderr)
    return written
