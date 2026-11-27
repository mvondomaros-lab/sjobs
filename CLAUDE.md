# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`sjobs` is a small Click CLI that shows Slurm jobs grouped into RUNNING, PENDING and OTHER tables, under a summary with the user's fair share. It runs one shell script (`sshare`, `sprio`, then `squeue --json`) either locally or on a configured SSH host, and shows the result in an interactive Textual app that refreshes itself (there is no print-once mode). Runtime dependencies are `click`, `rich` and `textual`; see `pyproject.toml` for the supported Python range.

## Commands

- Lint/format: `pixi run lint` (ruff check plus format check), `pixi run format` to apply
- Tests: `pixi run test`, or `python -m unittest discover -s tests -v` in any environment with the package installed (`pip install -e .`)
- Single test: `python -m unittest tests.test_sjobs.JobTests.test_state_flags_override_base_state -v`
- CLI: `sjobs`, `sjobs --all`, `sjobs --user NAME`, `sjobs -H HOST`, `sjobs -c cwd,nodes`, `sjobs --interval 120`, `python -m sjobs`
- Build, as CI does it: `pixi install && pixi build && pixi run hatch build` (conda package plus wheel/sdist)

## Architecture

All Slurm and formatting logic lives in `sjobs/sjobs.py`; `sjobs/tui.py` is the interactive app and only calls into it. `__init__.py` re-exports `main` (the `sjobs` console script), and `__main__.py` enables `python -m sjobs`.

`main` runs a single pipeline:
1. **Config**:
   - `load_config` reads optional TOML from `config_path()`: `SJOBS_CONFIG`, else `$XDG_CONFIG_HOME/sjobs/config.toml`, else `~/.config/sjobs/config.toml`.
   - `resolve_host` picks `--host`, then `SJOBS_HOST`, then `host` from config.
   - `path_prefixes` combines the home directory where `squeue` runs with `path_prefixes` from config.
   - `columns` (keys of `OPTIONAL_COLUMNS`; also `--columns`), `gpu_types` (extra GPU model names) and `theme` (TUI, default `auto`) are validated.
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
   - GPUs (`format_gpu_requests`): use the allocation in `tres_alloc_str` (typed via `gres_detail` if it only has a generic count) when present, else the scoped `tres_per_{job,node,socket,task}` requests, else `tres_req_str`. An untyped count takes its models from the job's constraint (`features`, e.g. `a100|a40&ssd_2tb`) through `gpu_types_from_features`/`GPU_MODEL_RE`.
   - Pending reasons become words plus a severity through `describe_reason` (`PENDING_REASONS`, QOS/Assoc limit names, `describe_dependency`); finished states through `FINISHED_STATES`. `_severity` ("error"/"warn"/"ok") drives highlighting and the summary's "will never start" lines.
   - `mem` comes from `tres_alloc_str`, else `tres_req_str`; `exit` from `exit_code`; `log` from `standard_output`. Elapsed/progress/left only exist for jobs still on a node.
   - `_raw` keeps the original job dict for the TUI detail and log views.
   - Times are relative to `now`, which is passed in (tests use a fixed `NOW`): `format_duration`, `format_age`, `format_when`, `progress_bar`.
   - Priority comes from sprio's best partition row, else squeue's `priority`. `format_factors` shows the weighted factors as shares.
5. **Render helpers** (Rich `Text`, used by the app): `render_summary`, `category_title`, `job_cell` (styled cells, the progress bar in colored segments). Styles use ANSI color names; the app maps them to the terminal's palette.
   - `classify_job` buckets jobs by `primary_state`. Flags in `STATE_FLAGS` override the base state, as squeue does.
   - `CATEGORIES` holds each category's columns, colour and sort keys. `sort_jobs` sorts by the `_`-prefixed raw keys from `normalize_job` (unset values last; `_priority` is negated so the highest comes first).
   - `category_fields` adds a User column with `--all`/`--user` and drops `OPTIONAL_COLUMNS` not in `columns`.
   - `fit_widths` gives each column its natural width, with `CAPPED` columns cut by `cell` (`CUT`: paths keep their end, job IDs their closing bracket, the rest their start). Columns never shrink to the window: wider tables scroll sideways, with thin scrollbars (`ThinScrollBarRender`). Styles come from `cell_style`.
   - To add a column, add a key in `normalize_job` and reference it in `CATEGORIES`.
   - Rows: `group_tasks`/`fold_tasks` fold tasks of one array job (`_array_id`) into a row with `_tasks`, unless the array is in `expanded`. `status_glyph` sets `glyph`/`_glyph_style`. `output_path` fills in Slurm filename patterns (`_output`, `_errors`).
   - Changes: `job_changes` compares two snapshots (started/ended), `describe_change` words them; the app notifies and, with `desktop_notifications`, sends OSC 9.
6. **App** (`tui.py`): `main` checks for a terminal and calls `run_tui` (`SjobsApp`): one `DataTable` per category (stacked tables line up their common leading columns through `align_widths`; tabs via `Tabs` when they do not fit, keys 1–3 switch), `JobTable` moves the cursor on into the next stacked table at either end, refreshed by a thread worker calling `fetch_snapshot` every `--interval` seconds (`MIN_INTERVAL` is 30; manual `r` is limited by `MIN_MANUAL_REFRESH`). A failed refresh keeps the last tables. Keys choose columns (`ColumnChooser`), switch mine/all, filter, show details (`detail_sections`), show a log tail (`run_tail`, on request) and cancel through `run_scancel`/`build_scancel_cmdline`, which only accepts IDs matching `CANCELABLE_JOB_ID_RE`.
   - Theme: before the app starts, `query_terminal_colors` asks for the terminal's foreground, background and 16 colors (OSC 10/11/4, ended by a device-attributes query every terminal answers). `terminal_themes` turns them into a Textual `Theme` (`terminal`) and a Rich ANSI palette; without an answer the app uses `textual-light`/`textual-dark`. The focused table's cursor row is a tint of `$primary`.

Each refresh starts exactly one process or SSH connection, which makes exactly one `squeue` call, to keep load on shared Slurm controllers low. A test asserts this.

## Tests

`tests/test_sjobs.py` and `tests/test_tui.py` (Textual Pilot, `IsolatedAsyncioTestCase`) run fully offline:
- `subprocess.run` and `shutil.which` are patched.
- `setUpModule` points `SJOBS_CONFIG` at a missing file and clears `SJOBS_HOST`, so the developer's own config never leaks in.
- `tests/fixtures/squeue.json`, `sshare.txt` and `sprio.txt` are synthetic fixtures modeled on Slurm 23.11 output. `script_output()` combines them into what the script prints.

## CI

- `.github/workflows/test.yml` runs on pull requests and is reused by the deploy workflow. A lint job runs `pixi run lint`; the test job runs the tests on the oldest and newest supported Python, then checks `sjobs --help` and `python -m sjobs --help`.
- `.github/workflows/deploy.yml` runs on pushes to `main` and on `v*` tags: test, then build.
  - Branch pushes only do a dry run.
  - A `v*` tag must match `version` in `pyproject.toml`. It pushes the artifacts to the `mvondomaros-lab/mvondomaros-lab.github.io` pages repo and opens a PR there.
