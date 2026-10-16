# must-gather-sos

An [sos report](https://github.com/sosreport/sos)-style front end for
[`oc adm must-gather`](https://github.com/openshift/must-gather).

`oc adm must-gather` is all-or-nothing. Its default entrypoint runs a base
`oc adm inspect` pass plus ~25 `gather_*` scripts, and the only supported
alternative is running exactly one of them by hand:

```console
$ oc adm must-gather -- /usr/bin/gather_audit_logs
```

There is no way to say *"everything except monitoring and istio"*, or
*"etcd and SR-IOV, plus the base inspect"*. On a large cluster that means
waiting twenty minutes and collecting gigabytes when you wanted one subsystem.

`sos report` solved this for hosts years ago. This brings the same vocabulary
to must-gather:

```console
$ must-gather-sos --list-plugins
$ must-gather-sos -n monitoring,istio --since 2h
$ must-gather-sos -o core,etcd,sriov
$ must-gather-sos -o audit_logs -k audit_logs.force=on --since 30m
$ must-gather-sos -o core,etcd --case-id 04123456 --archive
```

Output lands in the same directory layout `oc` would have produced, with a
`manifest.json` recording what ran.

## Requirements

Python 3.9+ and `oc`. No third-party Python packages — standard library only.

## Install

```console
$ make install                 # into /usr/local
$ make install PREFIX=~/.local
$ make install PIP_FLAGS='--no-build-isolation --no-index'   # fully offline
```

This installs the CLI, the man page and the bash/zsh completions.

Or run straight from a checkout, no install needed:

```console
$ python3 -m must_gather_sos --list-plugins
```

## Plugins

Plugins are named after the upstream scripts with the `gather_` prefix
dropped — `/usr/bin/gather_audit_logs` is the `audit_logs` plugin. One extra,
synthetic plugin is added:

| Plugin | What it is |
| ------ | ---------- |
| `core` | The base `oc adm inspect` pass that upstream's `gather` does inline: cluster version, cluster operators, API services, CSRs, nodes, core namespaces, storage, image mirroring, networking, leases, APF, quotas. |

`core` is on by default. **`-o` selections that omit it get no base inspect at
all** — `-o etcd` really means only `gather_etcd`, which is usually not what
you want for a support case. Use `-o core,etcd`.

| Flag | Meaning |
| ---- | ------- |
| `-o`, `--only-plugins` | run exactly these |
| `-n`, `--skip-plugins` | run the defaults except these |
| `-e`, `--enable-plugins` | also run these non-default plugins |
| `-a`, `--all-plugins` | run everything that can run unattended |
| `-k`, `--plugin-option` | `plugin.option=value`, repeatable |

`--list-plugins` shows the full set, which are on by default, and what options
each accepts.

### Plugin options

```console
$ must-gather-sos -o audit_logs -k audit_logs.force=on
$ must-gather-sos -o service_logs -k service_logs.roles=master,worker
$ must-gather-sos -e metrics -k metrics.match='{job="etcd"}' -k metrics.min_time=1700000000000
$ must-gather-sos -e profiling_node -k profiling_node.seconds=60
```

Booleans take `on`/`off` and may be given bare (`-k audit_logs.force`).
Repeatable options like `metrics.match` take their value **verbatim**, so a
Prometheus selector containing commas and spaces stays one selector.

## `--since`

`--since` and `--since-time` are passed through to `oc`, which sets
`MUST_GATHER_SINCE` / `MUST_GATHER_SINCE_TIME` on the pod. The generated
script sources `/usr/bin/common.sh` and calls `get_log_collection_args` before
anything else, which is what turns those variables into `--since` arguments
for `oc adm inspect` and the correctly reformatted `--since` for
`oc adm node-logs`. Every plugin that supports it then honours the window.

```console
$ must-gather-sos --since 2h
$ must-gather-sos --since-time 2026-10-16T09:00:00Z
```

`--reduce-logs` exports `REDUCE_LOGS`, also read by `common.sh`:

```console
$ must-gather-sos --reduce-logs skip_rotated_logs,compress_logs
```

## How it works

`must-gather-sos` builds a bash program and hands it to
`oc adm must-gather` after `--`. See exactly what it will do with
`--dry-run`, which needs no cluster:

```console
$ must-gather-sos -o core,etcd --since 2h --dry-run
```

### The quoting problem

Getting a generated script into the pod is not as simple as it looks. `oc`
does two things to whatever follows `--`
([mustgather.go](https://github.com/openshift/oc/blob/master/pkg/cli/admin/mustgather/mustgather.go)):

1. `executedCommand = strings.Join(o.Command, " ")` — the argv is flattened
   into one string, so a multi-element command gets re-split by a shell later.
2. `buildPodCommand` embeds that string in an **unquoted heredoc**:

   ```bash
   setsid -w bash <<-MUSTGATHER_EOF
   <command>
   MUSTGATHER_EOF
   ```

   An unquoted delimiter means the *outer* shell expands `$VAR`, `$(...)` and
   backticks before the pod's bash ever sees the text.

So a plain inline script would be quietly corrupted. `must-gather-sos` passes
exactly one argv element and base64-encodes the real script, whose alphabet
(`A-Za-z0-9+/=`) the shell cannot act on:

```bash
oc adm must-gather --dest-dir=... --since=2h -- \
  "echo 'IyEvYmluL2Jhc2gK...' | base64 -d >/tmp/must-gather-sos.sh && bash /tmp/must-gather-sos.sh"
```

`tests/test_runner.py` is the regression guard for this.

### Upstream fidelity

The generated script mirrors upstream's `gather`, including the details a
naive wrapper loses:

- `gather_aro` runs serially *after* the parallel `wait`, not alongside.
- `gather_ppc` is given `MUST_GATHER_NODES_INSPECTED=true` only because the
  base inspect already covered nodes — so it is **not** set when `core` is
  deselected, letting `ppc` inspect nodes itself.
- `gather_service_logs` takes its node role as a positional argument, default
  `master`.
- `gather_network_logs` starts by running `/usr/bin/gather`, making it a
  superset of everything. Combining it with `-n` is rejected rather than
  silently ignoring your skips.
- `gather_metrics` exits 1 when called with no arguments, so it is excluded
  from `-a` unless you give it a `-k` option.
- The pod's working directory is pinned to `/`, because upstream mixes
  `must-gather` and `../must-gather` relative paths that only agree from there.

## Output

```
must-gather.local.1234567890/
└── <image-sha>/
    ├── manifest.json                     what ran, and how it went
    ├── must-gather-sos-script.sh         the script that ran in the pod
    ├── must-gather-sos-plugins.log       name / exit code / seconds, per plugin
    ├── version
    └── ...                               the usual must-gather tree
```

`manifest.json` records the tool and catalog versions, the plugin selection
and options, the `--since` window, cluster and `oc` versions, timings, `oc`'s
exit code, and each plugin's exit status and duration.

Plugin failures do **not** change the exit status — must-gather deliberately
collects what it can. They are listed at the end of the run and in the
manifest:

```
plugins that reported a non-zero exit status:
  etcd: rc=1 after 3s
```

### Archiving

```console
$ must-gather-sos -o core,etcd --case-id 04123456 --archive
archive: must-gather-04123456-20261016T153045Z.tar.xz (182.4 MiB)
```

## The plugin catalog

Plugin names, the default set, execution phases, per-plugin arguments and the
`core` resource lists all come from a JSON catalog at
`must_gather_sos/data/openshift.json`, generated from upstream by parsing
`collection-scripts/gather`:

```console
$ make sync-catalog              # from main
$ make sync-catalog REF=release-4.19
```

The catalog is committed, so `--list-plugins` and `--dry-run` work offline and
every resync is a reviewable diff. Human descriptions and plugin option specs
live in the `OVERLAY` table in `tools/sync_catalog.py` and are merged in at
sync time.

Point at a different catalog with `--catalog PATH`.

## Development

```console
$ make check      # compile-check, shellcheck, bash -n, unit tests
$ make test
$ make golden     # regenerate golden script fixtures after an intended change
```

The tests need neither a cluster nor network access.

- `tests/data/golden/` pins the generated bash. The fixtures are normalised so
  a catalog resync does not churn them; `make golden` regenerates them.
- `tests/test_integration.py` builds a fake must-gather image on disk —
  upstream's real `common.sh`, a stub `oc`, stub `gather_*` scripts — and runs
  the generated program *through the same mangling `oc` applies*, joined argv
  in an unquoted heredoc. That is how `--since` plumbing, plugin ordering,
  `env_if_core` and the base64 escaping are checked for real rather than by
  string matching.
- `tests/test_runner.py` asserts the payload alphabet and the single-argv-element
  invariant.

## Limitations

- One `--image` at a time: the catalog describes a single image's plugin
  layout. Run `must-gather-sos` once per image, or pass `--catalog` for a
  third-party must-gather image.
- The catalog is a snapshot. If your cluster's must-gather image is much newer
  than the last sync, a brand-new plugin will be missing from the list; the
  base inspect may also miss a newly added resource type. `make sync-catalog`
  fixes both.
- Skipping plugins does not make the base inspect smaller. If `core` dominates
  your collection size, `--since` and `--reduce-logs` are the levers.

## Licence

Apache-2.0, matching upstream `openshift/must-gather`.
