"""Build and execute the ``oc adm must-gather`` invocation.

Getting a generated script into the pod is not as simple as it looks, because
``oc`` does two things to whatever follows ``--``:

1. ``executedCommand = strings.Join(o.Command, " ")`` -- the argv is flattened
   into one string with spaces, so a multi-element command is re-split by the
   shell later.
2. ``buildPodCommand`` embeds that string in an **unquoted** heredoc::

       setsid -w bash <<-MUSTGATHER_EOF
       <command>
       MUSTGATHER_EOF

   An unquoted heredoc delimiter means the *outer* shell expands ``$VAR``,
   ``$(...)`` and backticks before the inner bash ever sees the text.

(Both are in openshift/oc, ``pkg/cli/admin/mustgather/mustgather.go``.)

So we pass exactly one argv element, and we make that element free of anything
the outer shell could act on by base64-encoding the real script: base64's
alphabet is ``A-Za-z0-9+/=``, all inert inside single quotes and inside a
heredoc.  :func:`encode_payload` is the whole trick; :func:`assert_payload_safe`
is the guard that keeps it true.
"""

from __future__ import annotations

import base64
import os
import random
import re
import shutil
import subprocess
import sys

from . import UserError

#: Where the decoded script is written inside the pod.
POD_SCRIPT = "/tmp/must-gather-sos.sh"

_SAFE_PAYLOAD = re.compile(r"^[A-Za-z0-9+/=]+$")

#: ``oc adm must-gather`` flags we forward untouched, as (attr, flag) pairs.
PASSTHROUGH = [
    ("since", "--since"),
    ("since_time", "--since-time"),
    ("image", "--image"),
    ("image_stream", "--image-stream"),
    ("timeout", "--timeout"),
    ("node_name", "--node-name"),
    ("node_selector", "--node-selector"),
    ("run_namespace", "--run-namespace"),
    ("volume_percentage", "--volume-percentage"),
]


def encode_payload(script):
    """Wrap a bash program in a single shell-inert command string."""
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    assert_payload_safe(payload)
    return f"echo '{payload}' | base64 -d >{POD_SCRIPT} && bash {POD_SCRIPT}"


def assert_payload_safe(payload):
    """Fail loudly if the payload could survive into the outer shell unscathed."""
    if not _SAFE_PAYLOAD.match(payload):
        raise UserError(
            "internal error: encoded payload contains characters that oc's "
            "unquoted heredoc would expand"
        )


def default_dest_dir():
    """A fresh output directory, named the way ``oc`` names its own."""
    return f"must-gather.local.{random.randint(10 ** 9, 10 ** 10 - 1)}"


def build_oc_args(args, payload, oc="oc"):
    """Assemble the full ``oc adm must-gather`` argv.

    The returned list ends with ``--`` followed by *exactly one* element.
    """
    argv = [oc, "adm", "must-gather", f"--dest-dir={args.dest_dir}"]

    for attr, flag in PASSTHROUGH:
        value = getattr(args, attr, None)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            for item in value:
                argv.append(f"{flag}={item}")
        else:
            argv.append(f"{flag}={value}")

    if getattr(args, "host_network", False):
        argv.append("--host-network=true")
    if getattr(args, "source_dir", None):
        argv.append(f"--source-dir={args.source_dir}")

    argv.append("--")
    argv.append(payload)
    return argv


def which_oc(oc):
    path = shutil.which(oc)
    if not path:
        raise UserError(
            f"{oc!r} not found on PATH. Install the OpenShift CLI or pass --oc /path/to/oc."
        )
    return path


def check_cluster(oc):
    """Fail early with a clear message instead of deep inside oc."""
    proc = subprocess.run(
        [oc, "whoami"], text=True, capture_output=True, check=False
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        hint = detail[0] if detail else "oc whoami failed"
        raise UserError(f"not logged in to a cluster: {hint}")
    return proc.stdout.strip()


def probe(oc, argv, timeout=30):
    """Run a short read-only oc command. Returns stdout, or None on any failure."""
    try:
        proc = subprocess.run(
            [oc, *argv], text=True, capture_output=True, check=False, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def execute(argv, log=None):
    """Run oc, streaming its output to our own stdout/stderr. Returns the rc."""
    log = log or sys.stderr
    print(f"running: {' '.join(_display(a) for a in argv[:-1])} <payload>", file=log)
    try:
        return subprocess.call(argv)
    except KeyboardInterrupt:
        print("\ninterrupted; the must-gather pod may still be running", file=log)
        return 130
    except OSError as exc:
        raise UserError(f"failed to run {argv[0]}: {exc}") from None


def _display(arg):
    return arg if re.match(r"^[\w@%+=:,./-]+$", arg) else repr(arg)


def resolve_output_dir(dest_dir):
    """oc nests its output one level down, under a per-image directory."""
    if not os.path.isdir(dest_dir):
        return None
    entries = [
        os.path.join(dest_dir, name)
        for name in sorted(os.listdir(dest_dir))
        if os.path.isdir(os.path.join(dest_dir, name))
    ]
    return entries[0] if len(entries) == 1 else dest_dir
