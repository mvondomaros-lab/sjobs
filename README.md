# sjobs

[![made-with-python](https://img.shields.io/badge/Made%20with-Python-green.svg)](https://www.python.org/)
[![MIT license](https://img.shields.io/badge/License-MIT-green.svg)](https://lbesson.mit-license.org/)

Show Slurm jobs grouped into running, pending, and other states, together with
your fair share and what drives each pending job's priority, either on a cluster
login node or from your own machine over SSH.

## Installation

See [https://mvondomaros-lab.github.io](https://mvondomaros-lab.github.io).

For a local development installation with Python 3.11 or newer:

```sh
python -m pip install -e .
```

## Usage

```sh
sjobs                 # Your jobs
sjobs --all           # All users' jobs
sjobs --user alice    # One user's jobs (takes precedence over --all)
sjobs -H mycluster    # Run squeue on an SSH host
sjobs -c cwd,nodes    # Extra columns: cwd, nodes, partition, priority, or all
sjobs --interval 120  # Refresh every 2 minutes (default 60 s, at least 30 s)
sjobs --version       # Show the installed version
python -m sjobs       # Also available as a Python module
```

The cluster must support `squeue --json` (sjobs was developed against Slurm 23.11).

## Configuration

sjobs runs `squeue` over SSH on the first host it finds, in this order:

1. `--host`/`-H HOST`
2. the `SJOBS_HOST` environment variable
3. `host` in the config file

Without a host it runs `squeue` locally, and fails with a hint if `squeue` is not
on `PATH`. The host is anything `ssh` accepts, so aliases from `~/.ssh/config`
work. On the remote side the Slurm tools run in a login shell (`sh -lc`), so `PATH`
set in your profile applies. The same call first prints the remote home directory
(both `$HOME` and its physical path, since Slurm records physical paths), so it is
shortened in paths without extra configuration. The default user filter
applies to the account running `squeue`, which is your remote account when using
SSH.

The config file is `$XDG_CONFIG_HOME/sjobs/config.toml` (default
`~/.config/sjobs/config.toml`). Set `SJOBS_CONFIG` to use another file.

```toml
host = "mycluster"
# Prefixes removed from paths, in addition to your home directory
# (local, or remote when using SSH).
path_prefixes = ["/scratch/", "/mnt/nfs/home/"]
# Optional columns to always show (same as --columns).
columns = ["cwd", "nodes"]
# Color theme of the interactive view. "auto" (the default) uses your
# terminal's own colors, light or dark; any Textual theme name also works.
theme = "auto"
# GPU model names to look for in job constraints, if sjobs does not
# recognize yours (a100, a40, l40s, h200, rtxpro6000 and similar are built in).
gpu_types = ["bigcard"]
```

Each invocation runs one shell script, locally or over a single SSH connection. It
calls `sshare` (your fair share), `sprio` (priority factors of pending jobs) and
exactly one `squeue` query, which includes all job states still known to the
controller. Only `squeue` is required: if `sshare` or `sprio` is missing or fails
(for example on clusters without the multifactor priority plugin), sjobs prints a
short note instead. A stuck connection or `squeue` is stopped after 60 seconds.

sjobs is an interactive view that repeats this every `--interval` seconds
(default 60, minimum 30); a failed refresh is shown and the last tables are
kept. It needs a terminal.

| Key | Action |
|---|---|
| `↑` `↓`, `1` `2` `3` | move through the jobs (across tables), jump to running / pending / finished |
| `Enter`, `d` | details of the selected job (`Enter` on an array row shows its tasks) |
| `Space` | expand or fold the tasks of an array job |
| `l` | the end of the selected job's output file (`e` switches to errors) |
| `/`, `Esc` | filter by job ID, name, user, path, partition or reason; clear |
| `o` | choose optional columns |
| `a` | switch between your jobs and all users' jobs |
| `r` | refresh now |
| `c` | cancel the selected job with `scancel`, after asking `y`/`n` |
| `?` | the keys and what sjobs's markers and colors mean |
| `q` | quit |

What else the view does:

- **Status markers** start each row: `●` running (yellow near its time limit),
  `○` pending, `!` held or waiting on something outside the queue, `✗` blocked
  for good or failed, `✓` completed.
- **Array jobs** fold into one row per array (`500_[1-3,7]  sweep ×4`) with the
  total CPUs and memory and the average progress; `Space` shows the tasks.
- **Tabs** replace the stacked tables when they do not fit the window, for
  example with `--all`.
- **Changes are announced**: when a job starts or ends between refreshes, a
  notification names it (failures in red). With `desktop_notifications = true` in the config, sjobs also
  asks the terminal for a desktop notification (OSC 9: iTerm2, Ghostty, WezTerm,
  kitty and others).

Reading a log (`l`) and cancelling (`c`) each run one extra command (`tail` or
`scancel`) when you ask for them.

The view takes its colors from your terminal: at startup it asks the terminal for
its background, text color and 16-color palette and builds a theme from them, so
it matches any light or dark terminal theme. Terminals that do not answer get
Textual's light or dark theme. `Ctrl+P` offers other themes for the session, and
`theme = "…"` in the config picks one permanently.

Avoid leaving it on all users' jobs (`--all` or `a`) for long, as that adds load
on the Slurm controller.

### Faster SSH

Most of a remote run is spent setting up the SSH connection. Reusing one
connection makes every refresh nearly instant and
avoids repeated logins on the cluster's login node:

```sshconfig
Host mycluster
    ControlMaster auto
    ControlPath ~/.ssh/cm-%C
    ControlPersist 10m
```

For older completed jobs, use `sacct`. See the
[squeue manual](https://slurm.schedmd.com/squeue.html) for details.

## Output

A text rendering of the view:

```
sjobs  mycluster   ● 2 running 33 CPUs · 132G · 4 GPUs   ○ 2 pending   · 1 finished   fair share 0.016 low   14:32
! 812399 post: dependency failed (after 812350 ok); will never start

Running  2
   Job ID   Name      CPUs   Mem   GPUs              Progress            Left
●  812345   md-run1     32  128G   4 x A100 (80GB)   ━─────────  13%   20h48m
●  812346   prep         1    4G                     ━━━━━━━━━─  92%       9m

Pending  2
   Job ID   Name      CPUs   Mem   GPUs           Limit   Waiting   Starts    Why
○  812398   md-run2     32  128G   1 x A100|A40   2d00h       35m   in 3h33m  behind higher-priority jobs
✗  812399   post         4    8G                  1h00m       12m             dependency failed (after 812350 ok); will never start

Finished & other  1
   Job ID   Name    State    Exit   Ran     Ended    Log
✗  812350   md-eq   failed      1   2h03m   4m ago   proj/logs/md-eq-812350.log
```

What the view shows:

- **Header.** Job counts, the CPUs, memory and GPUs your running jobs hold, and
  your fair share as `sshare` reports it, with a verdict (good from 0.5, fair from
  0.2, low below). Pending jobs that will never start without your help are listed
  under it in red.
- **Running.** Progress against the time limit and the time left; yellow from 90%,
  because Slurm stops the job at its limit.
- **Pending.** The requested time limit, how long the job has waited, the
  scheduler's estimated start (`in 3h33m`, when it has one), and **Why** it waits, in words:
  red needs your action, yellow waits on a hold or unavailable nodes.
- **Finished & other.** Finished jobs stay in squeue for a few minutes. They show
  how they ended (red for failed, timed out, out of memory), the exit code or
  signal, the run time and the output file. For older jobs, use `sacct`.
- **Optional columns** (`--columns`, `columns` in the config, `o` in the
  interactive view): `cwd` (working directory), `nodes`, `partition`, and
  `priority` (the priority of pending jobs with its weighted factors, e.g.
  `17331 (partition 96%, fair share 2%, waiting time 1%)`). With `--all` or
  `--user` the tables also get a User column.

Running jobs are sorted by end time, pending jobs by priority (then submission),
and finished jobs most recent first. Long job IDs, names, reasons and paths are cut to a maximum width; tables wider
than the window scroll sideways (`←` `→` or the mouse). Paths have your home directory and any
`path_prefixes` removed.

Array tasks keep identifiers such as `1000_4` or `1000_[1-4]`, heterogeneous jobs
show the component as `1000+0`. GPUs show the allocated models once a job runs
(`1 x A100 (80GB)`), else the request with its scope (`1 x GPU / node`). A job
that asks for an untyped `--gpus 1` and picks the model with `--constraint
"a100|a40"` shows `1 x A100|A40`; a plain `1 x GPU` means any model. Times are
relative to today (`today 18:05`, `Thu 09:12`) in your computer's timezone.

## Development

```sh
pixi run test     # unit tests
pixi run lint     # ruff check and format check
pixi run format   # apply ruff fixes and formatting
```

Or, after installing the package:

```sh
python -m unittest discover -s tests -v
```

Tests use synthetic fixtures modeled on Slurm's `squeue --json`, `sshare -P` and
`sprio -o` output, with
mocked subprocesses and an isolated config. They do not contact a cluster or
submit jobs. Pull requests run ruff and the tests on Python 3.11 and 3.14, and the
build/deploy workflow requires them to pass before building artifacts.

Pushes to `main` and `dev` build artifacts as a dry run. To release, set
`version` in `pyproject.toml`, merge into `main`, and push a matching tag
(`git tag v0.8 && git push origin v0.8`). The tag build publishes the conda
package and wheel to the lab's pages repository via a pull request there.

