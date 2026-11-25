# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`sjobs` is a small Click CLI that shows Slurm jobs grouped into RUNNING, PENDING and OTHER tables, under a summary with the user's fair share. It runs one shell script (`sshare`, `sprio`, then `squeue --json`) either locally or on a configured SSH host, once or repeatedly with `--watch`. Runtime dependencies are only `click` and `tabulate`; see `pyproject.toml` for the supported Python range.

## Commands

- Lint/format: `pixi run lint` (ruff check plus format check), `pixi run format` to apply
- Tests: `pixi run test`, or `python -m unittest discover -s tests -v` in any environment with the package installed (`pip install -e .`)
- Single test: `python -m unittest tests.test_sjobs.JobTests.test_state_flags_override_base_state -v`
- CLI: `sjobs`, `sjobs --all`, `sjobs --user NAME`, `sjobs -H HOST`, `sjobs -w [--interval 60]`, `python -m sjobs`
- Build, as CI does it: `pixi install && pixi build && pixi run hatch build` (conda package plus wheel/sdist)

## Architecture

All logic lives in `sjobs/sjobs.py`. `__init__.py` re-exports `main` (the `sjobs` console script), and `__main__.py` enables `python -m sjobs`.

`main` runs a single pipeline:
1. **Config**:
   - `load_config` reads optional TOML from `config_path()`: `SJOBS_CONFIG`, else `$XDG_CONFIG_HOME/sjobs/config.toml`, else `~/.config/sjobs/config.toml`.
   - `resolve_host` picks `--host`, then `SJOBS_HOST`, then `host` from config.
   - `path_prefixes` combines the home directory where `squeue` runs with `path_prefixes` from config.
2. **Command**: `build_script` writes one `sh` script, and `build_cmdline` runs it with `sh -c` locally, or as `ssh HOST "sh -lc '<script>'"` when a host is set. With no host and no local `squeue`, it raises an error naming the three ways to set a host. The script:
   - prints `SJOBS_HOME=` marker lines for `$HOME` and its physical path (Slurm records physical working directories);
   - runs `sshare` and `sprio` through `optional_section`. Each prints a `SJOBS_SECTION=<name>` marker, merges stderr into stdout, and prints `<tool> not found` when the tool is missing, so these steps can never fail the run;
   - prints `SJOBS_SECTION=squeue` and runs `build_squeue_args` last, so squeue's exit status and stderr decide success. `--me` is the default and `--user` takes precedence over `--all`. sshare/sprio get `-u NAME` or `-u "$(id -un)"`, and `--all` leaves sprio unfiltered.
   - User input must stay a single shell-quoted argument. One test checks for injection, and another runs the script in a real `sh` with fake `squeue`/`sshare`/`sprio`.
3. **Fetch**: `run_squeue` runs the command with a timeout. Every failure becomes a `click.ClickException`, so no partial tables are printed. This covers non-zero exit, a missing executable, timeouts, bad JSON, Slurm `errors` and unexpected shapes.
   - `split_sections` returns the homes, the tool sections and the squeue JSON. It drops login-profile noise before the first marker, and treats output without markers as plain JSON.
   - `fetch_snapshot` returns `{jobs, shares, problems}`. `parse_sshare`/`parse_sprio` never raise: unusable output becomes a one-line `problem`, which the summary shows as a dim note.
4. **Normalize**: `parse_squeue_json` validates the payload, and `normalize_job(job, prefixes, now, priorities)` turns each raw job into flat display strings.
   - Slurm wraps scalars as `{set, infinite, number}`, so read values through `unwrap_slurm_value`/`format_value`/`epoch`/`time_limit_seconds`.
   - `SLURM_NO_VAL` marks unset array/het IDs and timestamps.
   - GPUs (`format_gpu_requests`): use the allocation in `tres_alloc_str` when present, else the scoped `tres_per_{job,node,socket,task}` requests, else `tres_req_str`.
   - Times are relative to `now`, which is passed in (tests use a fixed `NOW`): `format_duration`, `format_age`, `format_when`, `progress_bar`.
   - Priority comes from sprio's best partition row, else squeue's `priority`. `format_factors` shows the weighted factors as shares.
5. **Render**: `render_frame` builds the whole output as one string, `render_summary` plus each `render_category`, so one-shot mode and `run_watch` share it.
   - `classify_job` buckets jobs by `primary_state`. Flags in `STATE_FLAGS` override the base state, as squeue does.
   - `CATEGORIES` holds each category's columns, colour and sort keys. `sort_jobs` sorts by the `_`-prefixed raw keys from `normalize_job` (unset values last; `_priority` is negated so the highest comes first).
   - `category_fields` adds a User column with `--all`/`--user`.
   - `fit_table` caps the columns in `CAPPED` (CWD from the left, Name from the right). When `terminal_width()` reports a terminal (stdout is a TTY) that is too narrow, it shrinks the `SHRINKABLE` columns widest-first down to their minimums. Table cells must not contain ANSI codes, because widths are measured with `len()`.
   - To add a column, add a key in `normalize_job` and reference it in `CATEGORIES`.
6. **Watch**: `--watch` calls `run_watch`, which clears the screen (on a TTY only), redraws, and sleeps `--interval` seconds (`MIN_WATCH_INTERVAL` is 30). Errors are shown inside the frame, and Ctrl-C exits with status 0.

Each run (and each watch refresh) starts exactly one process or SSH connection, which makes exactly one `squeue` call, to keep load on shared Slurm controllers low. A CLI test asserts this.

## Tests

`tests/test_sjobs.py` runs fully offline:
- `subprocess.run` and `shutil.which` are patched.
- `setUpModule` points `SJOBS_CONFIG` at a missing file and clears `SJOBS_HOST`, so the developer's own config never leaks in.
- `tests/fixtures/squeue.json`, `sshare.txt` and `sprio.txt` are synthetic fixtures modeled on Slurm 23.11 output. `script_output()` combines them into what the script prints.

## CI

- `.github/workflows/test.yml` runs on pull requests and is reused by the deploy workflow. A lint job runs `pixi run lint`; the test job runs the tests on the oldest and newest supported Python, then checks `sjobs --help` and `python -m sjobs --help`.
- `.github/workflows/deploy.yml` runs on pushes to `main` or `dev` and on `v*` tags: test, then build.
  - Branch pushes only do a dry run.
  - A `v*` tag must match `version` in `pyproject.toml`. It pushes the artifacts to the `mvondomaros-lab/mvondomaros-lab.github.io` pages repo and opens a PR there.
