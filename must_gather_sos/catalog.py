"""Plugin catalog: loading, selection algebra and ``-k`` option binding.

The catalog is a JSON document generated from openshift/must-gather by
``tools/sync_catalog.py``.  See that file for the schema.  On top of the
``gather_*`` scripts it describes, this module adds one synthetic plugin,
``core``, standing for the three base ``oc adm inspect`` calls that upstream's
``gather`` performs inline.
"""

from __future__ import annotations

import difflib
import json
import os

from . import UserError

CORE = "core"

#: Why --since is not universal, in one paragraph, used by --list-plugins and
#: by the warning printed when a selection contains plugins that ignore it.
SINCE_HELP = (
    "--since / --since-time take effect in two places: as --since on "
    "'oc adm inspect' (trims pod logs), and as --since on 'oc adm node-logs' "
    "journal queries (trims host service logs). Elsewhere the window is "
    "accepted but has no effect. Note the distinction: several upstream "
    "scripts -- audit_logs, kas_startup_termination_logs, windows_node_logs "
    "-- do pass the since arguments on to 'oc adm node-logs --path=FILE', so "
    "the plugin supports it; it is the layers underneath that discard it. oc "
    "attaches the since parameter only when the path is 'journal' "
    "(pkg/cli/admin/node/logs.go), and the kubelet does not time-filter plain "
    "file serving even when asked directly. Plugins that copy files out of "
    "pods or query Prometheus have no window either. All of this is identical "
    "under a plain 'oc adm must-gather --since'."
)

SINCE_LABEL = {
    "both": "pod logs + host journals",
    "pod-logs": "pod logs only",
    "journal": "host journals only",
    "files": "file selection, applied by the collection script itself",
    "passed-through": "passed to oc, but oc/kubelet ignore it for files",
    "none": "no time window",
}

_TRUE = {"on", "true", "yes", "1", "enable", "enabled"}
_FALSE = {"off", "false", "no", "0", "disable", "disabled"}

_DEFAULT_CATALOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "openshift.json")

#: Catalog schema versions this build understands. Version 2 added the image
#: identity and the runtime profile, and made ``core`` optional; a version 1
#: document still loads, and is given the OpenShift profile below. The gate
#: matters because catalogs are user-supplyable with ``--catalog``: a v2
#: document handed to a v1 build would otherwise die on a missing ``core``
#: key rather than saying which part it did not understand.
SUPPORTED_VERSIONS = (1, 2)


class Profile:
    """How to prepare one family of must-gather image before running plugins.

    This is what ``script.py`` used to hardcode: which library to source, what
    to call before collecting, where the collection directory is, and which of
    ``--since`` / ``--reduce-logs`` the image can actually honour.

    A profile describes an image *family*, not a catalog, and several catalogs
    may share one: ``openshift.json`` and ``better-gather.json`` describe the
    same image layout and differ only in how far ``--since`` reaches.

    The defaults are the OpenShift ones, so a version 1 catalog -- which has no
    ``profile`` key at all -- behaves exactly as it did before profiles existed.
    """

    def __init__(self, spec=None):
        spec = spec or {}
        self.id = spec.get("id", "openshift")
        # A shell test that is true inside this image and false in the others,
        # used to dispatch when one script is sent to several images.
        self.detect = spec.get("detect")
        self.base_path = spec.get("base_path", "/must-gather")
        self.source = list(spec.get("source", ["/usr/bin/common.sh"]))
        self.setup = list(spec.get("setup", ["get_log_collection_args"]))

        # An explicit null command means "this image has no /usr/bin/version";
        # an absent key means "the usual one".
        version = spec.get("version") or {}
        self.version_banner = version.get("banner", "openshift/must-gather")
        self.version_command = version.get(
            "command", ". /usr/bin/version && version"
        )

        # Whether the image reads MUST_GATHER_SINCE / REDUCE_LOGS at all. oc
        # sets them on every pod regardless, so an image that ignores them
        # makes the flags misleading rather than merely inert, and the CLI
        # needs to say so once instead of per plugin.
        self.since = bool(spec.get("since", True))
        self.reduce_logs = bool(spec.get("reduce_logs", True))

        # Overrides TASK_NOOP_PATTERN, which encodes how upstream's scripts
        # phrase "the thing I collect is not installed". Another product
        # words it differently, and the ok/no-op split depends on matching it.
        self.noop_pattern = spec.get("noop_pattern")

    @property
    def plugin_log(self):
        """Where the in-pod task library writes its results, in this image."""
        return f"{self.base_path}/must-gather-sos-plugins.log"

    def __repr__(self):
        return f"<Profile {self.id}>"


