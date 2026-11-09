"""Offline tests: registry, sync fixtures, and per-version script smoke tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
FIXTURES = os.path.join(ROOT, "tests", "data", "sync")

if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from catalog_registry import RegistryError, by_name, load_registry  # noqa: E402
from catalog_status import (  # noqa: E402
    MARKER_END,
    MARKER_START,
    collect_status,
    render_report,
    write_readme,
)
from script_compat import common_sh_for, smoke_test_from_doc  # noqa: E402
from sync_catalog import build_catalog, sync_entry, write_catalog  # noqa: E402


def _build(tree, name="fixture", **kw):
    src = os.path.join(FIXTURES, tree)
    return build_catalog(
        src, tree, commit=f"commit-{tree}",
        name=name, repo="https://example.com/must-gather.git",
        image_match=["fixture-must-gather"],
        **kw,
    )


class TestCatalogRegistry(unittest.TestCase):
    def test_loads_shipped_registry(self):
        entries = load_registry()
        names = [e["name"] for e in entries]
        self.assertIn("openshift", names)
        self.assertIn("openshift-4.18", names)
        self.assertIn("better-gather", names)
        self.assertIn("better-gather-4.20", names)
        self.assertIn("acm", names)
        self.assertIn("odf", names)
        self.assertIn("better-gather-odf", names)
        odf = by_name(entries, "odf")
        self.assertEqual(odf["sync"], "manual")
        self.assertIn("odf-must-gather", odf["image_match"])
        bgo = by_name(entries, "better-gather-odf")
        self.assertEqual(bgo["sync"], "manual")
        self.assertIn("better-gather-odf", bgo["image_match"])
        openshift = by_name(entries, "openshift")
        self.assertEqual(openshift["sync"], "openshift-layout")
        self.assertEqual(openshift["sync_ref"], "main")
        self.assertTrue(openshift["payload"])
        self.assertIsNone(openshift["version"])
        release = by_name(entries, "openshift-4.19")
        self.assertEqual(release["family"], "openshift")
        self.assertEqual(release["sync_ref"], "release-4.19")
        self.assertEqual(release["version"], "4.19")
        self.assertFalse(release["payload"])

    def test_rejects_bad_sync_method(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(
                "catalogs:\n"
                "  - name: x\n"
                "    sync: magic\n"
                "    repo: https://example.com/x.git\n"
                "    out: x.json\n"
            )
            path = fh.name
        try:
            with self.assertRaises(RegistryError):
                load_registry(path)
        finally:
            os.unlink(path)


class TestScriptCompat(unittest.TestCase):
    """The script must generate+run against each fixture must-gather tree."""

    def test_base_tree_passes(self):
        doc = _build("base")
        ok, detail = smoke_test_from_doc(
            doc, common_sh=common_sh_for(os.path.join(FIXTURES, "base")),
        )
        self.assertTrue(ok, detail)

    def test_newer_tree_passes(self):
        doc = _build("newer")
        ok, detail = smoke_test_from_doc(
            doc, common_sh=common_sh_for(os.path.join(FIXTURES, "newer")),
        )
        self.assertTrue(ok, detail)

    def test_weaker_tree_passes(self):
        doc = _build("weaker")
        ok, detail = smoke_test_from_doc(
            doc, common_sh=common_sh_for(os.path.join(FIXTURES, "weaker")),
        )
        self.assertTrue(ok, detail)

    def test_shipped_openshift_passes(self):
        from must_gather_sos.catalog import Catalog
        catalog = Catalog.load("openshift")
        common = os.path.join(ROOT, "tests", "data", "upstream", "common.sh")
        with open(catalog.path, encoding="utf-8") as fh:
            doc = json.load(fh)
        ok, detail = smoke_test_from_doc(
            doc, common_sh=common if os.path.isfile(common) else None,
        )
        self.assertTrue(ok, detail)


class TestCompatReport(unittest.TestCase):
    def test_report_tables_from_src_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            shipped = _build("base")
            shipped_path = os.path.join(tmp, "fixture.json")
            write_catalog(shipped, shipped_path)

            registry = os.path.join(tmp, "catalogs.yaml")
            with open(registry, "w", encoding="utf-8") as fh:
                fh.write(
                    "catalogs:\n"
                    "  - name: fixture\n"
                    "    sync: openshift-layout\n"
                    "    repo: https://example.com/must-gather.git\n"
                    "    sync_ref: base\n"
                    "    check_refs: [base, newer, weaker]\n"
                    f"    out: {shipped_path}\n"
                    "    payload: true\n"
                    "    image_match: [fixture-must-gather]\n"
                )

            entries = load_registry(registry)
            src_map = {
                "fixture=base": os.path.join(FIXTURES, "base"),
                "fixture=newer": os.path.join(FIXTURES, "newer"),
                "fixture=weaker": os.path.join(FIXTURES, "weaker"),
            }
            shipped_rows, compat = collect_status(entries, src_map=src_map)
            report = render_report(shipped_rows, compat)

            self.assertIn("#### Script compatibility", report)
            self.assertIn("**fixture** (", report)
            self.assertIn("img.shields.io/badge/pass-", report)
            self.assertNotIn("| pass |", report)
            self.assertEqual(len(compat), 1)
            self.assertEqual(compat[0]["status"], "pass")
            self.assertIn("plugins", compat[0]["notes"])

    def test_write_readme_is_surgical(self):
        before = (
            "# Title\n\n"
            f"{MARKER_START}\n\nold\n\n{MARKER_END}\n\n"
            "outro\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "README.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(before)
            write_readme("#### Script compatibility\n\n| ok |\n", readme_path=path)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        self.assertIn("Script compatibility", text)
        self.assertNotIn("old", text)
        self.assertTrue(text.endswith("outro\n"))


if __name__ == "__main__":
    unittest.main()
