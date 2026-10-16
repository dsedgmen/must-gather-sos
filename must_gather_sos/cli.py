"""Command line interface."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import textwrap

from . import PROG, UserError, __version__
from . import archive as archive_mod
from . import manifest as manifest_mod
from . import runner, script
from .catalog import (SINCE_HELP, SINCE_LABEL, Catalog, build_selection,
                      split_list)

_DURATION = re.compile(r"^\d+(\.\d+)?(ns|us|ms|s|m|h)$")
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)
REDUCE_LOGS_CHOICES = ("skip_rotated_logs", "compress_service_logs", "compress_logs")

EPILOG = """\
examples:
  %(prog)s                                  everything the default gather does
  %(prog)s -n monitoring,istio              ... minus two noisy plugins
  %(prog)s -o core,etcd,sriov               only those, nothing else
  %(prog)s -e audit_logs --since 2h         defaults plus audit logs, last 2h
  %(prog)s -o audit_logs -k audit_logs.force=on
  %(prog)s -o core,etcd --case-id 04123456 --archive
  %(prog)s --list-plugins
"""


def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="An sos-style front end for 'oc adm must-gather': list, "
        "select or skip the collection plugins instead of always running "
        "every one of them.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version=f"{PROG} {__version__}")

    selection = parser.add_argument_group("plugin selection")
    selection.add_argument("-l", "--list-plugins", action="store_true",
                           help="list available plugins and exit")
    selection.add_argument("--format", choices=("text", "json"), default="text",
                           help="output format for --list-plugins (default text)")
    selection.add_argument("--list-plugin-names", action="store_true",
                           help="print bare plugin names, one per line "
                                "(for scripting and shell completion)")
    selection.add_argument("--list-plugin-options", action="store_true",
                           help="print bare 'plugin.option' keys, one per line")
    selection.add_argument("-o", "--only-plugins", metavar="LIST", default="",
                           help="run only these plugins (comma separated); "
                                "include 'core' to keep the base oc adm inspect pass")
    selection.add_argument("-n", "--skip-plugins", metavar="LIST", default="",
                           help="run the defaults except these")
    selection.add_argument("-e", "--enable-plugins", metavar="LIST", default="",
                           help="additionally run these non-default plugins")
    selection.add_argument("-a", "--all-plugins", action="store_true",
                           help="run every plugin, including the non-default ones")
    selection.add_argument("-k", "--plugin-option", metavar="PLUG.OPT=VAL",
                           action="append", default=[], dest="plugin_options",
                           help="set a plugin option; repeatable")

    collection = parser.add_argument_group("collection")
    collection.add_argument("--since", metavar="DURATION",
                            help="only logs newer than e.g. 5s, 2m, 3h")
    collection.add_argument("--since-time", metavar="RFC3339",
                            help="only logs after a specific time")
    collection.add_argument("--reduce-logs", metavar="LIST", default="",
                            help="comma separated: " + ", ".join(REDUCE_LOGS_CHOICES))

    output = parser.add_argument_group("output")
    output.add_argument("--dest-dir", metavar="DIR",
                        help="write gathered data here (default must-gather.local.<rand>)")
    output.add_argument("--case-id", metavar="ID",
                        help="support case number, recorded in manifest.json "
                             "and used in the archive name")
    output.add_argument("--archive", nargs="?", const="xz", default=None,
                        choices=("xz", "gz", "none"), metavar="FORMAT",
                        help="tar up the result (default format xz)")
    output.add_argument("--no-manifest", action="store_true",
                        help="do not write manifest.json into the output")

    passthrough = parser.add_argument_group("oc adm must-gather passthrough")
    passthrough.add_argument("--image", metavar="IMAGE", action="append", default=[],
                             help="must-gather plugin image to run")
    passthrough.add_argument("--image-stream", metavar="NS/NAME:TAG")
    passthrough.add_argument("--timeout", metavar="DURATION",
                             help="how long to gather for (oc default 10m)")
    passthrough.add_argument("--node-name", metavar="NODE")
    passthrough.add_argument("--node-selector", metavar="SELECTOR")
    passthrough.add_argument("--run-namespace", metavar="NAMESPACE")
    passthrough.add_argument("--host-network", action="store_true")
    passthrough.add_argument("--volume-percentage", metavar="N", type=int)
    passthrough.add_argument("--source-dir", metavar="DIR",
                             help="directory in the pod to copy from "
                                  "(oc default /must-gather/)")

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument("--dry-run", action="store_true",
                           help="print the generated script and oc command, run nothing")
    behaviour.add_argument("--print-command", action="store_true",
                           help="print only the full oc command, shell-quoted "
                                "and ready to paste, and exit")
    behaviour.add_argument("--print-script", action="store_true",
                           help="print only the generated in-pod script and exit")
    behaviour.add_argument("--batch", action="store_true",
                           help="do not prompt for confirmation")
    behaviour.add_argument("-q", "--quiet", action="store_true")
    behaviour.add_argument("-v", "--verbose", action="store_true")
    behaviour.add_argument("--catalog", metavar="PATH",
                           help="use an alternative plugin catalog")
    behaviour.add_argument("--oc", metavar="PATH", default="oc",
                           help="path to the oc binary (default: found on PATH)")
    return parser


def validate(args):
    if args.since and args.since_time:
        raise UserError("--since and --since-time are mutually exclusive")
    if args.since and not _DURATION.match(args.since):
        raise UserError(
            f"--since {args.since!r} is not a duration; expected e.g. 30s, 5m, 2h"
        )
    if args.since_time and not _RFC3339.match(args.since_time):
        raise UserError(
            f"--since-time {args.since_time!r} is not RFC3339; expected e.g. "
            "2026-10-16T09:00:00Z"
        )
    if len(args.image) > 1:
        raise UserError(
            "only one --image is supported: the catalog describes a single "
            "image's plugin layout. Run must-gather-sos once per image."
        )
    if args.volume_percentage is not None and not 1 <= args.volume_percentage <= 100:
        raise UserError("--volume-percentage must be between 1 and 100")

    reduce_logs = split_list(args.reduce_logs)
    for item in reduce_logs:
        if item not in REDUCE_LOGS_CHOICES:
            raise UserError(
                f"--reduce-logs {item!r} is not one of "
                f"{', '.join(REDUCE_LOGS_CHOICES)}"
            )
    return reduce_logs


def _terminal_width():
    return min(shutil.get_terminal_size((100, 24)).columns, 110)


def list_plugins(catalog, as_json):
    if as_json:
        import json

        payload = {
            "catalog": catalog.source,
            "plugins": {
                name: {
                    "default": plugin.default,
                    "phase": plugin.phase,
                    "description": plugin.description,
                    "superset": plugin.superset,
                    "since": plugin.since,
                    "options": plugin.options,
                }
                for name, plugin in sorted(catalog.plugins.items())
            },
        }
        print(json.dumps(payload, indent=2))
        return 0

    width = _terminal_width()
    name_width = 38
    source = catalog.source

    print(f"{PROG} {__version__} -- plugin catalog "
          f"{source.get('ref', '?')} @ {(source.get('commit') or '?')[:12]}")

    def entry(name, text):
        """Print 'name  description', hanging the description if name is long."""
        body = textwrap.wrap(text, width=max(width - name_width - 2, 30)) or [""]
        if len(name) > name_width - 3:
            print(f"  {name}")  # too long to share a line
        else:
            print(f"  {name:<{name_width - 2}}{body[0]}")
            body = body[1:]
        for line in body:
            print(f"  {'':<{name_width - 2}}{line}")

    def show(title, names):
        if not names:
            return
        print(f"\n{title}")
        for name in names:
            plugin = catalog.plugins[name]
            text = plugin.description or "(no description)"
            if plugin.superset:
                text = "[superset] " + text
            if plugin.options:
                text += f"  Options: {', '.join(sorted(plugin.options))}."
            text += f"  --since: {SINCE_LABEL[plugin.since]}."
            entry(name, text)

    # Alphabetical here -- this list is for picking plugins, not for reading
    # execution order -- but keep the synthetic 'core' at the top.
    ordered = sorted(catalog.names, key=lambda n: (n != "core", n))
    show("enabled by default:", [n for n in ordered if catalog.plugins[n].default])
    show("not enabled by default (select with -e or -o):",
         [n for n in ordered if not catalog.plugins[n].default])

    options = [
        (f"{name}.{opt}", spec)
        for name, plugin in sorted(catalog.plugins.items())
        for opt, spec in sorted(plugin.options.items())
    ]
    print("\nabout --since:")
    for line in textwrap.wrap(SINCE_HELP, width=max(width - 2, 40)):
        print(f"  {line}")

    if options:
        print("\nplugin options (-k plugin.option=value):")
        for key, spec in options:
            detail = spec.get("help", "")
            if spec.get("choices"):
                detail += f" One of: {', '.join(spec['choices'])}."
            if spec.get("type") == "bool":
                detail += " on/off."
            wrapped = textwrap.wrap(detail.strip(),
                                    width=max(width - name_width - 2, 30))
            print(f"  {key:<{name_width - 2}}{wrapped[0] if wrapped else ''}")
            for line in wrapped[1:]:
                print(f"  {'':<{name_width - 2}}{line}")
    return 0


def summarise(catalog, selection, args, out=None):
    out = out or sys.stderr
    skipped = sorted(set(catalog.names) - set(selection.names))
    print(f"{PROG} {__version__}", file=out)
    print(f"  plugins ({len(selection.names)}): {', '.join(selection.names)}", file=out)
    if skipped:
        print(f"  skipping ({len(skipped)}): {', '.join(skipped)}", file=out)
    for plugin, options in sorted(selection.options.items()):
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(options.items()))
        print(f"  options: {plugin}: {rendered}", file=out)
    if args.since:
        print(f"  since: {args.since}", file=out)
    if args.since_time:
        print(f"  since-time: {args.since_time}", file=out)
    print(f"  output: {args.dest_dir}", file=out)

    warnings = list(selection.warnings)
    if args.since or args.since_time:
        passed = [n for n in selection.names
                  if catalog.plugins[n].since == "passed-through"]
        inert = [n for n in selection.names
                 if catalog.plugins[n].since == "none"]
        if passed:
            warnings.append(
                f"{', '.join(passed)} forwards --since to 'oc adm node-logs "
                "--path=FILE', but oc only applies it to journal queries and "
                "the kubelet does not time-filter files, so whole files are "
                "collected regardless. Same under plain oc adm must-gather."
            )
        if inert:
            warnings.append(
                f"--since has no effect on {', '.join(inert)}; "
                f"{'they collect' if len(inert) > 1 else 'it collects'} "
                "point-in-time state or whole files."
            )
    for warning in warnings:
        print(f"  warning: {warning}", file=out)


def confirm(out=None):
    out = out or sys.stderr
    if not sys.stdin.isatty():
        return True
    try:
        print("\nPress Enter to start collection, or Ctrl-C to quit.",
              file=out, end=" ")
        out.flush()
        input()
    except (KeyboardInterrupt, EOFError):
        print("\naborted", file=out)
        return False
    return True


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except UserError as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"\n{PROG}: interrupted", file=sys.stderr)
        return 130


def _run(args):
    catalog = Catalog.load(args.catalog)

    if args.list_plugin_names:
        print("\n".join(sorted(catalog.names)))
        return 0

    if args.list_plugin_options:
        print("\n".join(sorted(
            f"{name}.{opt}"
            for name, plugin in catalog.plugins.items()
            for opt in plugin.options
        )))
        return 0

    if args.list_plugins:
        return list_plugins(catalog, args.format == "json")

    reduce_logs = validate(args)
    selection = build_selection(
        catalog,
        only=args.only_plugins,
        skip=args.skip_plugins,
        enable=args.enable_plugins,
        all_plugins=args.all_plugins,
        raw_options=args.plugin_options,
    )

    if not args.dest_dir:
        args.dest_dir = runner.default_dest_dir()

    text = script.generate(catalog, selection, reduce_logs=reduce_logs)
    payload = runner.encode_payload(text)

    if args.print_script:
        print(text, end="")
        return 0

    if args.print_command:
        # Shell-quoted so it can be pasted and run verbatim.
        oc_argv = runner.build_oc_args(args, payload, oc=args.oc)
        print(runner.quote_command(oc_argv))
        return 0

    if args.dry_run:
        oc_argv = runner.build_oc_args(args, payload, oc=args.oc)
        summarise(catalog, selection, args, out=sys.stdout)
        print("\n--- in-pod script ---")
        print(text, end="")
        print("\n--- oc command ---")
        print(runner.quote_command(oc_argv[:-1]))
        print(f"  {oc_argv[-1][:72]}...  ({len(oc_argv[-1])} chars, one argv element)")
        print("\n(use --print-command for the full, paste-ready command)")
        return 0

    oc = runner.which_oc(args.oc)
    runner.check_cluster(oc)
    oc_argv = runner.build_oc_args(args, payload, oc=oc)

    if not args.quiet:
        summarise(catalog, selection, args)
    if not args.batch and not confirm():
        return 130

    started = manifest_mod.utcnow()
    exit_code = runner.execute(oc_argv)
    finished = manifest_mod.utcnow()

    output_dir = runner.resolve_output_dir(args.dest_dir) or args.dest_dir

    if not args.no_manifest and os.path.isdir(output_dir):
        doc = manifest_mod.build(
            catalog, selection, args, started, finished, exit_code, output_dir, oc
        )
        for path in manifest_mod.write(doc, text, output_dir):
            if args.verbose:
                print(f"wrote {path}", file=sys.stderr)
        _report_plugin_failures(doc, quiet=args.quiet)

    if args.archive is not None and os.path.isdir(args.dest_dir):
        path, size = archive_mod.create(
            args.dest_dir, args.case_id, finished, compression=args.archive
        )
        print(f"archive: {path} ({archive_mod.human_size(size)})")

    if not args.quiet:
        duration = int((finished - started).total_seconds())
        print(f"collected into {args.dest_dir} in {duration}s "
              f"(oc exit code {exit_code})", file=sys.stderr)
    return exit_code


def _report_plugin_failures(doc, quiet=False):
    results = doc["results"]["plugins"]
    failures = [p for p in results if p["rc"] != 0]
    if not failures or quiet:
        return
    # A non-zero rc here is routine, not a failed collection: oc adm inspect
    # exits 1 when any optional resource is absent, and most gather_* scripts
    # no-op loudly when their operator is not installed. Upstream's gather
    # discards these statuses entirely; we surface them, so say what they mean.
    print(f"\n{len(failures)} of {len(results)} collection steps exited "
          "non-zero. This is common and usually harmless -- oc adm inspect "
          "exits 1 when an optional resource is absent. Data already "
          "collected is still written.", file=sys.stderr)
    for item in failures:
        print(f"  {item['name']}: rc={item['rc']} after {item['seconds']}s",
              file=sys.stderr)
    print("  see gather.logs in the output for the reason", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
