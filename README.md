# must-gather-sos

> **Disclaimer:** This is an independent, personal project. It is **not
> provided, maintained, or supported by Red Hat**, and comes with **no
> warranty or support** of any kind. Use at your own risk.

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

### What `--since` can and cannot reach

**`--since` is not universal.** It changes what is collected in exactly two
places:

| Route | Effect | Plugins |
| ----- | ------ | ------- |
| `oc adm inspect --since` | trims **pod logs** | `core` and 13 others |
| `oc adm node-logs --since` on a **journal** query | trims **host service logs** | `service_logs`, `ppc` |

Everywhere else the window is accepted and has no effect.

Worth being precise about one case, because it looks like it should work.
`gather_audit_logs` **does support `--since`** — it sources `common.sh`, calls
`get_log_collection_args`, and threads `node_log_collection_args` into both of
its `oc adm node-logs` calls, with a comment documenting the fifth xargs
argument as `--since=ISO time`. The plugin is not the problem. The two layers
underneath it are:

1. **`oc` does not send it.** `oc adm node-logs` attaches the `since`
   parameter only when the path is `journal`
   ([`pkg/cli/admin/node/logs.go`](https://github.com/openshift/oc/blob/master/pkg/cli/admin/node/logs.go)):

   ```go
   if o.Path == "journal" {
           ...
           if len(o.Since) > 0 { req.Param("since", o.Since) }
   ```

   The flag's own help says *"Only applies to node service logs."*

2. **The kubelet would not honour it anyway.** Asked directly, it *validates*
   the parameter but does not time-filter plain file serving.

Measured on a 4.18.54 cluster at 06:40 UTC, against a file rotated at 19:30
the previous day:

```console
$ oc adm node-logs master-0 --path=kube-apiserver              # listing
$ oc adm node-logs --since=-5m master-0 --path=kube-apiserver  # byte-identical

$ oc get --raw ".../logs/kube-apiserver/audit-2026-10-15T19-30-44.933.log"
134269 lines, first event 2026-10-15T18:26:39Z
$ ... "?since=-5m"      -> 134269 lines, same first event
$ ... "?since=-1h"      -> 134269 lines, same first event
$ ... "?since=2026-10-16 06:00:00" -> 134269 lines, same first event
```

So the affected plugins always return whole files: `audit_logs`,
`kas_startup_termination_logs`, `windows_node_logs`. Plugins that `oc cp`
files out of pods (`insights`, `haproxy_config`, `core_dumps`, …) or query
Prometheus (`monitoring`, `metrics`) have no window either. **All of this is
identical under a plain `oc adm must-gather --since`.**

`must-gather-sos` will not let it surprise you — it says so before it runs:

```console
$ must-gather-sos -o audit_logs -k audit_logs.force=on --since 30m
  warning: audit_logs forwards --since to 'oc adm node-logs --path=FILE', but
           oc only applies it to journal queries and the kubelet does not
           time-filter files, so whole files are collected regardless.
```

`--list-plugins` annotates every plugin with what `--since` does to it, and
`--list-plugins --format json` exposes it as a `since` field
(`pod-logs` / `journal` / `both` / `passed-through` / `none`).

Where `--since` *does* work, it works well. `-o core --since 5m` on that same
cluster left 657 of 916 pod logs empty, with every surviving entry inside the
window.

## How it works

`must-gather-sos` builds a bash program and hands it to
`oc adm must-gather` after `--`. Three flags show you exactly what it will do,
none of which need a cluster:

```console
$ must-gather-sos -o core,etcd --since 2h --dry-run        # summary + script + command
$ must-gather-sos -o core,etcd --since 2h --print-command  # just the command, paste-ready
$ must-gather-sos -o core,etcd --since 2h --print-script   # just the in-pod script
```

`--print-command` emits a single line you can copy, tweak and run yourself:

```console
$ must-gather-sos -o etcd --since 30m --print-command
oc adm must-gather --dest-dir=must-gather.local.123 --since=30m -- "echo 'IyEvYmlu...' | base64 -d >/tmp/must-gather-sos.sh && bash /tmp/must-gather-sos.sh"
```

Pipe it through `--print-script` to read what that base64 actually is.

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

## Several images at once

`oc adm must-gather` takes repeated `--image`, and so does this:

```console
$ must-gather-sos \
    --image quay.io/openshift/origin-must-gather:4.18 \
    --image registry.redhat.io/rhacm2/acm-must-gather-rhel9:v2.14 \
    -n monitoring --jobs 4
```

Each image needs a catalog, and one is chosen by matching the reference
against the catalogs listed by `--list-catalogs`. If none matches, say which
describes it:

| you write | you get |
|---|---|
| nothing | the cluster's own payload image, `openshift` catalog |
| `--image IMG` | the catalog whose pattern matches `IMG` |
| `--catalog NAME` | that catalog, and its default image if it has one |
| `--catalog NAME --image IMG` | they pair — the long-standing fork workflow |
| `--catalog IMG=NAME` | pins `NAME` to `IMG`; repeatable |

`-o`, `-n`, `-e` and `-k` are **global**: a name has to exist in *some*
selected catalog, and simply does not apply to the images that lack it. Above,
`-n monitoring` skips OpenShift's monitoring plugin and means nothing to ACM.
If a selection leaves an image with nothing to do, that image is dropped from
the run and the reason is printed.

### How one command covers several images

This is the part worth understanding, because it shapes everything else.
`oc` joins everything after `--` into a **single** string and gives that same
string to every image's pod — `o.Command` in
[`mustgather.go`](https://github.com/openshift/oc/blob/master/pkg/cli/admin/mustgather/mustgather.go)
is one field, applied by `newPod` to each. There is no per-image command.

So rather than running `oc` once per image, the generated script carries a
branch per image family and works out at runtime which one it woke up in:

```bash
mgs_detect() {
	if [ -f /usr/bin/common.sh ]; then echo openshift; return 0; fi
	if [ -f /usr/bin/gather_utils ]; then echo acm; return 0; fi
	return 1
}

case "$(mgs_detect)" in
openshift) mgs_run_openshift ;;
acm)       mgs_run_acm ;;
*)         echo "must-gather-sos: running its own entrypoint instead."
           exec /usr/bin/gather ;;
esac
```

`--print-script` shows you the whole thing. Three consequences:

- **oc's output layout is untouched.** Each pod writes its own
  `/must-gather`, and oc copies it to its own digest-named directory, exactly
  as for a single image. Each pod also drops a `must-gather-sos-profile` file
  naming the branch it took, because the digest is not predictable and that
  is how each directory is attributed to an image afterwards.
- **The pods run in parallel**, four at a time, which is oc's own limit.
- **An image no catalog describes runs its own entrypoint**, which is what
  plain `oc adm must-gather` would have done, so an extra `--image` is never
  worse than useless.

The trade is that two pods' progress lines interleave in the live stream.
`oc` prefixes each with `POD <name>`, and every pod still writes its own
clean `gather-progress.log` and `task-logs/`, so the per-image record stays
readable.

### ACM

The ACM catalog covers `stolostron/must-gather`: `hub_logs`,
`governance_logs`, `application_lifecycle_logs`, `observability_logs`,
`dr_logs`, `mce_logs`, `spoke_logs`, `mce_spoke_logs`, `hostedcluster`, and
`all_logs` as the whole hub fan-out in one.

```console
$ must-gather-sos --catalog acm -o governance_logs,hub_logs
$ must-gather-sos --catalog acm -o hostedcluster -k hostedcluster.name=hc1
```

Two differences from the OpenShift image are worth knowing, and both are why
catalogs carry a runtime *profile* rather than assuming upstream's layout:

- **`--since` and `--reduce-logs` do nothing here.** ACM has no `common.sh`
  and never reads `MUST_GATHER_SINCE`. `oc` still sets the variable on the
  pod, so the flag looks like it worked; `must-gather-sos` says plainly that
  it did not, once, instead of listing every plugin.
- **The sub-scripts read `IS_HUB` / `IS_ENGINE` / `IS_OBS_HUB`**, defaulting
  them to false, and only ACM's own `gather` entrypoint sets them. The
  profile runs the same `check_if_*` detection before collecting. Without
  that the plugins would collect nothing — and since an empty log reads as
  `no-op`, they would do it quietly.

ACS is **not** supported. There is no official Red Hat ACS must-gather image;
the documented route is `roxctl central debug download-diagnostics`, a
client-side CLI needing Central credentials rather than a collection image.

## Reading the run

A default must-gather launches a couple of dozen collection scripts at once
and lets their output interleave into one stream. The result is thousands of
lines in no particular order, with no way to tell how far along the run is,
which scripts have finished, or which did anything at all. Support cases
routinely arrive with a log nobody has read.

The run is given the shape `sos report` has had for years. The plan is
announced before anything starts, each task reports `[n/total]` as it
finishes, and a summary at the end says what collected something, what
collected nothing, and what failed:

```console
$ must-gather-sos -o core,etcd,sriov,monitoring --since 30m

Collecting with 6 task(s).
  core:all-namespaces core:groups core:named etcd monitoring sriov

Per-task output goes to /must-gather/task-logs/, not into this log.

  [1/6] core:named                              ok          9s
  [2/6] sriov                                   no-op       1s
  [3/6] core:all-namespaces                     ok         14s
  ... 3/6 done; still running: etcd 41s, monitoring 41s
  [4/6] etcd                                    ok         63s
  [5/6] core:groups                             ok         71s
  [6/6] monitoring                              ok        112s

----------------------------------------------------------------
6 task(s): 5 ok, 1 no-op, 0 failed, in 1m54s

  collected nothing (not present on this cluster):
    sriov                                    not detected

  slowest:
    monitoring                                 112s  ok
    core:groups                                 71s  ok
    etcd                                        63s  ok
```

Four things follow from that:

- **Each task's own output goes to its own file**, `task-logs/<name>.log` in
  the collection, instead of into the shared stream. The stream is then
  short enough to read.
- **A task that collects nothing is not a failure.** Most of a default gather
  is no-ops on any given cluster — the operator simply is not installed — and
  `no-op` says so rather than leaving you to guess from silence.
- **A script the image does not have is `missing`, not `failed`.** That is
  `rc=127`, and it means the catalog and the image are from different
  releases; the summary says so and suggests the matching `--image`.
- **A slow task does not look like a hang.** Every 30 seconds
  (`TASK_HEARTBEAT`) the run says what is still going and for how long.

### Running fewer at once

A default gather launches two dozen collection scripts simultaneously, which
is a lot of `oc adm inspect` arriving at one API server. `--jobs N` caps how
many run at a time, the way `sos report --threads` does:

```console
$ must-gather-sos --jobs 4 --since 2h

Collecting with 27 task(s), 4 at a time.
...
  ... 9/27 done; still running: monitoring 44s, etcd 12s, olm 11s, sriov 9s; 14 queued
```

The default is `0`, unlimited — exactly what upstream's `gather` does, so
nothing changes unless you ask for it. The value is written into the
generated script as `TASK_JOBS`, visible in `--print-script`, because nothing
carries an environment variable into the pod.

Worth knowing about the cap: a task waiting for a slot has genuinely not
started, so it is not counted as running and its reported duration is
collection time, not queue time. Liveness is checked against the kernel
rather than against the results file, so a task the OOM killer takes still
frees its slot instead of stalling the run.

Compression (`--reduce-logs compress_logs`) joins the plan on the same terms
as everything else, so gzipping several hundred megabytes shows as progress
rather than as a pause after the summary.

The progress library is
[`must_gather_sos/data/task_common.sh`](must_gather_sos/data/task_common.sh).
It is inlined into the generated script because the image has no copy of it,
and is kept function-for-function in step with better-gather's
`collection-scripts/task_common.sh` so the two can be resynced; the
deliberate differences are marked `DIVERGENCE` in the file.

## oc's own noise

Most of what a must-gather prints is not about the collection. Two sources,
neither of which the must-gather image can do anything about, because both
are `oc`'s:

- a disk-usage line every five seconds for the whole run, from a checker oc
  starts as a sibling process inside the pod;
- one line per collected file as oc copies the data out — thousands of them
  on a real cluster, ending in `sent … bytes  received … bytes`.

Both are hidden by default, and the unfiltered stream is written to
`oc-output.log` in the output directory, so nothing is lost:

```console
$ must-gather-sos -o core,etcd --since 30m
  [ 1/3] core:named    ok    9s
  ...
(2847 line(s) of oc's disk-usage and file-copy output hidden; full output in
 must-gather.local.123/oc-output.log; --no-filter shows everything)
```

The copy listing is silenceable at source — `rsync.RsyncOptions` has a
`Quiet` field, which is what `oc rsync -q` sets — but `oc adm must-gather`
never sets it. Until it does, a wrapper is the only layer that can drop
these, which is rather the point of this one.

## Output

```
must-gather.local.1234567890/
├── oc-output.log                         oc's stream, unfiltered
├── manifest.json                         the run, when several images ran
└── <image-sha>/                          one per --image, named by digest
    ├── manifest.json                     what ran, and how it went
    ├── must-gather-sos-script.sh         the script that ran in the pod
    ├── must-gather-sos-plugins.log       name / state / rc / seconds / detail
    ├── gather-progress.log               the progress, without oc's chatter
    ├── must-gather-sos-profile           which image family this was
    ├── task-logs/                        one log per task
    │   ├── core:named.log
    │   ├── etcd.log
    │   └── ...
    ├── version
    └── ...                               the usual must-gather tree
```

`must-gather-sos-plugins.log` is tab separated, one row per task, and is what
the manifest is built from. `gather-progress.log` is the same progress you saw
live, minus oc's disk-usage checker — that checker is a sibling process
writing to the same container stdout, so the live stream cannot be cleaned up
from inside the pod, but the archive can have a readable record.

`manifest.json` records the tool and catalog versions, the plugin selection
and options, the `--since` window, cluster and `oc` versions, timings, `oc`'s
exit code, and each plugin's state, exit status and duration, with a tally by
state.

Plugin failures do **not** change the exit status — must-gather deliberately
collects what it can. The pod prints its own summary, but it arrives in the
middle of the copy step and scrolls away, so the tally is repeated locally
once the data has landed:

```
6 collection task(s): 5 collected, 0 found nothing, 1 failed.
  failed: etcd rc=1 after 3s
  A non-zero status is common and often harmless -- oc adm inspect exits 1
  when an optional resource is absent. Data already collected is still written.
  per-task output: task-logs/ in the output directory
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

Point at a different catalog with `--catalog PATH`. A second catalog ships
for [better-gather](https://github.com/dsedgmen/better-gather), a fork of
must-gather that applies the window to file collection itself:

```console
$ must-gather-sos --catalog must_gather_sos/data/better-gather.json \
      --image <better-gather-image> --run-namespace <ns> -o audit_logs --since 30m
```

Against that image the `--since` warnings correctly go away for the plugins
the fork fixes — `audit_logs`, `kas_startup_termination_logs`, `insights`,
`core_dumps`, `machineconfigdaemon_termination_logs` and `network_logs` all
move from `passed-through`/`none` to `files`. `windows_node_logs` honestly
stays unsupported: it fetches fixed filenames, so there is nothing for a
window to select between.

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
- `tests/test_tasklib.py` runs `must_gather_sos/data/task_common.sh` directly,
  in a temp directory. It is a port of better-gather's
  `tests/task_common.bats`, case for case, so the two can be compared when
  either side changes — including the awkward ones: flock missing, flock
  present but failing, forty tasks finishing at once, and the heartbeat not
  outliving `task_wait`.

## Limitations

- Two catalogs describing the same kind of image cannot be used in one run.
  The pod picks its branch by probing its own filesystem, so `openshift` and
  `better-gather` -- same layout, different `--since` metadata -- are
  indistinguishable once the script is running. Collect them separately.
- No ACS support; see above.
- The catalog is a snapshot. If your cluster's must-gather image is much newer
  than the last sync, a brand-new plugin will be missing from the list; the
  base inspect may also miss a newly added resource type. `make sync-catalog`
  fixes both.
- Skipping plugins does not make the base inspect smaller. If `core` dominates
  your collection size, `--since` and `--reduce-logs` are the levers.
- `ok` versus `no-op` is a heuristic: a task is a no-op when it exits zero and
  either printed nothing or printed one of a handful of phrases
  (`TASK_NOOP_PATTERN` in the library) that upstream scripts use to say the
  thing they collect is not installed. A task that collects plenty *and*
  happens to print "not found" somewhere is called a no-op anyway. The row
  carries the matched phrase so you can see why, and the data is collected
  either way — only the label is wrong. Inherited as-is from better-gather.

## Licence

Apache-2.0, matching upstream `openshift/must-gather`.