class Image:
    """Which must-gather image(s) a catalog describes."""

    def __init__(self, spec=None):
        spec = spec or {}
        self.default = spec.get("default")
        self.match = [m.lower() for m in spec.get("match", [])]
        # True for the catalog describing the cluster payload's own image,
        # the one oc runs when --image is absent.
        self.payload = bool(spec.get("payload", False))

    def matches(self, ref):
        """Does *ref* name an image this catalog describes?

        Substrings of the repository path, not regexes: a malformed pattern
        in a data file is a traceback a long way from whoever wrote it. The
        host and the tag or digest are dropped first, so a mirrored registry
        and a pinned digest both still match.
        """
        if not ref:
            return False
        path = ref.split("@", 1)[0]
        head, _, tail = path.partition("/")
        if "." in head or ":" in head or head == "localhost":
            path = tail or path          # drop a registry host
        path = path.rsplit(":", 1)[0]    # drop the tag
        path = path.lower()
        return any(pattern in path for pattern in self.match)


class Plugin:
    """One collectable unit: a gather_* script, or the synthetic 'core'."""

    def __init__(self, name, spec):
        self.name = name
        self.script = spec.get("script")
        self.default = bool(spec.get("default", False))
        self.phase = spec.get("phase", "parallel")
        self.order = spec.get("order", 0)
        self.description = spec.get("description", "")
        self.args = list(spec.get("args", []))
        self.env = dict(spec.get("env", {}))
        self.env_if_core = dict(spec.get("env_if_core", {}))
        self.options = spec.get("options", {})
        self.superset = bool(spec.get("superset", False))
        self.requires_option = bool(spec.get("requires_option", False))
        # How far --since actually reaches:
        # pod-logs | journal | both | passed-through | none. See SINCE_HELP.
        self.since = spec.get("since", "none")

    @property
    def honours_since(self):
        """True only where the window changes what is collected.

        'passed-through' is deliberately False: the script forwards --since to
        'oc adm node-logs --path=FILE', but oc and the kubelet both ignore it
        there, so the collection is identical with or without it.
        """
        return self.since in ("pod-logs", "journal", "both", "files")

    @property
    def is_core(self):
        return self.name == CORE

    def __repr__(self):
        return f"<Plugin {self.name} default={self.default} phase={self.phase}>"


class Catalog:
    def __init__(self, doc, path=None):
        self.path = path
        self.version = doc.get("catalog_version")
        if self.version not in SUPPORTED_VERSIONS:
            raise UserError(
                f"unsupported catalog_version {self.version!r} in {path}; "
                f"this build understands "
                f"{', '.join(str(v) for v in SUPPORTED_VERSIONS)}"
            )
        self.source = doc.get("source", {})
        self.profile = Profile(doc.get("profile"))
        self.image = Image(doc.get("image"))
        self.name = doc.get("name") or _name_from_path(path)

        # Optional since version 2: not every must-gather image performs an
        # inline 'oc adm inspect' pass the way upstream's gather does.
        self.core_resources = doc.get("core")

        self.plugins = {}
        if self.core_resources:
            if "get_log_collection_args" not in self.profile.setup:
                # The core template expands ${log_collection_args} and
                # ${rotated_pod_logs_arg}, which only common.sh defines.
                # Without it the inspect calls would silently run unfiltered.
                raise UserError(
                    f"catalog {self.name!r} declares 'core' but its profile "
                    "does not call get_log_collection_args; the base inspect "
                    "pass needs common.sh"
                )
            self.plugins[CORE] = Plugin(CORE, {
                "default": True,
                "phase": "core",
                "order": -1,
                "description": self.core_resources.get("description", ""),
                "since": self.core_resources.get("since", "pod-logs"),
            })

        for name, spec in doc["plugins"].items():
            if name in self.plugins or name == CORE:
                raise UserError(f"catalog defines reserved plugin name {name!r}")
            self.plugins[name] = Plugin(name, spec)

    @classmethod
    def load(cls, path=None):
        path = resolve_catalog(path) if path else _default_catalog()
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            raise UserError(f"catalog not found: {path}") from None
        except json.JSONDecodeError as exc:
            raise UserError(f"catalog {path} is not valid JSON: {exc}") from None
        return cls(doc, path)

    @property
    def names(self):
        return sorted(self.plugins)

    @property
    def defaults(self):
        return {n for n, p in self.plugins.items() if p.default}

    def get(self, name):
        try:
            return self.plugins[name]
        except KeyError:
            raise UserError(_unknown_plugin_message(name, self.names)) from None

    def sort_key(self, name):
        """Order plugins the way upstream's gather does, core first."""
        plugin = self.plugins[name]
        return (plugin.order, plugin.name)


