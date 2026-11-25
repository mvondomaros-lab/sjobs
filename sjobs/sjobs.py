import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from datetime import datetime
from pathlib import Path

import click
import tabulate

MAX_CWD_WIDTH = 56
# Stop a hung SSH connection or squeue instead of leaving it on the login node.
SQUEUE_TIMEOUT = 60
SLURM_NO_VAL = 0xFFFFFFFE

# Match squeue's display precedence when JSON includes both a base state and flags.
STATE_FLAGS = (
    "COMPLETING",
    "STAGE_OUT",
    "CONFIGURING",
    "RESIZING",
    "REQUEUED",
    "REQUEUE_FED",
    "REQUEUE_HOLD",
    "SPECIAL_EXIT",
    "STOPPED",
    "REVOKED",
    "RESV_DEL_HOLD",
    "SIGNALING",
)

HOST_ENV_VAR = "SJOBS_HOST"
CONFIG_ENV_VAR = "SJOBS_CONFIG"

CATEGORIES = {
    "RUNNING": {
        "fields": [
            ("job_id", "Job ID"),
            ("cwd", "CWD"),
            ("cpus", "CPUs"),
            ("gpus", "GPUs"),
            ("nodes", "Nodes"),
            ("end_time", "End Time"),
        ],
        "color": "green",
    },
    "PENDING": {
        "fields": [
            ("job_id", "Job ID"),
            ("cwd", "CWD"),
            ("cpus", "CPUs"),
            ("gpus", "GPUs"),
            ("partition", "Partition"),
            ("submit_time", "Submit Time"),
            ("reason", "Reason"),
            ("dependency", "Dependency"),
        ],
        "color": "cyan",
    },
    "OTHER": {
        "fields": [
            ("job_id", "Job ID"),
            ("cwd", "CWD"),
            ("cpus", "CPUs"),
            ("gpus", "GPUs"),
            ("partition", "Partition"),
            ("state", "State"),
            ("reason", "Reason"),
        ],
        "color": "red",
    },
}


def config_path():
    """Return the config file path, honoring SJOBS_CONFIG and XDG_CONFIG_HOME."""
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override).expanduser()

    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "sjobs" / "config.toml"


def load_config():
    """Load and validate the optional TOML config file."""
    path = config_path()

    try:
        with path.open("rb") as handle:
            config = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise click.ClickException(f"Failed to read config {path}: {exc}") from exc

    host = config.get("host")
    if host is not None and not (isinstance(host, str) and host):
        raise click.ClickException(
            f"Config {path}: 'host' must be a non-empty string"
        )

    prefixes = config.get("path_prefixes", [])
    if not (
        isinstance(prefixes, list)
        and all(isinstance(prefix, str) and prefix for prefix in prefixes)
    ):
        raise click.ClickException(
            f"Config {path}: 'path_prefixes' must be a list of non-empty strings"
        )

    return config


def resolve_host(cli_host=None, config=None):
    """Pick the SSH host: --host, then SJOBS_HOST, then the config file."""
    if cli_host:
        return cli_host

    env_host = os.environ.get(HOST_ENV_VAR)
    if env_host:
        return env_host

    return (config or {}).get("host")


def path_prefixes(config=None):
    """Prefixes stripped from working directories: local $HOME plus config."""
    prefixes = [f"{Path.home()}/"]
    for prefix in (config or {}).get("path_prefixes", []):
        prefixes.append(prefix if prefix.endswith("/") else f"{prefix}/")
    return prefixes


def build_squeue_args(show_all=False, username=None):
    """Build the squeue arguments."""
    args = ["squeue", "--json", "--states=all"]

    if username:
        return args + [f"--user={username}"]

    if show_all:
        return args

    return args + ["--me"]


def build_cmdline(show_all=False, username=None, host=None):
    """Build squeue command: SSH to host if one is set, else run locally."""
    args = build_squeue_args(show_all=show_all, username=username)

    if not host:
        if shutil.which("squeue"):
            return args
        raise click.ClickException(
            "squeue is not on PATH and no cluster host is configured. "
            f"Use --host, set {HOST_ENV_VAR}, or set 'host' in {config_path()}."
        )

    remote_command = shlex.join(args)

    return [
        "ssh",
        host,
        shlex.join(["sh", "-lc", remote_command]),
    ]


