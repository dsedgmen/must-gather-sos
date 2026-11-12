"""Dedicated run namespace with CPU/memory limits for must-gather pods.

``oc adm must-gather`` has no flag for container resources. The supported
workaround (Red Hat solution 7128538) is to create a namespace, grant its
default service account ``cluster-admin``, apply a LimitRange so every
container gets default CPU/memory requests and limits, then pass that
namespace as ``--run-namespace``.

This module builds the manifests and the paste-ready create / cleanup
commands, and can apply them through ``oc``.
"""

from __future__ import annotations

import subprocess
import textwrap

from . import UserError

#: Namespace used when ``--limit-resources`` is set without ``--run-namespace``.
DEFAULT_NAMESPACE = "must-gather-sos"

#: LimitRange defaults from Red Hat solution 7128538.
DEFAULT_CPU_LIMIT = "500m"
DEFAULT_CPU_REQUEST = "250m"
DEFAULT_MEMORY_LIMIT = "2Gi"
DEFAULT_MEMORY_REQUEST = "512Mi"

LIMIT_RANGE_NAME = "must-gather-limits"


def limit_range_yaml(
    cpu_limit=DEFAULT_CPU_LIMIT,
    cpu_request=DEFAULT_CPU_REQUEST,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    memory_request=DEFAULT_MEMORY_REQUEST,
):
    """YAML for a LimitRange that injects default CPU/memory on every container."""
    return textwrap.dedent(f"""\
        apiVersion: v1
        kind: LimitRange
        metadata:
          name: {LIMIT_RANGE_NAME}
        spec:
          limits:
          - type: Container
            default:
              cpu: {cpu_limit}
              memory: {memory_limit}
            defaultRequest:
              cpu: {cpu_request}
              memory: {memory_request}
        """)


def sa_subject(namespace):
    return f"system:serviceaccount:{namespace}:default"


def create_commands(
    namespace,
    cpu_limit=DEFAULT_CPU_LIMIT,
    cpu_request=DEFAULT_CPU_REQUEST,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    memory_request=DEFAULT_MEMORY_REQUEST,
    oc="oc",
):
    """Shell commands that set up the limited run namespace."""
    yaml = limit_range_yaml(cpu_limit, cpu_request, memory_limit, memory_request)
    return [
        f"{oc} create namespace {namespace}",
        f"{oc} adm policy add-cluster-role-to-user cluster-admin {sa_subject(namespace)}",
        f"{oc} apply -n {namespace} -f - <<'EOF'\n{yaml}EOF",
    ]


def cleanup_commands(namespace, oc="oc"):
    """Shell commands that tear the limited run namespace down."""
    return [
        f"{oc} adm policy remove-cluster-role-from-user cluster-admin "
        f"{sa_subject(namespace)}",
        f"{oc} delete namespace {namespace}",
    ]


def resolve_namespace(args):
    """Namespace ``--limit-resources`` will use, or None when the flag is off."""
    if not getattr(args, "limit_resources", False):
        return None
    return getattr(args, "run_namespace", None) or DEFAULT_NAMESPACE


def apply_run_namespace(args):
    """Ensure ``args.run_namespace`` is set when ``--limit-resources`` is on."""
    ns = resolve_namespace(args)
    if ns:
        args.run_namespace = ns
    return ns


def _run(oc, argv, check=True):
    proc = subprocess.run(
        [oc, *argv], text=True, capture_output=True, check=False
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        hint = detail[0] if detail else f"oc {' '.join(argv)} failed"
        raise UserError(f"failed to set up limited namespace: {hint}")
    return proc


def namespace_exists(oc, namespace):
    return _run(oc, ["get", "namespace", namespace], check=False).returncode == 0


def setup(
    oc,
    namespace,
    cpu_limit=DEFAULT_CPU_LIMIT,
    cpu_request=DEFAULT_CPU_REQUEST,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    memory_request=DEFAULT_MEMORY_REQUEST,
):
    """Create the namespace (if needed), grant cluster-admin, apply the LimitRange.

    Returns True when this call created the namespace, so the caller can decide
    whether deleting it on cleanup is safe.
    """
    created = False
    if not namespace_exists(oc, namespace):
        _run(oc, ["create", "namespace", namespace])
        created = True

    subject = sa_subject(namespace)
    proc = _run(
        oc,
        ["adm", "policy", "add-cluster-role-to-user", "cluster-admin", subject],
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip().lower()
        # A second run finds the binding already there; that is fine.
        if "already" not in err:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            hint = detail[0] if detail else "failed to grant cluster-admin"
            raise UserError(f"failed to set up limited namespace: {hint}")

    yaml = limit_range_yaml(cpu_limit, cpu_request, memory_limit, memory_request)
    proc = subprocess.run(
        [oc, "apply", "-n", namespace, "-f", "-"],
        input=yaml,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        hint = detail[0] if detail else "oc apply LimitRange failed"
        raise UserError(f"failed to set up limited namespace: {hint}")
    return created


def cleanup(oc, namespace, delete_namespace=True):
    """Remove the cluster-admin binding and, optionally, the namespace."""
    subject = sa_subject(namespace)
    _run(
        oc,
        ["adm", "policy", "remove-cluster-role-from-user", "cluster-admin", subject],
        check=False,
    )
    if delete_namespace:
        _run(oc, ["delete", "namespace", namespace, "--wait=false"], check=False)


def format_print_commands(create, gather, cleanup):
    """Multi-line paste block for ``--print-command`` with ``--limit-resources``."""
    parts = [
        "# create the limited run namespace",
        *create,
        "",
        "# gather",
        gather,
        "",
        "# clean up",
        *cleanup,
    ]
    return "\n".join(parts) + "\n"