_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def registry():
    """The catalogs we ship, as {name: path}.

    By filename, not by reading each one: this is used to resolve
    ``--catalog NAME`` and to list the choices, both of which must be cheap
    and must not fail because some unrelated catalog is malformed.
    """
    try:
        names = sorted(os.listdir(_DATA_DIR))
    except OSError:  # pragma: no cover - the package would be broken
        return {}
    return {
        os.path.splitext(name)[0]: os.path.join(_DATA_DIR, name)
        for name in names if name.endswith(".json")
    }


def resolve_catalog(value):
    """Turn a --catalog argument into a path.

    A bare name from the registry first, then a filesystem path, so
    ``--catalog acm`` works without anyone knowing where the data directory
    is, and ``--catalog ./mine.json`` still works for one you wrote.
    """
    known = registry()
    if value in known:
        return known[value]
    if os.path.sep in value or value.endswith(".json"):
        return value
    raise UserError(
        f"unknown catalog {value!r}. Built in: {', '.join(sorted(known))}. "
        "Pass a path to use one of your own."
    )


def _default_catalog():
    """The catalog for the image oc runs when --image is absent."""
    for path in registry().values():
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        if (doc.get("image") or {}).get("payload"):
            return path
    return _DEFAULT_CATALOG


def describe_catalogs():
    """One summary row per shipped catalog, for --list-catalogs."""
    rows = []
    for name, path in registry().items():
        try:
            catalog = Catalog.load(path)
        except UserError:
            continue
        rows.append({
            "name": name,
            "profile": catalog.profile.id,
            "images": list(catalog.image.match),
            "default_image": catalog.image.default,
            "payload": catalog.image.payload,
            "plugins": len(catalog.plugins),
            "ref": catalog.source.get("ref"),
        })
    return rows


def _name_from_path(path):
    """A catalog with no name of its own is known by its filename."""
    if not path:
        return "catalog"
    return os.path.splitext(os.path.basename(path))[0]


def _unknown_plugin_message(name, known):
    near = difflib.get_close_matches(name, known, n=3, cutoff=0.6)
    msg = f"unknown plugin {name!r}"
    if near:
        msg += f" (did you mean: {', '.join(near)}?)"
    msg += "\nRun 'must-gather-sos --list-plugins' to see the full list."
    return msg


def split_list(value):
    """Split a comma and/or whitespace separated CLI list."""
    if not value:
        return []
    return [item for item in value.replace(",", " ").split() if item]


