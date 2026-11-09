"""Offline tests for the multi-catalog sync registry and drift checker."""

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
    classify_drift,
    collect_status,
    render_report,
    write_readme,
)
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
        self.assertEqual(names, ["openshift", "better-gather", "acm"])
        openshift = by_name(entries, "openshift")
        self.assertEqual(openshift["sync"], "openshift-layout")
        self.assertIn("release-4.19", openshift["check_refs"])
        self.assertTrue(openshift["out"].endswith("openshift.json"))
        self.assertTrue(os.path.isabs(openshift["out"]))
        acm = by_name(entries, "acm")
        self.assertEqual(acm["sync"], "manual")

    def test_loads_fixture_registry(self):
        entries = load_registry(os.path.join(FIXTURES, "catalogs.yaml"))
        self.assertEqual([e["name"] for e in entries], ["fixture", "hand"])
        self.assertEqual(by_name(entries, "fixture")["sync_ref"], "base")

    def test_unknown_catalog_raises(self):
        entries = load_registry(os.path.join(FIXTURES, "catalogs.yaml"))
        with self.assertRaises(RegistryError):
            by_name(entries, "nope")

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


class TestBuildCatalogFromFixtures(unittest.TestCase):
    def test_base_tree_yields_expected_plugins(self):
        catalog = _build("base")
        self.assertEqual(catalog["name"], "fixture")
        self.assertEqual(
            set(catalog["plugins"]),
            {"etcd", "monitoring", "insights"},
        )
        self.assertTrue(catalog["plugins"]["etcd"]["default"])
        self.assertEqual(catalog["plugins"]["etcd"]["phase"], "parallel")
        self.assertEqual(catalog["plugins"]["insights"]["phase"], "post")
        self.assertEqual(catalog["plugins"]["etcd"]["since"], "pod-logs")
        self.assertEqual(catalog["plugins"]["insights"]["since"], "none")
        self.assertEqual(
            catalog["core"]["named_resources"],
            ["ns/default", "ns/openshift"],
        )
        self.assertEqual(catalog["image"]["match"], ["fixture-must-gather"])
        self.assertTrue(catalog["image"]["payload"])

    def test_sync_entry_writes_json(self):
        entries = load_registry(os.path.join(FIXTURES, "catalogs.yaml"))
        entry = dict(by_name(entries, "fixture"))
        with tempfile.TemporaryDirectory() as tmp:
            entry["out"] = os.path.join(tmp, "fixture.json")
            catalog = sync_entry(
                entry,
                ref="base",
                src=os.path.join(FIXTURES, "base"),
            )
            self.assertTrue(os.path.isfile(entry["out"]))
            with open(entry["out"], encoding="utf-8") as fh:
                written = json.load(fh)
            self.assertEqual(written["plugins"].keys(), catalog["plugins"].keys())
            self.assertEqual(written["source"]["ref"], "base")


class TestClassifyDrift(unittest.TestCase):
    def setUp(self):
        self.shipped = _build("base")

    def test_identical_is_match(self):
        status, notes = classify_drift(self.shipped, _build("base"))
        self.assertEqual(status, "match")
        self.assertEqual(notes, "")

    def test_additive_upstream_is_newer(self):
        status, notes = classify_drift(self.shipped, _build("newer"))
        self.assertEqual(status, "newer")
        self.assertIn("upstream adds 1 plugin", notes)
        self.assertIn("apirequestcounts", notes)
        self.assertIn("upstream --since stronger:", notes)

    def test_weaker_upstream_is_older(self):
        status, notes = classify_drift(self.shipped, _build("weaker"))
        self.assertEqual(status, "older")
        self.assertIn("upstream lacks 1 plugin (monitoring)", notes)
        self.assertIn("upstream --since weaker: etcd", notes)
        self.assertIn("upstream lacks", notes)
        self.assertIn("core resources", notes)


class TestCompatReport(unittest.TestCase):
    """CI-style: build the status tables from local fixture checkouts."""

    def test_report_tables_from_src_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Point a throwaway registry at fixture trees via --src-map.
            shipped = _build("base")
            shipped_path = os.path.join(tmp, "fixture.json")
            write_catalog(shipped, shipped_path)
            hand = {
                "catalog_version": 2,
                "name": "hand",
                "image": {"default": None, "match": [], "payload": False},
                "profile": {
                    "id": "hand",
                    "detect": "true",
                    "base_path": "/must-gather",
                    "source": ["/usr/bin/true"],
                    "setup": [],
                    "version": {"banner": "hand", "command": None},
                    "since": False,
                    "reduce_logs": False,
                    "noop_pattern": None,
                },
                "source": {
                    "repo": "https://example.com/hand.git",
                    "ref": "main",
                    "commit": "deadbeef",
                    "synced": "2026-01-01T00:00:00+00:00",
                },
                "plugins": {
                    "one": {
                        "script": "/usr/bin/gather_one",
                        "default": True,
                        "phase": "parallel",
                        "order": 0,
                        "description": "stub",
                        "since": "none",
                    },
                },
            }
            hand_path = os.path.join(tmp, "hand.json")
            with open(hand_path, "w", encoding="utf-8") as fh:
                json.dump(hand, fh)

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
                    "  - name: hand\n"
                    "    sync: manual\n"
                    "    repo: https://example.com/hand.git\n"
                    f"    out: {hand_path}\n"
                )

            entries = load_registry(registry)
            src_map = {
                "fixture=base": os.path.join(FIXTURES, "base"),
                "fixture=newer": os.path.join(FIXTURES, "newer"),
                "fixture=weaker": os.path.join(FIXTURES, "weaker"),
            }
            shipped_rows, coverage = collect_status(entries, src_map=src_map)
            report = render_report(shipped_rows, coverage)

            self.assertIn("#### Shipped catalogs", report)
            self.assertIn("#### Upstream coverage", report)
            self.assertIn("| fixture", report)
            self.assertIn("| hand", report)
            self.assertIn("manual", report)

            by_ref = {r["ref"]: r["status"] for r in coverage}
            self.assertEqual(by_ref["base"], "match")
            self.assertEqual(by_ref["newer"], "newer")
            self.assertEqual(by_ref["weaker"], "older")

            fixture_row = next(r for r in shipped_rows if r["catalog"] == "fixture")
            self.assertEqual(fixture_row["status"], "match")

    def test_write_readme_is_surgical(self):
        before = (
            "# Title\n\n"
            "intro\n\n"
            f"{MARKER_START}\n\nold table\n\n{MARKER_END}\n\n"
            "outro\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "README.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(before)
            write_readme(
                "#### Shipped catalogs\n\n| Catalog | Status |\n"
                "| ------- | ------ |\n| acm | manual |\n",
                readme_path=path,
            )
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        self.assertTrue(text.startswith("# Title\n"))
        self.assertTrue(text.endswith("outro\n"))
        self.assertIn("| acm | manual |", text)
        self.assertNotIn("old table", text)
        self.assertEqual(text.count(MARKER_START), 1)
        self.assertEqual(text.count(MARKER_END), 1)

    def test_write_readme_requires_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "README.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("no markers here\n")
            with self.assertRaises(SystemExit):
                write_readme("| x |\n", readme_path=path)


if __name__ == "__main__":
    unittest.main()
