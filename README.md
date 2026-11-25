# sjobs

[![made-with-python](https://img.shields.io/badge/Made%20with-Python-green.svg)](https://www.python.org/)
[![MIT license](https://img.shields.io/badge/License-MIT-green.svg)](https://lbesson.mit-license.org/)

Show Slurm jobs grouped into running, pending, and other states, either on a
cluster login node or from your own machine over SSH.

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
work. On the remote side `squeue` runs in a login shell (`sh -lc`), so `PATH` set
in your profile applies. The same call first prints the remote home directory
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

Each invocation makes exactly one `squeue` query and includes all job states still
known to the controller. sjobs has no watch mode, and running it in a tight
`watch` loop (especially with `--all`) adds needless load on the Slurm controller.
A stuck connection or `squeue` is stopped after 60 seconds.

### Faster SSH

Most of a remote run is spent setting up the SSH connection. Reusing one
connection makes repeated runs nearly instant and avoids repeated logins on the
cluster's login node:

```sshconfig
Host mycluster
    ControlMaster auto
    ControlPath ~/.ssh/cm-%C
    ControlPersist 10m
```

For older completed jobs, use `sacct`. See the
[squeue manual](https://slurm.schedmd.com/squeue.html) for details.

## Output

Each table header shows its job count, e.g. `>>> PENDING (12)`. Running jobs are
sorted by end time (soonest first), pending jobs by submit time (oldest first),
and other jobs by job ID. In a terminal, the CWD column shrinks (down to 16
characters) so tables fit the window; when piped or redirected, it is capped at
56 characters.

Array tasks retain identifiers such as `1000_4` or `1000_[1-4]`. Heterogeneous
jobs show the component as `1000+0`. Once GPUs are allocated, the GPUs column
shows the job's total by allocated type (e.g. `1 x A100 (80GB)`), however they
were requested (`--gpus`, `--gres=gpu:N`, …). Before allocation it shows the
requests with their scope: per-job, per-node, per-socket, and per-task values,
where `1 x GPU / node` means one GPU on each node and overlapping requests are
displayed separately. Working directories have your home directory (on the
machine running `squeue`) and any configured `path_prefixes` removed and are
shortened from the left when needed.
Timestamps use the timezone of the computer running sjobs.

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

Tests use synthetic JSON fixtures modeled on Slurm's `squeue --json` schema, with
mocked subprocesses and an isolated config. They do not contact a cluster or
submit jobs. Pull requests run ruff and the tests on Python 3.11 and 3.14, and the
build/deploy workflow requires them to pass before building artifacts.

Pushes to `main` and `dev` build artifacts as a dry run. To release, set
`version` in `pyproject.toml`, merge into `main`, and push a matching tag
(`git tag v0.7 && git push origin v0.7`). The tag build publishes the conda
package and wheel to the lab's pages repository via a pull request there.