def run_squeue(show_all=False, username=None, host=None):
    """Run squeue and return stdout."""
    cmdline = build_cmdline(show_all=show_all, username=username, host=host)

    try:
        result = subprocess.run(
            cmdline,
            capture_output=True,
            text=True,
            check=True,
            timeout=SQUEUE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise click.ClickException(
            f"`{shlex.join(cmdline)}` did not finish within {SQUEUE_TIMEOUT} s"
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = (
            (exc.stderr or "").strip() or (exc.stdout or "").strip() or "unknown error"
        )
        raise click.ClickException(
            f"Failed to fetch jobs with `{shlex.join(cmdline)}`: {details}"
        ) from exc
    except OSError as exc:
        raise click.ClickException(
            f"Failed to run `{shlex.join(cmdline)}`: {exc}"
        ) from exc

    return result.stdout


def parse_squeue_json(stdout):
    """Parse JSON output from squeue."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f"Failed to parse squeue JSON output: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise click.ClickException("squeue JSON output was not an object")

    errors = payload.get("errors")
    if errors:
        if not isinstance(errors, list):
            errors = [errors]
        details = "; ".join(format_slurm_error(error) for error in errors)
        raise click.ClickException(f"squeue reported errors: {details}")

    jobs = payload.get("jobs")

    if jobs is None:
        raise click.ClickException("squeue JSON output did not contain a 'jobs' field")

    if not isinstance(jobs, list):
        raise click.ClickException("squeue JSON field 'jobs' was not a list")

    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise click.ClickException(
                f"squeue JSON job at index {index} was not an object"
            )

    return jobs


def format_slurm_error(error):
    """Retain both Slurm's error message and its diagnostic context."""
    if isinstance(error, dict):
        message = format_value(error.get("error"))
        description = format_value(error.get("description"))
        if message and description and message != description:
            return f"{message}: {description}"
        return message or description or format_value(error)
    return format_value(error)


def unwrap_slurm_value(value):
    """Unwrap common Slurm JSON scalar wrappers."""
    if not isinstance(value, dict):
        return value

    if {"set", "infinite", "number"}.issubset(value):
        if value.get("infinite"):
            return "infinite"
        if not value.get("set"):
            return ""
        return value.get("number")

    return value


def format_timestamp(value):
    """Format a Slurm epoch timestamp for display."""
    value = unwrap_slurm_value(value)

    if value in (None, "", 0):
        return ""

    if value == "infinite":
        return "infinite"

    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return format_value(value)


def primary_state(job):
    """Return the Slurm state displayed by squeue, including overriding flags."""
    states = job.get("job_state", [])
    if not isinstance(states, list):
        return format_value(states)

    for flag in STATE_FLAGS:
        if flag in states:
            return flag

    return format_value(states[0]) if states else ""


def format_job_id(job):
    """Preserve Slurm's array-task and heterogeneous-component identifiers."""
    array_id = unwrap_slurm_value(job.get("array_job_id"))
    if isinstance(array_id, int) and 0 < array_id < SLURM_NO_VAL:
        tasks = format_value(job.get("array_task_string"))
        if tasks:
            return f"{array_id}_[{tasks}]"

        task_id = unwrap_slurm_value(job.get("array_task_id"))
        if isinstance(task_id, int) and 0 <= task_id < SLURM_NO_VAL:
            return f"{array_id}_{task_id}"

    het_id = unwrap_slurm_value(job.get("het_job_id"))
    offset = unwrap_slurm_value(job.get("het_job_offset"))
    if (
        isinstance(het_id, int)
        and 0 < het_id < SLURM_NO_VAL
        and isinstance(offset, int)
        and 0 <= offset < SLURM_NO_VAL
    ):
        return f"{het_id}+{offset}"

    return format_value(job.get("job_id"))


def format_value(value):
    """Format a JSON value for table display."""
    value = unwrap_slurm_value(value)

    if value is None:
        return ""

    if isinstance(value, bool):
        return str(value).lower()

    if isinstance(value, (list, tuple)):
        return ",".join(format_value(item) for item in value)

    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)

    return str(value)


def shorten_path(path, prefixes=None, max_width=MAX_CWD_WIDTH):
    """Strip a known prefix, then left-truncate long paths keeping the tail."""
    path = format_value(path)

    if prefixes is None:
        prefixes = path_prefixes()

    for prefix in sorted(prefixes, key=len, reverse=True):
        if path.startswith(prefix):
            path = path.removeprefix(prefix)
            break

    if len(path) <= max_width:
        return path

    return "…" + path[-(max_width - 1) :]


def format_gpu_type(gpu_type):
    """Format a Slurm GPU type for display."""
    if not gpu_type:
        return "GPU"

    gpu_type = gpu_type.strip().lower()

    memory_match = re.search(r"_(\d+)gb$", gpu_type)
    memory = memory_match.group(1) if memory_match else None

    if memory:
        gpu_type = gpu_type[: memory_match.start()]

    name = gpu_type.replace("_", " ").upper()

    if memory:
        return f"{name} ({memory}GB)"

    return name


def format_gpus(tres_per_job):
    """Format GPU requests from a Slurm TRES string."""
    tres_per_job = format_value(tres_per_job)

    if not tres_per_job:
        return ""

    gpus = []

    for item in tres_per_job.split(","):
        parts = item.strip().split(":")
        if parts[0] not in {"gres/gpu", "gpu"}:
            continue

        if len(parts) == 2:
            gpu_type = ""
            count = parts[1]
        elif len(parts) >= 3:
            gpu_type = ":".join(parts[1:-1])
            count = parts[-1]
        else:
            continue

        gpus.append(f"{count} x {format_gpu_type(gpu_type)}")

    return ", ".join(gpus)


def format_gpu_totals(tres_str):
    """Format job-total GPUs from a TRES count string such as tres_alloc_str.

    Slurm lists a generic `gres/gpu=N` and, when known, typed
    `gres/gpu:<type>=N` entries; typed entries are preferred.
    """
    tres_str = format_value(tres_str)
    typed = []
    generic = None

    for item in tres_str.split(","):
        name, sep, count = item.strip().partition("=")
        if not sep:
            continue
        if name == "gres/gpu":
            generic = count
        elif name.startswith("gres/gpu:"):
            typed.append(f"{count} x {format_gpu_type(name.removeprefix('gres/gpu:'))}")

    if typed:
        return ", ".join(typed)
    if generic and generic != "0":
        return f"{generic} x GPU"
    return ""


def format_gpu_requests(job):
    """Show allocated GPUs when known, otherwise each request with its scope.

    The allocation (tres_alloc_str) covers every request form and names the
    GPU type actually assigned. Before allocation, scoped requests are shown
    without adding overlapping limits; tres_req_str is the last fallback.
    """
    allocated = format_gpu_totals(job.get("tres_alloc_str"))
    if allocated:
        return allocated

    requests = []
    for scope in ("job", "node", "socket", "task"):
        gpus = format_gpus(job.get(f"tres_per_{scope}"))
        if gpus:
            requests.append(gpus if scope == "job" else f"{gpus} / {scope}")
    if requests:
        return "; ".join(requests)

    return format_gpu_totals(job.get("tres_req_str"))


def normalize_job(job, prefixes=None):
    """Normalize one squeue JSON job into display fields."""
    return {
        "job_id": format_job_id(job),
        "cwd": shorten_path(job.get("current_working_directory"), prefixes),
        "cpus": format_value(job.get("cpus")),
        "gpus": format_gpu_requests(job),
        "state": primary_state(job),
        "nodes": format_value(job.get("nodes")),
        "end_time": format_timestamp(job.get("end_time")),
        "partition": format_value(job.get("partition")),
        "submit_time": format_timestamp(job.get("submit_time")),
        "reason": format_value(job.get("state_reason")),
        "dependency": format_value(job.get("dependency")),
    }


def fetch_jobs(show_all=False, username=None, host=None, prefixes=None):
    """Fetch and normalize all jobs."""
    stdout = run_squeue(show_all=show_all, username=username, host=host)
    jobs = parse_squeue_json(stdout)
    return [normalize_job(job, prefixes) for job in jobs]


def classify_job(job):
    """Return display category for a job."""
    state = job["state"].upper()

    if state in {"R", "RUNNING"}:
        return "RUNNING"

    if state in {"PD", "PENDING"}:
        return "PENDING"

    return "OTHER"


def render_category(name, jobs):
    """Render one category."""
    spec = CATEGORIES[name]

    rows = [[job.get(key, "") for key, _header in spec["fields"]] for job in jobs]

    if not rows:
        return

    click.echo(click.style(f"\n>>> {name}", fg=spec["color"], bold=True))
    click.echo()

    click.echo(
        tabulate.tabulate(
            rows,
            headers=[header for _key, header in spec["fields"]],
            tablefmt="simple",
            disable_numparse=True,
            colalign=tuple(
                "right" if key in {"job_id", "cpus"} else "left"
                for key, _header in spec["fields"]
            ),
        )
    )


@click.command()
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Show jobs for all users instead of only your own jobs.",
)
@click.option(
    "--user",
    "username",
    metavar="USERNAME",
    help="Show jobs for the specified user.",
)
@click.option(
    "--host",
    "-H",
    "host",
    metavar="HOST",
    help=f"SSH host to run squeue on (overrides {HOST_ENV_VAR} and the config file).",
)
def main(show_all, username, host):
    """Show Slurm jobs."""
    config = load_config()
    grouped = {category: [] for category in CATEGORIES}

    jobs = fetch_jobs(
        show_all=show_all,
        username=username,
        host=resolve_host(host, config),
        prefixes=path_prefixes(config),
    )
    for job in jobs:
        grouped[classify_job(job)].append(job)

    for category in CATEGORIES:
        render_category(category, grouped[category])


if __name__ == "__main__":
    main()
