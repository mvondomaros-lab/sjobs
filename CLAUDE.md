# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`sjobs` is a small Click CLI that shows Slurm jobs grouped into RUNNING, PENDING and OTHER tables. It runs `squeue --json` either locally or on a configured SSH host. Runtime dependencies are only `click` and `tabulate`; see `pyproject.toml` for the supported Python range.

## Commands

- Lint/format: `pixi run lint` (ruff check plus format check), `pixi run format` to apply
- Tests: `pixi run test`, or `python -m unittest discover -s tests -v` in any environment with the package installed (`pip install -e .`)
- Single test: `python -m unittest tests.test_sjobs.JobTests.test_state_flags_override_base_state -v`
- CLI: `sjobs`, `sjobs --all`, `sjobs --user NAME`, `sjobs -H HOST`, `python -m sjobs`
- Build, as CI does it: `pixi install && pixi build && pixi run hatch build` (conda package plus wheel/sdist)

## Architecture

All logic lives in `sjobs/sjobs.py`. `__init__.py` re-exports `main` (the `sjobs` console script), and `__main__.py` enables `python -m sjobs`.

`main` runs a single pipeline:
1. **Config**:
   - `load_config` reads optional TOML from `config_path()`: `SJOBS_CONFIG`, else `$XDG_CONFIG_HOME/sjobs/config.toml`, else `~/.config/sjobs/config.toml`.
   - `resolve_host` picks `--host`, then `SJOBS_HOST`, then `host` from config.
   - `path_prefixes` combines the home directory where `squeue` runs with `path_prefixes` from config.
2. **Command**: `build_squeue_args` builds a single `squeue --json --states=all` call: `--me` by default, and `--user` takes precedence over `--all`. `build_cmdline` decides how it runs:
   - A host is set: `ssh HOST "sh -lc '<script>'"`. The script prints `SJOBS_HOME=` marker lines for `$HOME` and its physical path (Slurm records physical working directories), then runs `squeue`. `split_remote_output` drops any login-profile noise before the markers and returns the remote homes. User input must stay a single shell-quoted argument; one test checks for injection, and another runs the script in a real `sh` with a fake `squeue`.
   - No host: run `squeue` locally, or raise an error naming the three ways to set a host.
3. **Fetch**: `run_squeue` runs the command with a timeout. Every failure becomes a `click.ClickException`, so no partial tables are printed. This covers non-zero exit, a missing executable, timeouts, bad JSON, Slurm `errors` and unexpected shapes.
4. **Normalize**: `parse_squeue_json` validates the payload, and `normalize_job` turns each raw job into flat display strings.
   - Slurm wraps scalars as `{set, infinite, number}`, so read values through `unwrap_slurm_value`/`format_value`.
   - `SLURM_NO_VAL` marks unset array/het IDs.
   - GPUs (`format_gpu_requests`): use the allocation in `tres_alloc_str` when present, else the scoped `tres_per_{job,node,socket,task}` requests, else `tres_req_str`.
5. **Render**:
   - `classify_job` buckets jobs by `primary_state`. Flags in `STATE_FLAGS` override the base state, as squeue does.
   - `render_category` prints each non-empty category with its count, using the column, colour and sort spec in `CATEGORIES`. `sort_jobs` sorts by the `_`-prefixed raw keys from `normalize_job` (unset values last).
   - `normalize_job` keeps the full prefix-stripped CWD. `fit_table` truncates it from the left at `MAX_CWD_WIDTH`, and shrinks it toward `MIN_CWD_WIDTH` when `terminal_width()` reports a terminal (stdout is a TTY) that is too narrow.
   - To add a column, add a key in `normalize_job` and reference it in `CATEGORIES`.

Each run makes exactly one `squeue` call, to keep load on shared Slurm controllers low. A CLI test asserts this.

## Tests

`tests/test_sjobs.py` runs fully offline:
- `subprocess.run` and `shutil.which` are patched.
- `setUpModule` points `SJOBS_CONFIG` at a missing file and clears `SJOBS_HOST`, so the developer's own config never leaks in.
- `tests/fixtures/squeue.json` is a synthetic fixture modeled on Slurm's `squeue --json` schema.

## CI

- `.github/workflows/test.yml` runs on pull requests and is reused by the deploy workflow. A lint job runs `pixi run lint`; the test job runs the tests on the oldest and newest supported Python, then checks `sjobs --help` and `python -m sjobs --help`.
- `.github/workflows/deploy.yml` runs on pushes to `main` or `dev` and on `v*` tags: test, then build.
  - Branch pushes only do a dry run.
  - A `v*` tag must match `version` in `pyproject.toml`. It pushes the artifacts to the `mvondomaros-lab/mvondomaros-lab.github.io` pages repo and opens a PR there.
