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
sjobs --watch         # Redraw every 60 s until Ctrl-C (--interval, at least 30 s)
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
shortened in the CWD column without extra configuration. The default user filter
applies to the account running `squeue`, which is your remote account when using
SSH.

The config file is `$XDG_CONFIG_HOME/sjobs/config.toml` (default
`~/.config/sjobs/config.toml`). Set `SJOBS_CONFIG` to use another file.

```toml
host = "mycluster"
# Prefixes removed from the CWD column, in addition to your home directory
# (local, or remote when using SSH).
path_prefixes = ["/scratch/", "/mnt/nfs/home/"]
```

Each invocation runs one shell script, locally or over a single SSH connection. It
calls `sshare` (your fair share), `sprio` (priority factors of pending jobs) and
exactly one `squeue` query, which includes all job states still known to the
controller. Only `squeue` is required: if `sshare` or `sprio` is missing or fails
(for example on clusters without the multifactor priority plugin), sjobs prints a
short note instead. A stuck connection or `squeue` is stopped after 60 seconds.

`--watch` repeats this every `--interval` seconds (default 60, minimum 30) and
redraws the screen; a failed refresh is shown and the next one is tried. Use it
instead of `watch sjobs`, and avoid running it with `--all` for long, as that adds
load on the Slurm controller.

### Faster SSH

Most of a remote run is spent setting up the SSH connection. Reusing one
connection makes repeated runs (and every `--watch` refresh) nearly instant and
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

```
sjobs · mycluster · Wed 14:32   3 running (96 CPUs, 8 GPUs) · 5 pending · 1 other
Fair share  ag_example  0.734  (usage 6.1% vs. 8.0% share)

>>> RUNNING (3)

  Job ID  Name     CWD          CPUs  GPUs      Nodes    Elapsed  Progress        Left  Ends
--------  -------  ---------  ------  --------  -------  -------  -------------  -----  --------------
  812345  md-run1  proj/run1      32  4 x A100  gpu007     3h12m  ██░░░░░░  13%  20h48m  tomorrow 11:20
```

The first line shows the host, the time, and job counts with the CPUs and GPUs
held by running jobs. The fair share line comes from `sshare`: the value (green
from 0.5, yellow from 0.2, red below) and your account's share of recent usage
compared with its share of the allocation. With `--all` or `--user` it is labeled
`user@account`, and the tables get a User column.

Running jobs show elapsed time, progress against the time limit, time left and
the end time. Pending jobs show how long ago they were submitted, the scheduler's
estimated start (when the backfill scheduler has one), their priority, and the
weighted factors that make up that priority as shares, e.g. `age 75% fs 20%
part 5%` (age, fair share, job size, partition, QOS, TRES). Jobs that wait on a
dependency are not ranked by `sprio`; for them, squeue's priority is shown without
factors.

Each table header shows its job count, e.g. `>>> PENDING (12)`. Running jobs are
sorted by end time (soonest first), pending jobs by priority (highest first, then
oldest submission), and other jobs by job ID. In a terminal, columns such as CWD
(down to 16 characters), Name, Nodes and Factors shrink so tables fit the window;
when piped or redirected, CWD is capped at 56 characters and Name at 32.

Array tasks retain identifiers such as `1000_4` or `1000_[1-4]`. Heterogeneous
jobs show the component as `1000+0`. Once GPUs are allocated, the GPUs column
shows the job's total by allocated type (e.g. `1 x A100 (80GB)`), however they
were requested (`--gpus`, `--gres=gpu:N`, …). Before allocation it shows the
requests with their scope: per-job, per-node, per-socket, and per-task values,
where `1 x GPU / node` means one GPU on each node and overlapping requests are
displayed separately. Working directories have your home directory (on the
machine running `squeue`) and any configured `path_prefixes` removed and are
shortened from the left when needed.
Times are shown relative to today (`today 18:05`, `Thu 09:12`) in the timezone
of the computer running sjobs.

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

