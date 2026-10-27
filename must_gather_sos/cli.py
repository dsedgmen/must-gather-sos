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
from . import catalog as catalog_mod
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
    selection.add_argument("--list-catalogs", action="store_true",
                           help="list the built-in plugin catalogs and the "
                                "images they describe, and exit")
    selection.add_argument("-o", "--only-plugins", metavar="LIST", default="",
                           help="run only these plugins (comma separated); "
                                "where the catalog has a 'core' plugin, "
                                "include it to keep the base oc adm inspect "
                                "pass")
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
    collection.add_argument("--jobs", metavar="[IMAGE=]N", action="append",
                            default=[],
                            help="most collection scripts to run at once "
                                 "inside the pod; 0 (the default) means "
                                 "unlimited, as upstream's gather does. Use "
                                 "it when a cluster cannot take two dozen "
                                 "inspects at the same time. Prefix with an "
                                 "image to cap just that one")

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
    behaviour.add_argument("--no-filter", action="store_true",
                           help="show oc's output in full. By default the "
                                "disk-usage line it prints every five seconds "
                                "and the per-file listing from the copy step "
                                "are hidden, since neither says anything about "
                                "the collection; the unfiltered stream is kept "
                                "in the output directory either way")
    behaviour.add_argument("--batch", action="store_true",
                           help="do not prompt for confirmation")
    behaviour.add_argument("-q", "--quiet", action="store_true")
    behaviour.add_argument("-v", "--verbose", action="store_true")
    behaviour.add_argument("--catalog", metavar="[IMAGE=]NAME|PATH",
                           action="append", default=[],
                           help="use a different plugin catalog: a built-in "
                                "name (see --list-catalogs) or a path to one "
                                "of your own. With several images, prefix it "
                                "with the image it describes. Repeatable")
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
    if args.volume_percentage is not None and not 1 <= args.volume_percentage <= 100:
        raise UserError("--volume-percentage must be between 1 and 100")
    parse_jobs(getattr(args, "jobs", []))

    seen = set()
    for image in args.image:
        if image in seen:
            # Two pods with the same resolved digest land in the same
            # output directory, and oc opens gather.logs with O_TRUNC, so
            # the second silently overwrites the first.
            raise UserError(f"--image {image} given twice; each image can "
                            "only be collected once per run")
        seen.add(image)

    reduce_logs = split_list(args.reduce_logs)
    for item in reduce_logs:
        if item not in REDUCE_LOGS_CHOICES:
            raise UserError(
                f"--reduce-logs {item!r} is not one of "
                f"{', '.join(REDUCE_LOGS_CHOICES)}"
            )
    return reduce_logs


def _split_pin(value):
    """Split an '[IMAGE=]VALUE' argument.

    Only on the first '=', and only when what precedes it looks like an
    image reference rather than part of a path, so --catalog ./a=b.json
    still means a filename.
    """
    image, sep, rest = value.partition("=")
    if sep and ("/" in image or "." in image or ":" in image):
        return image, rest
    return None, value


def parse_jobs(values):
    """Turn repeated --jobs into (default, {image: cap})."""
    default, per_image = 0, {}
    for value in values:
        image, raw = _split_pin(value)
        try:
            count = int(raw)
        except ValueError:
            raise UserError(f"--jobs {value!r} is not a number") from None
        if count < 0:
            raise UserError("--jobs must be 0 (unlimited) or more")
        if image:
            per_image[image] = count
        else:
            default = count
    return default, per_image


def build_targets(args, reduce_logs):
    """Pair every image in this run with the catalog that describes it.

    oc sends one command to every --image pod, so all of these end up in a
    single generated script that works out at runtime which it is in. What
    is decided here is only which catalogs that script needs to carry.
    """
    default_jobs, image_jobs = parse_jobs(args.jobs)

    pinned, loose = {}, []
    for value in args.catalog:
        image, path = _split_pin(value)
        if image:
            pinned[image] = path
        else:
            loose.append(path)
    if len(loose) > 1:
        raise UserError(
            "--catalog given more than once without naming an image; say "
            "which each describes, as --catalog IMAGE=CATALOG"
        )

    images = list(args.image)
    if not images:
        # No --image: oc runs the cluster's own payload image. One loose
        # --catalog may still redirect which catalog describes it, which is
        # how the better-gather fork has always been selected.
        catalog = Catalog.load(loose[0]) if loose else Catalog.load()
        return [_target(catalog, None, args, reduce_logs,
                        default_jobs, image_jobs)]

    if loose and len(images) > 1:
        raise UserError(
            "--catalog without an image is ambiguous with several --image "
            "flags; write --catalog IMAGE=CATALOG for each"
        )

    catalogs = []
    for image in images:
        if image in pinned:
            catalogs.append((image, Catalog.load(pinned[image])))
        elif loose:
            catalogs.append((image, Catalog.load(loose[0])))
        else:
            catalogs.append((image, _catalog_for_image(image)))

    # Selection flags are global: a name has to exist in *some* selected
    # catalog, and simply does not apply to the images that lack it.
    union = set()
    for _, catalog in catalogs:
        union |= set(catalog.names)

    targets = []
    for image, catalog in catalogs:
        target = _target(catalog, image, args, reduce_logs, default_jobs,
                         image_jobs, known_elsewhere=union)
        if not target.selection.names:
            print(f"{PROG}: none of the selected plugins exist in "
                  f"{catalog.name}; not collecting from {image}",
                  file=sys.stderr)
            continue
        targets.append(target)

    if not targets:
        raise UserError("no plugins selected")

    by_profile = {}
    for target in targets:
        other = by_profile.get(target.catalog.profile.id)
        if other:
            # The pod picks its branch by probing the filesystem, so two
            # catalogs describing the same image family cannot be told
            # apart once the script is running.
            raise UserError(
                f"{target.catalog.name} and {other.catalog.name} describe "
                f"the same kind of image ({target.catalog.profile.id}), so "
                "a single run cannot tell them apart. Run them separately."
            )
        by_profile[target.catalog.profile.id] = target
    return targets


