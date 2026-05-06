import json
import re
import shlex
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import click
import tabulate

REMOTE_HOST = "marc3a"
MAX_CWD_WIDTH = 56

USER_HOME = str(Path.home())

COMMON_PATH_PREFIXES = [
    f"{USER_HOME}/",
    "/mnt/nfs/home/",
    "/mnt/nfs/home2/",
    "/scratch_shared/",
    "/masc_shared/",
]

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


def build_squeue_args(show_all=False, username=None):
    """Build the squeue arguments."""
    args = ["squeue", "--json"]

    if username:
        return args + [f"--user={username}"]

    if show_all:
        return args

    return args + ["--me"]


def build_cmdline(show_all=False, username=None):
    """Build squeue command, using SSH if needed."""
    args = build_squeue_args(show_all=show_all, username=username)

    if shutil.which("squeue"):
        return args

    remote_command = shlex.join(args)

    return [
        "ssh",
        REMOTE_HOST,
        shlex.join(["sh", "-lc", remote_command]),
    ]


def run_squeue(show_all=False, username=None):
    """Run squeue and return stdout."""
    cmdline = build_cmdline(show_all=show_all, username=username)

    try:
        result = subprocess.run(
            cmdline,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() or exc.stdout.strip() or "unknown error"
        raise click.ClickException(
            f"Failed to fetch jobs with `{shlex.join(cmdline)}`: {details}"
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

    jobs = payload.get("jobs")

    if jobs is None:
        raise click.ClickException("squeue JSON output did not contain a 'jobs' field")

    if not isinstance(jobs, list):
        raise click.ClickException("squeue JSON field 'jobs' was not a list")

    return jobs


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
    except (TypeError, ValueError, OverflowError):
        return format_value(value)


def primary_state(job):
    """Return the primary Slurm job state."""
    states = job.get("job_state", [])
    state = states[0] if isinstance(states, list) and states else states
    return format_value(state)


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


def shorten_path(path, max_width=MAX_CWD_WIDTH):
    """Left-truncate long paths while preserving the path tail."""
    path = format_value(path)

    for prefix in sorted(COMMON_PATH_PREFIXES, key=len, reverse=True):
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
        item = item.strip()
        if not item.startswith("gres/gpu"):
            continue

        parts = item.split(":")

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


def normalize_job(job):
    """Normalize one squeue JSON job into display fields."""
    return {
        "job_id": format_value(job.get("job_id")),
        "cwd": shorten_path(job.get("current_working_directory")),
        "cpus": format_value(job.get("cpus")),
        "gpus": format_gpus(job.get("tres_per_job")),
        "state": primary_state(job),
        "nodes": format_value(job.get("nodes")),
        "end_time": format_timestamp(job.get("end_time")),
        "partition": format_value(job.get("partition")),
        "submit_time": format_timestamp(job.get("submit_time")),
        "reason": format_value(job.get("state_reason")),
        "dependency": format_value(job.get("dependency")),
    }


def fetch_jobs(show_all=False, username=None):
    """Fetch and normalize all jobs."""
    stdout = run_squeue(show_all=show_all, username=username)
    jobs = parse_squeue_json(stdout)
    return [normalize_job(job) for job in jobs]


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
def main(show_all, username):
    """Show Slurm jobs."""
    grouped = {category: [] for category in CATEGORIES}

    for job in fetch_jobs(show_all=show_all, username=username):
        grouped[classify_job(job)].append(job)

    for category in CATEGORIES:
        render_category(category, grouped[category])


if __name__ == "__main__":
    main()