class Selection:
    """The resolved set of plugins to run, with their bound options."""

    def __init__(self, catalog, names, options, warnings):
        self.catalog = catalog
        self.names = names              # ordered list
        self.options = options          # {plugin: {option: value}}
        self.warnings = warnings

    @property
    def core_selected(self):
        return CORE in self.names

    def plugins(self, phase):
        return [self.catalog.plugins[n] for n in self.names
                if self.catalog.plugins[n].phase == phase]

    def invocation(self, plugin):
        """Return (env, argv) for one plugin, with -k options applied."""
        env = dict(plugin.env)
        if plugin.env_if_core and self.core_selected:
            env.update(plugin.env_if_core)

        argv = [plugin.script]
        positional = list(plugin.args)
        trailing = []

        for opt_name, value in sorted(self.options.get(plugin.name, {}).items()):
            spec = plugin.options[opt_name]
            kind = spec.get("kind", "opt")
            if kind == "env":
                env[spec["env"]] = value
            elif kind == "flag":
                if value:
                    trailing.append(spec["flag"])
            elif kind == "opt":
                for item in value if isinstance(value, list) else [value]:
                    trailing.append(f"{spec['flag']}={item}")
            elif kind == "positional":
                index = spec["index"]
                while len(positional) <= index:
                    positional.append("")
                if isinstance(value, list):
                    value = spec.get("separator", " ").join(value)
                positional[index] = value
            else:  # pragma: no cover - guarded by catalog validation
                raise UserError(f"catalog: unknown option kind {kind!r} for {plugin.name}.{opt_name}")

        # Padding only ever inserts "" *before* an index the user set, so an
        # empty slot here means a later positional was given without an
        # earlier one -- which would silently shift the script's arguments.
        if "" in positional:
            gap = positional.index("")
            raise UserError(
                f"{plugin.name}: positional argument {gap} is unset but a later "
                f"one was given; set it too"
            )
        return env, argv + positional + trailing


def resolve(catalog, only=None, skip=None, enable=None, all_plugins=False,
            configured=(), known_elsewhere=()):
    """Apply sos selection semantics and return (names, warnings).

    *configured* is the set of plugins the user gave ``-k`` options for; it only
    affects ``--all-plugins``, which otherwise cannot include plugins that
    refuse to run without one.

    *known_elsewhere* names plugins some other catalog in the same run has.
    Selection flags are global across images, so a name this catalog does not
    know is not a typo when another one does -- it just does not apply here.
    """
    only = split_list(only) if isinstance(only, str) else list(only or [])
    skip = split_list(skip) if isinstance(skip, str) else list(skip or [])
    enable = split_list(enable) if isinstance(enable, str) else list(enable or [])

    known_elsewhere = set(known_elsewhere)
    for name in only + skip + enable:
        if name not in known_elsewhere:
            catalog.get(name)  # raises UserError on unknown names

    # Drop the names another image's catalog owns. Remember whether --only
    # was asked for before filtering: "--only, and none of them are here"
    # must select nothing, not fall through to the defaults.
    only_requested = bool(only)
    only = [n for n in only if n in catalog.plugins]
    skip = [n for n in skip if n in catalog.plugins]
    enable = [n for n in enable if n in catalog.plugins]
    if only_requested and not only:
        return [], []

    if all_plugins and only:
        raise UserError("--all-plugins and --only-plugins are mutually exclusive")

    warnings = []

    if all_plugins:
        selected = set(catalog.names) - set(skip)
        # Two kinds of plugin can't sensibly be swept up by "run everything":
        # ones that refuse to start without a -k option, and ones that re-run
        # the entire default gather. Leave them out and say so.
        reasons = {}
        for name in sorted(selected):
            plugin = catalog.plugins[name]
            if plugin.superset:
                reasons[name] = "re-runs the whole default gather"
            elif plugin.requires_option and name not in configured:
                reasons[name] = f"needs an option, e.g. -k {name}.{sorted(plugin.options)[0]}=..."
        if reasons:
            selected -= set(reasons)
            detail = "; ".join(f"{n} ({why})" for n, why in reasons.items())
            warnings.append(
                f"--all-plugins left out {detail}. Add them explicitly with -e."
            )
    elif only:
        if skip:
            raise UserError("--skip-plugins has no meaning with --only-plugins")
        if enable:
            raise UserError("--enable-plugins has no meaning with --only-plugins; "
                            "list the plugin in --only-plugins instead")
        selected = set(only)
    else:
        selected = (catalog.defaults - set(skip)) | set(enable)

    if not selected:
        if known_elsewhere:
            # Nothing here matched, but another image's catalog may well
            # have. The caller drops this image and says so.
            return [], warnings
        raise UserError("no plugins selected")

    supersets = sorted(n for n in selected if catalog.plugins[n].superset)
    if supersets:
        joined = ", ".join(supersets)
        if skip:
            raise UserError(
                f"{joined} re-runs the complete default gather internally, so "
                f"--skip-plugins would have no effect. Drop one or the other."
            )
        if len(selected) > 1:
            warnings.append(
                f"{joined} re-runs the complete default gather internally; the "
                f"other selected plugins will largely be collected twice."
            )

    return sorted(selected, key=catalog.sort_key), warnings


