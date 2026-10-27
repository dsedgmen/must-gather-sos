"""Write a sos-report-style manifest.json describing the collection."""

from __future__ import annotations

import datetime
import json
import os
import sys

from . import __version__
from .runner import probe

MANIFEST_NAME = "manifest.json"
#: Bumped when the shape below changes, so a reader can tell.
MANIFEST_VERSION = 1
PROFILE_MARKER_NAME = "must-gather-sos-profile"
SCRIPT_NAME = "must-gather-sos-script.sh"
PLUGIN_LOG_NAME = "must-gather-sos-plugins.log"


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


#: What the in-pod task library concluded about a plugin. 'no-op' means it ran
#: cleanly and found nothing to collect, which on any given cluster is most of
#: a default gather; 'missing' means the image does not carry that script.
STATES = ("ok", "no-op", "failed", "missing")


def _parse_row(line):
    """One result row, in either the tab-separated or the pre-0.2 format."""
    # maxsplit: detail is free text lifted out of a plugin's own log, so it
    # can contain a tab of its own. It is the last column; let it keep them.
    fields = line.rstrip("\n").split("\t", 4)
    if len(fields) == 5:
        name, state, rc, seconds, detail = fields
    else:
        # Before the sos-style progress landed this was 'name rc seconds',
        # space separated and with no verdict. Archives written by an older
        # must-gather-sos are still worth reading.
        fields = line.split()
        if len(fields) != 3:
            return None
        name, rc, seconds = fields
        state, detail = ("failed" if rc != "0" else "ok"), ""
    try:
        row = {"name": name, "state": state, "rc": int(rc),
               "seconds": int(seconds)}
    except ValueError:
        return None
    if detail:
        row["detail"] = detail
    return row


def read_plugin_log(output_dir):
    """Parse the per-plugin results the in-pod task library wrote."""
    path = os.path.join(output_dir, PLUGIN_LOG_NAME)
    results = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                row = _parse_row(line)
                if row:
                    results.append(row)
    except OSError:
        return []
    return sorted(results, key=lambda item: item["name"])


def tally(results):
    """Count the results by state, so the summary does not have to."""
    counts = {state: 0 for state in STATES}
    for item in results:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    return counts


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


def read_profile(output_dir):
    """Which image family wrote this directory, per the marker the pod left.

    None when there is no marker, which is every single-image run -- there
    the caller already knows, so nothing is written.
    """
    try:
        with open(os.path.join(output_dir, PROFILE_MARKER_NAME),
                  encoding="utf-8", errors="replace") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def build(target, args, started, finished, exit_code, output_dir, oc):
    catalog, selection = target.catalog, target.selection
    plugins = read_plugin_log(output_dir)
    return {
        "manifest_version": MANIFEST_VERSION,
        "tool": {"name": "must-gather-sos", "version": __version__},
        "catalog": {
            "name": catalog.name,
            "profile": catalog.profile.id,
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
            # This image, not every --image in the run: a per-image manifest
            # claiming the whole list would simply be wrong. The full argv
            # is under invocation.argv.
            "image": target.image_ref,
            "jobs": target.jobs,
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
            "plugins": plugins,
            "tally": tally(plugins),
        },
    }


def build_run(entries, args, started, finished, exit_code):
    """The top-level manifest for a run that covered several images.

    Written only when there is more than one, so a single-image collection
    keeps exactly the layout it has always had. *entries* is a list of
    (catalog, output_dir, per-image manifest).
    """
    combined = {state: 0 for state in STATES}
    for _, _, doc in entries:
        for state, count in doc["results"]["tally"].items():
            combined[state] = combined.get(state, 0) + count
    return {
        "manifest_version": MANIFEST_VERSION,
        "tool": {"name": "must-gather-sos", "version": __version__},
        "case_id": args.case_id,
        "invocation": {"argv": sys.argv, "cwd": os.getcwd()},
        "timing": {
            "started": started.isoformat(),
            "finished": finished.isoformat(),
            "duration_seconds": int((finished - started).total_seconds()),
        },
        "results": {"exit_code": exit_code, "tally": combined},
        "images": [
            {
                "image": doc["collection"]["image"],
                "catalog": catalog.name,
                "profile": catalog.profile.id,
                # Relative, so the mapping survives the directory being
                # moved or unpacked somewhere else.
                "output_dir": os.path.basename(os.path.normpath(output_dir)),
                "plugins": doc["selection"]["plugins"],
                "tally": doc["results"]["tally"],
            }
            for catalog, output_dir, doc in entries
        ],
    }


def write_run(manifest, dest_dir):
    """Write the run manifest beside the per-image directories."""
    path = os.path.join(dest_dir, MANIFEST_NAME)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        print(f"warning: could not write run manifest: {exc}", file=sys.stderr)
        return None
    return path


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