def _catalog_for_image(image):
    matches = [
        name for name, path in catalog_mod.registry().items()
        if Catalog.load(path).image.matches(image)
    ]
    if len(matches) == 1:
        return Catalog.load(matches[0])
    if not matches:
        raise UserError(
            f"no built-in catalog recognises the image {image!r}. Say which "
            f"describes it with --catalog {image}=NAME, or see "
            "--list-catalogs."
        )
    raise UserError(
        f"{', '.join(sorted(matches))} all claim the image {image!r}; "
        f"pick one with --catalog {image}=NAME"
    )


def _target(catalog, image, args, reduce_logs, default_jobs, image_jobs,
            known_elsewhere=()):
    selection = build_selection(
        catalog,
        only=args.only_plugins,
        skip=args.skip_plugins,
        enable=args.enable_plugins,
        all_plugins=args.all_plugins,
        raw_options=args.plugin_options,
        known_elsewhere=known_elsewhere,
    )
    jobs = image_jobs.get(image, default_jobs)
    return script.Target(catalog, selection, reduce_logs, jobs, image)


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


def summarise(targets, args, out=None):
    """What is about to run. One stanza per image when there is more than one."""
    out = out or sys.stderr
    print(f"{PROG} {__version__}", file=out)

    for target in targets:
        if len(targets) > 1:
            image = target.image_ref or "the cluster's own must-gather image"
            print(f"  image: {image}  (catalog {target.catalog.name})", file=out)
        _summarise_target(target, args, out,
                          indent="    " if len(targets) > 1 else "  ")

    if args.since:
        print(f"  since: {args.since}", file=out)
    if args.since_time:
        print(f"  since-time: {args.since_time}", file=out)
    print(f"  output: {args.dest_dir}", file=out)


def _summarise_target(target, args, out, indent="  "):
    catalog, selection = target.catalog, target.selection
    skipped = sorted(set(catalog.names) - set(selection.names))
    print(f"{indent}plugins ({len(selection.names)}): "
          f"{', '.join(selection.names)}", file=out)
    if skipped:
        print(f"{indent}skipping ({len(skipped)}): {', '.join(skipped)}",
              file=out)
    for plugin, options in sorted(selection.options.items()):
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(options.items()))
        print(f"{indent}options: {plugin}: {rendered}", file=out)
    if target.jobs:
        print(f"{indent}jobs: {target.jobs} collection script(s) at a time",
              file=out)
    for warning in target_warnings(target, args):
        print(f"{indent}warning: {warning}", file=out)


def target_warnings(target, args):
    """Everything worth saying about one image before it runs."""
    catalog, selection = target.catalog, target.selection
    warnings = list(selection.warnings)

    if args.reduce_logs and not catalog.profile.reduce_logs:
        warnings.append(
            f"--reduce-logs has no effect on the {catalog.name} image: it has "
            "no common.sh to read REDUCE_LOGS, so nothing is skipped or "
            "compressed."
        )

    if not (args.since or args.since_time):
        return warnings

    if not catalog.profile.since:
        # One honest line, rather than naming every plugin: oc still sets
        # MUST_GATHER_SINCE on the pod, so the flag looks like it worked.
        warnings.append(
            f"--since has no effect on the {catalog.name} image. oc still "
            "sets MUST_GATHER_SINCE on the pod, but these collection scripts "
            "never read it -- there is no common.sh here -- so everything is "
            "collected whole."
        )
        return warnings

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
    return warnings


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


def list_catalogs(out=None):
    out = out or sys.stdout
    rows = catalog_mod.describe_catalogs()
    width = max((len(r["name"]) for r in rows), default=4) + 2
    for row in rows:
        where = ", ".join(row["images"]) or "no image pattern; select by name"
        if row["payload"]:
            where = "the cluster's own payload image; " + where
        print(f"  {row['name']:<{width}}{row['plugins']} plugins, "
              f"profile {row['profile']}", file=out)
        print(f"  {'':<{width}}{where}", file=out)
    return 0


