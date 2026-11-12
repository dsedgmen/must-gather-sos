"""Emit a MustGather CR for openshift/must-gather-operator.

The operator runs gather in-cluster (optional SFTP upload, obfuscation, PVC).
It accepts a custom ``gatherSpec.command`` / ``args``, but the CRD caps each
string at 256 characters and each list at 256 items. The full must-gather-sos
script is therefore base64-encoded and split across ``args``, with a short
bootstrap in ``command`` that reassembles and runs it — the same payload
``runner.encode_payload`` builds for ``oc``, without oc's heredoc quoting
problem (the operator sets container argv directly).

See https://github.com/openshift/must-gather-operator
"""

from __future__ import annotations

import base64
import re

from . import UserError
from .runner import POD_SCRIPT, assert_payload_safe

API_VERSION = "operator.openshift.io/v1"
KIND = "MustGather"

DEFAULT_NAME = "must-gather-sos"
DEFAULT_SERVICE_ACCOUNT = "must-gather-admin"
DEFAULT_CASE_SECRET = "case-management-creds"

#: Matches the CRD markers on GatherSpec.Command / Args
#: (``MaxItems=256``, ``Items:MaxLength=256``).
CHUNK_SIZE = 256
MAX_CHUNKS = 256

#: Bootstrap that becomes gatherSpec.command. Kept under CHUNK_SIZE.
#: ``$@`` skips $0 (the ``--`` placeholder) and concatenates the base64
#: chunks from args.
_BOOTSTRAP = (
    f'printf \'%s\' "$@" | base64 -d >{POD_SCRIPT} && bash {POD_SCRIPT}'
)

_IMAGE_STREAM = re.compile(
    r"^(?:(?P<ns>[^/]+)/)?(?P<name>[^:]+):(?P<tag>.+)$"
)


def encode_chunks(script):
    """Return ``(command, args)`` for gatherSpec that run *script* in-pod."""
    if len(_BOOTSTRAP) > CHUNK_SIZE:
        raise UserError(
            "internal error: MustGather bootstrap exceeds CRD item maxLength"
        )
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    assert_payload_safe(payload)
    chunks = [
        payload[i:i + CHUNK_SIZE]
        for i in range(0, len(payload), CHUNK_SIZE)
    ]
    if len(chunks) > MAX_CHUNKS:
        raise UserError(
            f"generated gather script is too large for a MustGather CR "
            f"({len(chunks)} base64 chunks > {MAX_CHUNKS} args); "
            "narrow the plugin selection"
        )
    command = ["/bin/bash", "-c", _BOOTSTRAP, "--"]
    return command, chunks


def parse_image_stream(ref):
    """Map an ``oc --image-stream`` value to operator ``imageStreamRef``.

    Accepts ``NAME:TAG`` or ``NAMESPACE/NAME:TAG``. The operator resolves the
    ImageStream in its own namespace, so the optional namespace prefix is
    dropped.
    """
    if not ref:
        return None
    m = _IMAGE_STREAM.match(ref)
    if not m:
        raise UserError(
            f"--image-stream {ref!r} is not NAME:TAG or NAMESPACE/NAME:TAG"
        )
    return {"name": m.group("name"), "tag": m.group("tag")}


def build(
    script,
    *,
    name=DEFAULT_NAME,
    namespace=None,
    service_account=DEFAULT_SERVICE_ACCOUNT,
    case_id=None,
    case_secret=DEFAULT_CASE_SECRET,
    internal_user=False,
    since=None,
    since_time=None,
    timeout=None,
    image_stream=None,
    obfuscate=False,
):
    """Build a MustGather document (plain dict) for the given in-pod script."""
    if obfuscate and not case_id:
        raise UserError(
            "--obfuscate with --print-mustgather requires --case-id "
            "(operator needs uploadTarget, storage, or obfuscate.source)"
        )

    command, args = encode_chunks(script)
    gather_spec = {"command": command, "args": args}
    if since:
        gather_spec["since"] = since
    if since_time:
        gather_spec["sinceTime"] = since_time

    spec = {
        "serviceAccountName": service_account,
        "gatherSpec": gather_spec,
    }

    image_stream_ref = parse_image_stream(image_stream)
    if image_stream_ref:
        spec["imageStreamRef"] = image_stream_ref

    if timeout:
        spec["mustGatherTimeout"] = timeout

    if case_id:
        sftp = {
            "caseID": str(case_id),
            "caseManagementAccountSecretRef": {"name": case_secret},
        }
        if internal_user:
            sftp["internalUser"] = True
        spec["uploadTarget"] = {"type": "SFTP", "sftp": sftp}

    if obfuscate:
        spec["obfuscate"] = {"enabled": True}

    metadata = {"name": name}
    if namespace:
        metadata["namespace"] = namespace

    return {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "metadata": metadata,
        "spec": spec,
    }


def _yaml_scalar(value):
    """Render a scalar for our constrained YAML subset."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    # Quote when YAML might misread the value (case IDs, durations, etc.).
    if (
        not text
        or text[0] in "0123456789"
        or any(c in text for c in ":{}[]#&*!|>'\"%@`")
        or text in ("true", "false", "null", "yes", "no", "on", "off")
        or text != text.strip()
    ):
        return "'" + text.replace("'", "''") + "'"
    return text


def _emit(doc, indent=0, lines=None):
    """Serialize a dict/list/scalar tree to YAML lines (stdlib only)."""
    if lines is None:
        lines = []
    pad = "  " * indent
    if isinstance(doc, dict):
        for key, value in doc.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}:")
                _emit(value, indent + 1, lines)
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(value)}")
    elif isinstance(doc, list):
        for item in doc:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                _emit(item, indent + 1, lines)
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
    else:
        lines.append(f"{pad}{_yaml_scalar(doc)}")
    return lines


def render_yaml(doc):
    """Return a MustGather CR as YAML text ending in a newline."""
    return "\n".join(_emit(doc)) + "\n"
