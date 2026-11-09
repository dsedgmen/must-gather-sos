"""Smoke-test that must-gather-sos can drive a given must-gather version.

Builds a catalog from an upstream checkout (or uses a shipped one), generates
the in-pod script for a small selection, and runs it through the same heredoc
mangling ``oc`` applies — against a fake image whose ``common.sh`` and
``gather_*`` stubs come from that version.

This is the compatibility signal the README/CI table reports: not "does the
JSON match", but "does our generated script still run against that must-gather".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# Allow `python tools/script_compat.py` and imports from sibling tools/.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from must_gather_sos import runner, script  # noqa: E402
from must_gather_sos.catalog import CORE, Catalog, build_selection  # noqa: E402

OC_POD_COMMAND = """\
set -e
HAVE_SESSION_TOOLS=false
{command}
"""

STUB = """\
#!/bin/bash
printf '%s\\t%s\\n' "$(basename "$0")" "$*" >>"$RECORD/invocations"
exit ${STUB_RC:-0}
"""

OC_STUB = """\
#!/bin/bash
printf '%s\\n' "$*" >>"$RECORD/oc-calls"
if [ "$1" = "get" ]; then
  case "$2" in
  *nonexistent*) exit 1 ;;
  *) exit 0 ;;
  esac
fi
exit 0
"""

VERSION_STUB = """\
#!/usr/bin/env bash
function version() { echo "compat-test"; }
"""


class CompatSandbox:
    """Minimal fake must-gather image for a compatibility smoke test."""

    def __init__(self, catalog, common_sh=None):
        self.root = tempfile.mkdtemp(prefix="mgs-compat-")
        self.bin = os.path.join(self.root, "bin")
        self.record = os.path.join(self.root, "record")
        self.collect = os.path.join(self.root, "must-gather")
        for path in (self.bin, self.record, self.collect):
            os.makedirs(path, exist_ok=True)

        if common_sh and os.path.isfile(common_sh):
            shutil.copyfile(common_sh, os.path.join(self.bin, "common.sh"))
        else:
            # Profile may not need it (e.g. ACM); leave a no-op.
            with open(os.path.join(self.bin, "common.sh"), "w", encoding="utf-8") as fh:
                fh.write("# no common.sh in this image family\n")

        self._write("version", VERSION_STUB)
        self._write("oc", OC_STUB)
        # Profile libraries (common.sh already handled; gather_utils for ACM).
        for src in catalog.profile.source:
            base = os.path.basename(src)
            if base == "common.sh":
                continue
            path = os.path.join(self.bin, base)
            if not os.path.exists(path):
                self._write(base, "#!/bin/bash\n# stub library\n")
        for name, plugin in catalog.plugins.items():
            if plugin.is_core:
                continue
            # Function-style scripts (ACM dump_hostedcluster) are not files.
            if plugin.script and plugin.script.startswith("/"):
                self._write(os.path.basename(plugin.script), STUB)

    def _write(self, name, content):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(path, 0o755)

    def localise(self, text):
        text = text.replace("/usr/bin/", self.bin + "/")
        text = re.sub(r"(?<![\w-])/must-gather/", self.collect + "/", text)
        text = re.sub(r"(?<![\w-])/must-gather(?![-\w])", self.collect, text)
        return text.replace("cd / || exit 1", f"cd {self.root} || exit 1")

    def run(self, text):
        payload = runner.encode_payload(text).replace(
            runner.POD_SCRIPT, os.path.join(self.root, "run.sh")
        )
        pod_command = OC_POD_COMMAND.format(command=payload)
        env = dict(os.environ)
        env.update({
            "RECORD": self.record,
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
        })
        return subprocess.run(
            ["bash", "-c", pod_command],
            env=env, capture_output=True, text=True, check=False,
        )

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def _pick_selection(catalog):
    """A small, always-valid selection for the smoke test."""
    names = [n for n in catalog.names if n != CORE]
    if not names:
        return build_selection(catalog, all_plugins=True)
    # Prefer a default plugin if any; otherwise the first by sort order.
    defaults = [n for n in names if catalog.get(n).default]
    pick = sorted(defaults or names, key=catalog.sort_key)[:2]
    if CORE in catalog.plugins:
        pick = [CORE] + pick
    return build_selection(catalog, only=",".join(pick))


def smoke_test(catalog, common_sh=None):
    """Run one generate+execute smoke test. Return (ok, detail)."""
    sandbox = CompatSandbox(catalog, common_sh=common_sh)
    try:
        selection = _pick_selection(catalog)
        text = script.generate(catalog, selection)
        proc = sandbox.run(sandbox.localise(text))
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = tail[-1] if tail else f"exit {proc.returncode}"
            return False, detail[:100]
        return True, f"{len(selection.names)} plugins exercised"
    except Exception as exc:  # noqa: BLE001 — reported as a table cell
        return False, f"{type(exc).__name__}: {exc}"[:100]
    finally:
        sandbox.cleanup()


def smoke_test_from_doc(doc, common_sh=None):
    """Write *doc* to a temp catalog file, load it, and smoke-test."""
    with tempfile.TemporaryDirectory(prefix="mgs-cat-") as tmp:
        path = os.path.join(tmp, f"{doc.get('name', 'catalog')}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        catalog = Catalog.load(path)
        return smoke_test(catalog, common_sh=common_sh)


def common_sh_for(src_dir):
    """Locate common.sh in an upstream must-gather checkout."""
    if not src_dir:
        return None
    candidate = os.path.join(src_dir, "collection-scripts", "common.sh")
    return candidate if os.path.isfile(candidate) else None
