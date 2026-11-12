"""Experimental: inspect a must-gather image via a cluster pod and generate a catalog.

Opt-in only (``--experimental-inspect-catalogs``). Heuristics may change.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid

from . import UserError
from . import catalog as catalog_mod
from .cluster_catalog import (
    _NOT_PLUGIN_SCRIPTS,
    _match_patterns_from_image,
    _stem_from_image,
    _utcnow,
)

INSPECT_BEGIN = "###MGS_INSPECT_BEGIN###"
INSPECT_END = "###MGS_INSPECT_END###"
SCRIPTS_MARK = "###SCRIPTS###"
LIBS_MARK = "###LIBS###"
GATHER_MARK = "###GATHER###"

# Shell run inside the must-gather image; markers keep parsing stable.
_INSPECT_SCRIPT = r"""
set +e
echo '###MGS_INSPECT_BEGIN###'
echo '###SCRIPTS###'
ls -1 /usr/bin/gather* 2>/dev/null
echo '###LIBS###'
for f in /usr/bin/common.sh /usr/bin/utils.sh /usr/bin/version /usr/bin/gather; do
  [ -e "$f" ] && echo "$f"
done
echo '###GATHER###'
if [ -f /usr/bin/gather ]; then cat /usr/bin/gather; fi
echo '###MGS_INSPECT_END###'
"""

_MANDATORY_RE = re.compile(
    r"mandatory_scripts=\(\s*((?:\"[^\"]+\"|\s+)+)\)",
    re.MULTILINE,
)
_ARRAY_RE = re.compile(
    r"^\s*(named_resources|group_resources|all_ns_resources)\+=\(([^)]*)\)\s*$"
)
_INVOKE_RE = re.compile(
    r"""^\s*
    (?P<env>(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*)
    /usr/bin/(?P<script>gather_[A-Za-z0-9_]+)
    (?P<args>(?:\s+[^\s&]+)*)
    \s*(?P<amp>&)?
    \s*$""",
    re.VERBOSE,
)

OPENSHIFT_PROFILE = {
    "id": "openshift",
    "detect": "[ -f /usr/bin/common.sh ]",
    "base_path": "/must-gather",
    "source": ["/usr/bin/common.sh"],
    "setup": ["get_log_collection_args"],
    "version": {
        "banner": "must-gather",
        "command": ". /usr/bin/version && version",
    },
    "since": True,
    "reduce_logs": True,
}

ODF_LIKE_PROFILE = {
    "id": "odf",
    "detect": "[ -f /usr/bin/utils.sh ] && [ ! -f /usr/bin/common.sh ]",
    "base_path": "/must-gather",
    "source": ["/usr/bin/utils.sh"],
    "setup": [
        "export BASE_COLLECTION_PATH=/must-gather",
        'mkdir -p "${BASE_COLLECTION_PATH}"',
        "parse_since_time",
    ],
    "version": {"banner": "must-gather", "command": None},
    "since": True,
    "reduce_logs": False,
}

GENERIC_PROFILE = {
    "id": "generic",
    "detect": "true",
    "base_path": "/must-gather",
    "source": [],
    "setup": [
        "export BASE_COLLECTION_PATH=/must-gather",
        'mkdir -p "${BASE_COLLECTION_PATH}"',
    ],
    "version": {"banner": "must-gather", "command": None},
    "since": False,
    "reduce_logs": False,
}


def parse_inspect_output(text):
    """Parse the marked inspect-pod log into scripts, libs, gather text."""
    if INSPECT_BEGIN not in text or INSPECT_END not in text:
        raise UserError("inspect pod output missing expected markers")
    body = text.split(INSPECT_BEGIN, 1)[1].split(INSPECT_END, 1)[0]
    if SCRIPTS_MARK not in body or LIBS_MARK not in body or GATHER_MARK not in body:
        raise UserError("inspect pod output missing section markers")
    after_scripts = body.split(SCRIPTS_MARK, 1)[1]
    scripts_block, rest = after_scripts.split(LIBS_MARK, 1)
    libs_block, gather_block = rest.split(GATHER_MARK, 1)

    scripts = []
    for line in scripts_block.splitlines():
        line = line.strip()
        if not line or not line.startswith("/usr/bin/gather"):
            continue
        base = os.path.basename(line)
        if base == "gather" or base in _NOT_PLUGIN_SCRIPTS:
            continue
        if base.startswith("gather_"):
            scripts.append(base[len("gather_"):])

    libs = set()
    for line in libs_block.splitlines():
        line = line.strip()
        if line:
            libs.add(os.path.basename(line))

    return {
        "scripts": scripts,
        "libs": libs,
        "gather": gather_block.strip("\n"),
    }


def parse_mandatory_scripts(gather_text):
    """Names listed in mandatory_scripts=(...) inside /usr/bin/gather."""
    names = []
    for match in _MANDATORY_RE.finditer(gather_text or ""):
        for part in re.findall(r'"([^"]+)"', match.group(1)):
            if part not in names:
                names.append(part)
    return names


def parse_openshift_layout_gather(gather_text):
    """Best-effort openshift-layout core + invocations from gather text.

    Returns (core_or_None, invocations). core is None when resource arrays
    are missing (product entrypoints that are not openshift/must-gather).
    """
    core = {"named_resources": [], "group_resources": [], "all_ns_resources": []}
    invocations = []
    seen_wait = False
    for raw in (gather_text or "").splitlines():
        line = raw.rstrip("\n")
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        m = _ARRAY_RE.match(line)
        if m:
            core[m.group(1)].extend(m.group(2).split())
            continue
        if re.match(r'^\s*wait\s+"\$\{pids\[@\]\}"', line):
            seen_wait = True
            continue
        m = _INVOKE_RE.match(line)
        if m:
            env = {}
            for assignment in m.group("env").split():
                key, _, value = assignment.partition("=")
                env[key] = value
            invocations.append({
                "name": m.group("script")[len("gather_"):],
                "env": env,
                "args": m.group("args").split(),
                "parallel": m.group("amp") == "&" and not seen_wait,
            })
    if not all(core.values()):
        return None, invocations
    return core, invocations


def classify_since_text(text):
    """Best-effort since classification from script or gather text."""
    text = text or ""
    pod_logs = "${log_collection_args}" in text
    if any(h in text for h in (
            "filter_names_since", "since_find_predicate",
            "copy_pod_dir_since", "trim_jsonl_since")):
        return "both" if pod_logs else "files"
    journal = False
    file_passthrough = False
    for line in text.splitlines():
        if "node-logs" not in line or line.lstrip().startswith("#"):
            continue
        if "--path" in line:
            if "node_log_collection_args" in line or re.search(r"\$\{?5\}?", line):
                file_passthrough = True
        else:
            journal = True
    if pod_logs and journal:
        return "both"
    if pod_logs:
        return "pod-logs"
    if journal:
        return "journal"
    if file_passthrough:
        return "passed-through"
    return "none"


def build_catalog_from_inspect(image, inspected, name=None):
    """Build a catalog_version 2 document from parse_inspect_output result."""
    stem = name or _stem_from_image(image)
    libs = inspected["libs"]
    scripts = list(inspected["scripts"])
    gather = inspected["gather"]

    if "common.sh" in libs:
        profile = dict(OPENSHIFT_PROFILE)
        if "version" not in libs:
            profile = dict(profile)
            profile["version"] = {"banner": stem, "command": None}
    elif "utils.sh" in libs:
        profile = dict(ODF_LIKE_PROFILE)
    else:
        profile = dict(GENERIC_PROFILE)

    mandatory = parse_mandatory_scripts(gather)
    core, invocations = parse_openshift_layout_gather(gather)
    invoked = {inv["name"]: inv for inv in invocations}

    # Defaults: CNV-style mandatory_scripts wins; else openshift invocations;
    # else nothing is default (safe for multi-image alongside payload).
    if mandatory:
        default_names = set(mandatory)
    elif core is not None and invoked:
        default_names = set(invoked)
    else:
        default_names = set()

    # Ensure every default name is present as a plugin even if ls missed it.
    for n in sorted(default_names):
        if n not in scripts:
            scripts.append(n)

    plugins = {}
    for order, plugin_name in enumerate(scripts):
        inv = invoked.get(plugin_name)
        plugin = {
            "script": f"/usr/bin/gather_{plugin_name}",
            "default": plugin_name in default_names,
            "phase": "parallel" if (inv is None or inv["parallel"]) else "post",
            "order": order if inv is None else list(invoked).index(plugin_name),
            "description": "",
            "since": classify_since_text(gather) if plugin_name in default_names
            else "none",
        }
        if inv and inv["args"]:
            plugin["args"] = inv["args"]
        if inv and inv["env"]:
            if inv["env"].get("MUST_GATHER_NODES_INSPECTED"):
                plugin["env_if_core"] = dict(inv["env"])
            else:
                plugin["env"] = dict(inv["env"])
        plugins[plugin_name] = plugin

    doc = {
        "catalog_version": 2,
        "name": stem,
        "comment": [
            "experimental: auto-generated by inspecting a must-gather image "
            "on the cluster. Heuristics may change; not a shipped catalog.",
        ],
        "image": {
            "default": image,
            "match": _match_patterns_from_image(image),
            "payload": False,
        },
        "profile": profile,
        "source": {
            "image": image,
            "inspected": _utcnow().isoformat(),
            "method": "cluster-pod-experimental",
        },
        "plugins": plugins,
    }

    if core is not None and "common.sh" in libs:
        # Only attach core when the gather looks like openshift's inline inspect.
        if "get_log_collection_args" in profile.get("setup", []):
            doc["core"] = {
                "description": "",
                "since": "pod-logs",
                "named_resources": core["named_resources"],
                "group_resources": core["group_resources"],
                "all_ns_resources": core["all_ns_resources"],
            }

    return doc


def _run_oc(oc, argv, timeout=120, check=False):
    try:
        proc = subprocess.run(
            [oc, *argv], text=True, capture_output=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UserError(f"oc {' '.join(argv)} failed: {exc}") from None
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        hint = detail[-1] if detail else f"exit {proc.returncode}"
        raise UserError(f"oc {' '.join(argv)} failed: {hint}")
    return proc


def inspect_image(image, namespace="default", oc="oc", timeout=180):
    """Run a one-shot inspect pod and return parse_inspect_output result."""
    pod = f"mgs-inspect-{uuid.uuid4().hex[:10]}"
    # Override command so the image entrypoint (gather) does not run.
    create = _run_oc(oc, [
        "run", pod,
        "-n", namespace,
        f"--image={image}",
        "--restart=Never",
        "--command",
        "--",
        "sh", "-c", _INSPECT_SCRIPT,
    ], timeout=60)
    if create.returncode != 0:
        detail = (create.stderr or create.stdout or "").strip().splitlines()
        hint = detail[-1] if detail else f"exit {create.returncode}"
        raise UserError(f"failed to create inspect pod: {hint}")

    try:
        deadline = time.time() + timeout
        phase = ""
        while time.time() < deadline:
            got = _run_oc(oc, [
                "get", "pod", pod, "-n", namespace,
                "-o", "jsonpath={.status.phase}",
            ], timeout=30)
            phase = (got.stdout or "").strip()
            if phase in ("Succeeded", "Failed"):
                break
            time.sleep(3)
        else:
            raise UserError(
                f"inspect pod {namespace}/{pod} timed out (last phase={phase!r})"
            )

        logs = _run_oc(oc, ["logs", "-n", namespace, pod], timeout=60)
        text = logs.stdout or ""
        if phase == "Failed" and INSPECT_BEGIN not in text:
            raise UserError(
                f"inspect pod {namespace}/{pod} failed: "
                + ((logs.stderr or text).strip().splitlines() or ["unknown"])[-1]
            )
        return parse_inspect_output(text)
    finally:
        _run_oc(oc, ["delete", "pod", pod, "-n", namespace,
                     "--ignore-not-found", "--wait=false"], timeout=30)


def inspect_and_write(image, namespace, dest_dir, oc="oc"):
    """Inspect *image* and write an experimental catalog JSON. Returns stem."""
    inspected = inspect_image(image, namespace=namespace, oc=oc)
    doc = build_catalog_from_inspect(image, inspected)
    # Validate it loads with our Catalog class before writing.
    catalog_mod.Catalog(doc, path="<generated>")
    stem = doc["name"]
    path = os.path.join(dest_dir, f"{stem}.json")
    # Avoid clobbering a shipped catalog written earlier in the same run.
    if os.path.exists(path):
        stem = f"{stem}-experimental"
        doc["name"] = stem
        path = os.path.join(dest_dir, f"{stem}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    return stem