def _run(args):
    if args.list_catalogs:
        return list_catalogs()

    # The listing modes describe one catalog; a pinned --catalog IMAGE=NAME
    # has no meaning there, so take the plain one.
    catalog = Catalog.load(_split_pin(args.catalog[0])[1]
                           if args.catalog else None)

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
    targets = build_targets(args, reduce_logs)

    if not args.dest_dir:
        args.dest_dir = runner.default_dest_dir()

    text = script.generate_targets(targets)
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
        summarise(targets, args, out=sys.stdout)
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
        summarise(targets, args)
    if not args.batch and not confirm():
        return 130

    started = manifest_mod.utcnow()
    os.makedirs(args.dest_dir, exist_ok=True)
    raw_log = os.path.join(args.dest_dir, "oc-output.log")
    exit_code = runner.execute(oc_argv, filter_noise=not args.no_filter,
                               raw_log_path=raw_log)
    finished = manifest_mod.utcnow()

    # oc gives each image its own directory, named after the resolved
    # digest. Which image each one is comes from the marker the pod wrote,
    # not from the name, which we cannot predict.
    output_dirs = runner.resolve_output_dirs(args.dest_dir) or [args.dest_dir]
    by_profile = {t.catalog.profile.id: t for t in targets}
    docs = []

    for output_dir in output_dirs:
        if not os.path.isdir(output_dir):
            continue
        target = by_profile.get(manifest_mod.read_profile(output_dir)) \
            or (targets[0] if len(targets) == 1 else None)
        if args.no_manifest or target is None:
            continue
        doc = manifest_mod.build(target, args, started, finished, exit_code,
                                 output_dir, oc)
        docs.append((target, output_dir, doc))
        for path in manifest_mod.write(doc, text, output_dir):
            if args.verbose:
                print(f"wrote {path}", file=sys.stderr)

    for target, output_dir, doc in docs:
        label = target.catalog.name if len(docs) > 1 else None
        _report_plugin_failures(doc, quiet=args.quiet, label=label)

    if len(docs) > 1 and not args.no_manifest:
        run_doc = manifest_mod.build_run(
            [(t.catalog, d, doc) for t, d, doc in docs],
            args, started, finished, exit_code,
        )
        manifest_mod.write_run(run_doc, args.dest_dir)

    if args.archive is not None and os.path.isdir(args.dest_dir):
        path, size = archive_mod.create(
            args.dest_dir, args.case_id, finished, compression=args.archive
        )
        print(f"archive: {path} ({archive_mod.human_size(size)})")

    if not args.quiet:
        duration = int((finished - started).total_seconds())
        print(f"collected into {args.dest_dir} in {duration}s "
              f"(oc exit code {exit_code})", file=sys.stderr)
        progress = os.path.join(output_dir, script.PROGRESS_LOG_NAME)
        if os.path.exists(progress):
            print(f"progress, without oc's chatter in it: {progress}",
                  file=sys.stderr)
    return exit_code


def _report_plugin_failures(doc, quiet=False, out=None, label=None):
    """Repeat the in-pod verdict locally, where it cannot scroll away.

    The pod prints its own summary, but it arrives in the middle of oc's copy
    step and is the last thing anybody scrolls back for. This is the same
    tally, after the copy, next to the output directory it refers to.
    """
    out = out or sys.stderr
    results = doc["results"]["plugins"]
    if not results or quiet:
        return
    counts = doc["results"].get("tally") or manifest_mod.tally(results)
    summary = (f"{counts['ok']} collected, {counts['no-op']} found nothing, "
               f"{counts['failed']} failed")
    if counts["missing"]:
        summary += f", {counts['missing']} not in the image"
    where = f" [{label}]" if label else ""
    print(f"\n{len(results)} collection task(s){where}: {summary}.", file=out)

    failures = [p for p in results if p["state"] == "failed"]
    if failures:
        # A non-zero rc is routine, not a failed collection: oc adm inspect
        # exits 1 when any optional resource is absent. Upstream's gather
        # discards these statuses entirely; we surface them, so say what
        # they mean rather than leaving them to be read as breakage.
        for item in failures:
            print(f"  failed: {item['name']} rc={item['rc']} "
                  f"after {item['seconds']}s", file=out)
        print("  A non-zero status is common and often harmless -- oc adm "
              "inspect exits 1 when an optional resource is absent. Data "
              "already collected is still written.", file=out)

    missing = [p for p in results if p["state"] == "missing"]
    if missing:
        print(f"  not present in this image: "
              f"{', '.join(p['name'] for p in missing)}", file=out)
        print("  The plugin catalog describes a different release from the "
              "image that ran. Pass the matching --image, or skip them "
              "with -n.", file=out)

    if failures or missing:
        print(f"  per-task output: {script.TASK_LOG_DIR_NAME}/ in the output "
              "directory", file=out)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
