#!/usr/bin/env python3
"""Regenerate must_gather_sos/data/openshift.json from openshift/must-gather.

Most of the catalog is *derived* from upstream's ``collection-scripts/gather``:
which plugins exist, which of them the default run invokes, in what phase, with
what arguments, and the three base ``oc adm inspect`` resource lists.  Anything
that cannot be read off the source -- human descriptions, plugin options, the
"this one re-runs everything" flag -- lives in the OVERLAY table below and is
merged in at sync time.

Usage:
    tools/sync_catalog.py                      # clone main into a temp dir
    tools/sync_catalog.py --ref release-4.19
    tools/sync_catalog.py --src /path/to/must-gather   # use an existing clone
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = "https://github.com/openshift/must-gather.git"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
DEFAULT_OUT = os.path.join(ROOT, "must_gather_sos", "data", "openshift.json")

#: Upstream files copied verbatim for the integration test to run against, so
#: our assumptions about get_log_collection_args are checked against the real
#: thing rather than a paraphrase of it.
VENDORED = {
    "collection-scripts/common.sh": "tests/data/upstream/common.sh",
}

# Scripts in collection-scripts/ that are not plugins.
NOT_PLUGINS = {
    # Sourced as a library by gather_service_logs; has no execute bit.
    "gather_service_logs_util",
    # "This script is not supposed to be executed manually. It is used from
    #  the gather_ppc collection script."
    "gather_ppc_single_node",
}

# Hand-maintained metadata, keyed by plugin name (script name minus "gather_").
# "default", "phase", "args" and "env" are derived from upstream and must not
# be set here.
OVERLAY = {
    "apirequestcounts": {
        "description": "APIRequestCount resources plus top-20 resource and user "
        "summaries for the last 24h (needs jq in the image).",
    },
    "aro": {
        "description": "Azure Red Hat OpenShift cluster resources. No-op on "
        "non-ARO clusters.",
    },
    "audit_logs": {
        "description": "kube-apiserver, openshift-apiserver, oauth-apiserver, "
        "oauth-server and etcd audit logs from every master, plus "
        "prometheus-adapter/metrics-server audit logs. Can be very large.",
        "options": {
            "force": {
                "kind": "flag",
                "type": "bool",
                "flag": "--force",
                "help": "Collect even when spec.audit.profile is None (the "
                "script otherwise refuses and exits 1).",
            },
        },
    },
    "core_dumps": {
        "description": "systemd coredumps from /var/lib/systemd/coredump on "
        "every node, via oc debug. Can be very large.",
        "options": {
            "out": {
                "kind": "env",
                "type": "str",
                "env": "OUT",
                "help": "Override the output directory inside the pod.",
            },
        },
    },
    "etcd": {
        "description": "etcd member list, endpoint health and status, and "
        "alarm list from a running etcd pod.",
    },
    "etcd_more": {
        "description": "Extended etcd diagnostics: per-pod endpoint data, "
        "cluster-etcd-operator state and the last 24h of etcd metrics.",
    },
    "frrk8s": {
        "description": "FRR-K8s (openshift-frr-k8s) CRs, pod logs and FRR "
        "runtime state. No-op if not installed.",
    },
    "haproxy_config": {
        "description": "haproxy.config from each router pod of every "
        "IngressController.",
    },
    "ingress_node_firewall": {
        "description": "Ingress Node Firewall operator CRs and logs. No-op if "
        "the operator is not subscribed.",
    },
    "insights": {
        "description": "Insights Operator archives from "
        "/var/lib/insights-operator.",
        "options": {
            "collector": {
                "kind": "positional",
                "type": "str",
                "index": 0,
                "choices": ["rsync", "cp"],
                "help": "oc subcommand used to pull the archives (default rsync).",
            },
        },
    },
    "istio": {
        "description": "Istio / OpenShift Service Mesh and Gateway API "
        "resources. No-op if not installed.",
    },
    "kas_startup_termination_logs": {
        "description": "kube-apiserver startup.log and termination.log (and "
        "rotated copies) from each master.",
    },
    "machineconfig_ondisk": {
        "description": "On-disk MachineConfig content and bootstrap config "
        "diffs from nodes in a Degraded state.",
    },
    "machineconfigdaemon_termination_logs": {
        "description": "Previous machine-config-daemon logs "
        "(/etc/machine-config-daemon/previous-logs) from every node.",
    },
    "metallb": {
        "description": "MetalLB operator CRs, speaker/controller logs and FRR "
        "state. No-op if the operator is not subscribed.",
    },
    "metrics": {
        "description": "Dump raw Prometheus TSDB data in OpenMetrics format. "
        "Refuses to run without at least one of min_time, max_time or match, "
        "because an unbounded dump can destabilise Prometheus.",
        "requires_option": True,
        "options": {
            "min_time": {
                "kind": "opt",
                "type": "str",
                "flag": "--min-time",
                "help": "Minimum timestamp to dump, in milliseconds.",
            },
            "max_time": {
                "kind": "opt",
                "type": "str",
                "flag": "--max-time",
                "help": "Maximum timestamp to dump, in milliseconds.",
            },
            "match": {
                "kind": "opt",
                "type": "list",
                "flag": "--match",
                "help": "Prometheus series selector. Repeatable.",
            },
        },
    },
    "monitoring": {
        "description": "openshift-monitoring state: Prometheus and Alertmanager "
        "status, targets, rules, firing alerts and TSDB stats.",
    },
    "network_logs": {
        "description": "Extra OVN-Kubernetes data. WARNING: this script starts "
        "by running the complete default gather, so it is a superset of every "
        "other plugin.",
        "superset": True,
        # Not derivable: it honours --since only via the /usr/bin/gather run it
        # starts with, not in its own extra OVN collection.
        "since": "pod-logs",
    },
    "network_logs_basics": {
        "description": "Core networking: cluster network operator, "
        "OVN-Kubernetes / SDN namespaces and pods, and network CRs.",
    },
    "nfd": {
        "description": "Node Feature Discovery CRs and operator logs. No-op if "
        "the operator is not subscribed.",
    },
    "nmstate": {
        "description": "kubernetes-nmstate CRs (NNCP, NNCE, NodeNetworkState) "
        "and operator logs. No-op if the operator is not subscribed.",
    },
    "olm": {
        "description": "Operator Lifecycle Manager resources across all "
        "namespaces.",
    },
    "olm_v1": {
        "description": "OLM v1 (olm.operatorframework.io) ClusterExtensions, "
        "ClusterCatalogs and related resources.",
    },
    "osus": {
        "description": "OpenShift Update Service (OSUS) resources. No-op if the "
        "operator is not installed.",
    },
    "podnetworkconnectivitycheck": {
        "description": "PodNetworkConnectivityCheck resources from "
        "openshift-network-diagnostics.",
    },
    "ppc": {
        "description": "Performance profile and node tuning data: tuned "
        "profiles, kubelet config and per-node collection via the NTO image.",
    },
    "priority_and_fairness": {
        "description": "API Priority and Fairness debug endpoints, which "
        "oc adm inspect deliberately skips.",
    },
    "profiling_node": {
        "description": "pprof profiles from kubelet and CRI-O on nodes.",
        "options": {
            "seconds": {
                "kind": "env",
                "type": "str",
                "env": "PROFILING_NODE_SECONDS",
                "help": "Profile duration in seconds (default 30).",
            },
            "target": {
                "kind": "env",
                "type": "str",
                "env": "PROFILING_NODE_TARGET",
                "help": "Node name to profile, or ALL (default ALL).",
            },
            "image": {
                "kind": "env",
                "type": "str",
                "env": "PROFILING_NODE_IMAGE",
                "help": "Support-tools image used to run the profiler.",
            },
        },
    },
    "service_logs": {
        "description": "Host systemd service journals (crio, kubelet, "
        "NetworkManager, openvswitch, ...) collected with oc adm node-logs.",
        "options": {
            "roles": {
                "kind": "positional",
                "type": "str",
                "index": 0,
                "choices": ["master", "worker", "master,worker", "worker,master"],
                "help": "Which node role(s) to collect from (default master).",
            },
            "services": {
                "kind": "positional",
                "type": "list",
                "index": 1,
                "separator": " ",
                "help": "Override the systemd unit list, comma separated.",
            },
        },
    },
    "sriov": {
        "description": "SR-IOV Network Operator CRs, node states and operator "
        "logs. No-op if the operator is not subscribed.",
    },
    "virt": {
        "description": "OpenShift Virtualization / KubeVirt and Forklift "
        "custom resource summaries.",
    },
    "vsphere": {
        "description": "vSphere CSI driver CRDs and configuration. No-op on "
        "non-vSphere platforms.",
    },
    "windows_node_logs": {
        "description": "Kubernetes component logs from Windows nodes.",
    },
}

CORE_DESCRIPTION = (
    "Base 'oc adm inspect' pass from upstream's gather script: cluster version, "
    "cluster operators, API services, CSRs, nodes, core namespaces, storage, "
    "image mirroring, networking, leases, API priority and fairness, and "
    "cluster resource quotas."
)

# named_resources+=(a b c)
_ARRAY_RE = re.compile(
    r"^\s*(named_resources|group_resources|all_ns_resources)\+=\(([^)]*)\)\s*$"
)

# [ENV=val ...] /usr/bin/gather_foo [args] [&]
_INVOKE_RE = re.compile(
    r"""^\s*
    (?P<env>(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*)
    /usr/bin/(?P<script>gather_[A-Za-z0-9_]+)
    (?P<args>(?:\s+[^\s&]+)*)
    \s*(?P<amp>&)?
    \s*$""",
    re.VERBOSE,
)


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw).stdout


def fetch_source(ref, src):
    """Return (checkout_dir, commit, cleanup_dir_or_None)."""
    if src:
        commit = run(["git", "-C", src, "rev-parse", "HEAD"]).strip()
        return src, commit, None
    tmp = tempfile.mkdtemp(prefix="mgs-sync-")
    print(f"cloning {REPO} @ {ref} ...", file=sys.stderr)
    run(["git", "clone", "--depth", "1", "--branch", ref, REPO, tmp])
    commit = run(["git", "-C", tmp, "rev-parse", "HEAD"]).strip()
    return tmp, commit, tmp


def parse_gather(path):
    """Parse collection-scripts/gather.

    Returns (core_resources, invocations) where invocations is an ordered list
    of {name, env, args, parallel}.
    """
    core = {"named_resources": [], "group_resources": [], "all_ns_resources": []}
    invocations = []
    seen_wait = False

    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if stripped.startswith("#"):
                continue

            m = _ARRAY_RE.match(line)
            if m:
                core[m.group(1)].extend(m.group(2).split())
                continue

            # Everything after the `wait` on the pid array runs serially.
            if re.match(r'^\s*wait\s+"\$\{pids\[@\]\}"', line):
                seen_wait = True
                continue

            m = _INVOKE_RE.match(line)
            if m:
                env = {}
                for assignment in m.group("env").split():
                    key, _, value = assignment.partition("=")
                    env[key] = value
                args = m.group("args").split()
                parallel = m.group("amp") == "&" and not seen_wait
                invocations.append(
                    {
                        "name": m.group("script")[len("gather_") :],
                        "env": env,
                        "args": args,
                        "parallel": parallel,
                    }
                )

    for key, values in core.items():
        if not values:
            raise SystemExit(f"sync: parsed no {key} from {path} -- did the format change?")
    if not invocations:
        raise SystemExit(f"sync: parsed no plugin invocations from {path}")
    return core, invocations


def _script_text(scripts_dir, name):
    """Script source with any sibling files it sources inlined."""
    path = os.path.join(scripts_dir, name)
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    for sourced in re.findall(r"^\s*(?:\.|source)\s+[\"']?([\w./$(){}-]+)[\"']?\s*$",
                              text, re.M):
        sibling = os.path.basename(sourced.strip("\"'"))
        if sibling in ("common.sh", name) or "$" in sibling:
            continue
        sibling_path = os.path.join(scripts_dir, sibling)
        if os.path.exists(sibling_path):
            with open(sibling_path, encoding="utf-8", errors="replace") as fh:
                text += "\n" + fh.read()
    return text


def classify_since(scripts_dir, name):
    """How far --since / --since-time actually reach into this plugin.

    'pod-logs'  passes ${log_collection_args} to oc adm inspect, so pod logs
                are trimmed.
    'journal'   queries host journals with oc adm node-logs and no --path, so
                the journal query is trimmed.
    'both'      does both of the above.
    'none'      neither -- including plugins that pass the since arguments to
                'oc adm node-logs --path=FILE', where oc silently drops them:
                node_logs.go only attaches the 'since' parameter when the path
                is 'journal' (the flag help says "Only applies to node service
                logs").
    """
    text = _script_text(scripts_dir, name)
    pod_logs = "${log_collection_args}" in text

    journal = False
    for line in text.splitlines():
        if "node-logs" not in line or line.lstrip().startswith("#"):
            continue
        if "--path" not in line:
            journal = True

    if pod_logs and journal:
        return "both"
    if pod_logs:
        return "pod-logs"
    if journal:
        return "journal"
    return "none"


def discover_scripts(scripts_dir):
    """All runnable gather_* scripts in collection-scripts/."""
    names = []
    for entry in sorted(os.listdir(scripts_dir)):
        if not entry.startswith("gather_") or entry in NOT_PLUGINS:
            continue
        if not os.access(os.path.join(scripts_dir, entry), os.X_OK):
            continue
        names.append(entry[len("gather_") :])
    return names


def build_catalog(src, ref, commit):
    scripts_dir = os.path.join(src, "collection-scripts")
    core, invocations = parse_gather(os.path.join(scripts_dir, "gather"))
    all_names = discover_scripts(scripts_dir)
    invoked = {inv["name"]: inv for inv in invocations}

    unknown = sorted(set(invoked) - set(all_names))
    if unknown:
        raise SystemExit(f"sync: gather invokes unknown scripts: {unknown}")

    plugins = {}
    order = 0
    for name in all_names:
        inv = invoked.get(name)
        overlay = dict(OVERLAY.get(name, {}))
        if "description" not in overlay:
            print(f"warning: no description in OVERLAY for '{name}'", file=sys.stderr)

        plugin = {
            "script": f"/usr/bin/gather_{name}",
            "default": inv is not None,
            "phase": "parallel" if (inv is None or inv["parallel"]) else "post",
            "order": order if inv is None else list(invoked).index(name),
            "description": overlay.pop("description", ""),
            "since": classify_since(scripts_dir, f"gather_{name}"),
        }
        order += 1

        if inv:
            if inv["args"]:
                plugin["args"] = inv["args"]
            if inv["env"]:
                # gather sets MUST_GATHER_NODES_INSPECTED=true for gather_ppc
                # only because the base inspect already covered nodes.  Record
                # it as conditional on `core` being selected.
                if inv["env"].get("MUST_GATHER_NODES_INSPECTED"):
                    plugin["env_if_core"] = dict(inv["env"])
                else:
                    plugin["env"] = dict(inv["env"])

        plugin.update(overlay)  # options, superset, requires_option
        plugins[name] = plugin

    stale = sorted(set(OVERLAY) - set(all_names))
    if stale:
        print(f"warning: OVERLAY has entries for missing scripts: {stale}", file=sys.stderr)

    return {
        "catalog_version": 1,
        "source": {
            "repo": REPO,
            "ref": ref,
            "commit": commit,
            "synced": datetime.datetime.now(datetime.timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        },
        "core": {
            "description": CORE_DESCRIPTION,
            "since": "pod-logs",
            "named_resources": core["named_resources"],
            "group_resources": core["group_resources"],
            "all_ns_resources": core["all_ns_resources"],
        },
        "plugins": plugins,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", default="main", help="branch or tag of openshift/must-gather")
    ap.add_argument("--src", help="use an existing checkout instead of cloning")
    ap.add_argument("-o", "--out", default=os.path.normpath(DEFAULT_OUT))
    args = ap.parse_args(argv)

    src, commit, cleanup = fetch_source(args.ref, args.src)
    try:
        catalog = build_catalog(src, args.ref, commit)
        for upstream_path, local_path in VENDORED.items():
            target = os.path.join(ROOT, local_path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(os.path.join(src, upstream_path), target)
            print(f"vendored {local_path}")
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, indent=2, sort_keys=False)
        fh.write("\n")

    defaults = sum(1 for p in catalog["plugins"].values() if p["default"])
    print(
        f"wrote {args.out}: {len(catalog['plugins'])} plugins "
        f"({defaults} default) from {args.ref} @ {commit[:12]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