def parse_options(catalog, raw_options, known_elsewhere=()):
    """Parse ``-k plugin.option[=value]`` strings into {plugin: {opt: value}}."""
    parsed = {}
    for raw in raw_options or []:
        spec_part, sep, value = raw.partition("=")
        if "." not in spec_part:
            raise UserError(
                f"bad plugin option {raw!r}: expected 'plugin.option=value'"
            )
        plugin_name, _, opt_name = spec_part.partition(".")
        if plugin_name not in catalog.plugins and plugin_name in known_elsewhere:
            continue  # another image's plugin; not ours to configure
        plugin = catalog.get(plugin_name)

        if opt_name not in plugin.options:
            known = sorted(plugin.options)
            if not known:
                raise UserError(f"plugin {plugin_name!r} takes no options")
            near = difflib.get_close_matches(opt_name, known, n=2, cutoff=0.5)
            hint = f" (did you mean: {', '.join(near)}?)" if near else ""
            raise UserError(
                f"unknown option {opt_name!r} for plugin {plugin_name!r}{hint}. "
                f"Known options: {', '.join(known)}"
            )

        opt_spec = plugin.options[opt_name]
        opt_type = opt_spec.get("type", "str")
        bucket = parsed.setdefault(plugin_name, {})

        if opt_type == "bool":
            if not sep:
                value = "on"
            lowered = value.strip().lower()
            if lowered in _TRUE:
                bucket[opt_name] = True
            elif lowered in _FALSE:
                bucket[opt_name] = False
            else:
                raise UserError(
                    f"{plugin_name}.{opt_name}: expected on/off, got {value!r}"
                )
            continue

        if not sep:
            raise UserError(f"{plugin_name}.{opt_name} requires a value")

        if opt_type == "list":
            if opt_spec.get("kind", "opt") == "positional":
                # One argument built from a comma/space separated list, e.g.
                # service_logs.services=crio,kubelet -> "crio kubelet".
                items = split_list(value)
            else:
                # A repeatable flag. Take the value verbatim: Prometheus series
                # selectors and the like legitimately contain commas and
                # spaces, so splitting here would corrupt them. Repeat -k to
                # pass more than one.
                items = [value] if value else []
            if not items:
                raise UserError(f"{plugin_name}.{opt_name} requires a value")
            bucket.setdefault(opt_name, [])
            bucket[opt_name].extend(items)
            value_for_check = items
        else:
            if not value:
                raise UserError(f"{plugin_name}.{opt_name} requires a value")
            bucket[opt_name] = value
            value_for_check = [value]

        choices = opt_spec.get("choices")
        if choices:
            for item in value_for_check:
                if item not in choices:
                    raise UserError(
                        f"{plugin_name}.{opt_name}: {item!r} is not one of "
                        f"{', '.join(choices)}"
                    )
    return parsed


def build_selection(catalog, only=None, skip=None, enable=None,
                    all_plugins=False, raw_options=None, known_elsewhere=()):
    """Resolve names and options together, cross-checking the two."""
    options = parse_options(catalog, raw_options,
                            known_elsewhere=known_elsewhere)
    names, warnings = resolve(catalog, only, skip, enable, all_plugins,
                              configured=set(options),
                              known_elsewhere=known_elsewhere)
    if not names:
        return Selection(catalog, [], {}, warnings)

    unselected = sorted(set(options) - set(names))
    if unselected:
        raise UserError(
            f"option given for unselected plugin(s): {', '.join(unselected)}"
        )

    for name in names:
        plugin = catalog.plugins[name]
        if plugin.requires_option and not options.get(name):
            known = ", ".join(sorted(plugin.options))
            raise UserError(
                f"plugin {name!r} refuses to run without an option; set at least "
                f"one of: {known} (e.g. -k {name}.{sorted(plugin.options)[0]}=...)"
            )

    return Selection(catalog, names, options, warnings)
