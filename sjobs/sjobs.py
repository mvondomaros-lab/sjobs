import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

import click
from rich.text import Text

MAX_CWD_WIDTH = 56
MAX_NAME_WIDTH = 32
MAX_JOB_ID_WIDTH = 20
MAX_REASON_WIDTH = 48
# Stop a hung SSH connection or squeue instead of leaving it on the login node.
SQUEUE_TIMEOUT = 60
# sjobs never polls the Slurm controller more often than this.
MIN_INTERVAL = 30
DEFAULT_INTERVAL = 60
PROGRESS_WIDTH = 10
BAR_DONE, BAR_LEFT = "━", "─"
SLURM_NO_VAL = 0xFFFFFFFE
# Columns shown only on request (--columns, `columns` in the config, or `o` in
# the interactive view), with a short description for the column chooser.
OPTIONAL_COLUMNS = {
    "cwd": "working directory",
    "nodes": "node names",
    "partition": "partition",
    "priority": "priority and its weighted factors (pending)",
}
# Progress at or beyond this is highlighted: the job is about to hit its limit.
NEAR_LIMIT = 0.9
# Constraint features that look like GPU models: a100, a100_80gb, l40s, h200,
# rtxpro6000, mi250x. CPU or disk features (zen2, ssd_2tb, intel) do not match.
GPU_MODEL_RE = re.compile(
    r"(?:[ahlvpbt]\d{1,4}s?(?:_[\w.]+)?|rtx[\w.]*|gtx[\w.]*|mi\d+\w*|gh\d+)"
)
# Job IDs as displayed and as scancel accepts them: 12, 12_3, 12_[1-4,7], 12+1.
CANCELABLE_JOB_ID_RE = re.compile(r"\d+(?:_(?:\d+|\[[\d,\-%]+\]))?(?:\+\d+)?")

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
# Printed before squeue over SSH so the remote home directory (as $HOME and as
# its physical path, which Slurm records for working directories) arrives with
# the same call.
REMOTE_HOME_MARKER = "SJOBS_HOME="
# Starts the output of one tool in the script; squeue always comes last.
SECTION_MARKER = "SJOBS_SECTION="

SSHARE_FIELDS = (
    "Account",
    "User",
    "RawShares",
    "NormShares",
    "RawUsage",
    "EffectvUsage",
    "FairShare",
)
# Job ID, user, partition, weighted total, then the weighted factors:
# age, fair share, job size, partition, QOS, TRES, nice.
SPRIO_FORMAT = "%i|%u|%r|%Y|%A|%F|%J|%P|%Q|%T|%N"
SPRIO_FACTORS = {"age": 4, "fs": 5, "size": 6, "part": 7, "qos": 8}
FACTOR_NAMES = {
    "age": "waiting time",
    "fs": "fair share",
    "size": "job size",
    "part": "partition",
    "qos": "QOS",
    "tres": "TRES",
}

CATEGORIES = {
    "RUNNING": {
        "title": "Running",
        "fields": [
            ("glyph", " "),
            ("job_id", "Job ID"),
            ("name", "Name"),
            ("cpus", "CPUs"),
            ("mem", "Mem"),
            ("gpus", "GPUs"),
            ("nodes", "Nodes"),
            ("partition", "Partition"),
            ("cwd", "Path"),
            ("progress", "Progress"),
            ("time_left", "Left"),
        ],
        "color": "green",
        # Soonest to finish first.
        "sort": ("_end_time", "_job_id"),
    },
    "PENDING": {
        "title": "Pending",
        "fields": [
            ("glyph", " "),
            ("job_id", "Job ID"),
            ("name", "Name"),
            ("cpus", "CPUs"),
            ("mem", "Mem"),
            ("gpus", "GPUs"),
            ("time_limit", "Limit"),
            ("partition", "Partition"),
            ("cwd", "Path"),
            ("waiting", "Waiting"),
            ("est_start", "Starts"),
            ("priority", "Priority"),
            ("reason", "Why"),
        ],
        "color": "cyan",
        # Highest priority first, then oldest submission.
        "sort": ("_priority", "_submit_time", "_job_id"),
    },
    "OTHER": {
        "title": "Finished & other",
        "fields": [
            ("glyph", " "),
            ("job_id", "Job ID"),
            ("name", "Name"),
            ("state_label", "State"),
            ("exit", "Exit"),
            ("ran", "Ran"),
            ("ended", "Ended"),
            ("partition", "Partition"),
            ("cwd", "Path"),
            ("log", "Log"),
        ],
        "color": "magenta",
        # Most recently ended first.
        "sort": ("_ended", "_job_id"),
    },
}
RIGHT_ALIGNED = {"cpus", "mem", "time_left", "time_limit", "waiting", "ran", "exit"}
# Styles use only the terminal's 16 ANSI colors, bold and dim, so they follow
# whatever light or dark palette the terminal has.
SEVERITY_STYLES = {"error": "bold red", "warn": "yellow", "ok": "green"}

# Pending reasons as Slurm reports them, in words, with how serious they are:
# "error" means the job will not start without the user doing something.
PENDING_REASONS = {
    "Priority": ("behind higher-priority jobs", None),
    "Resources": ("waiting for free resources", None),
    "BeginTime": ("waiting for its begin time", None),
    "JobArrayTaskLimit": ("array task limit (%) reached", None),
    "Reservation": ("waiting for its reservation", None),
    "ReqNodeNotAvail": ("needed nodes are down, drained or reserved", "warn"),
    "JobHeldUser": ("held by you: scontrol release <id>", "warn"),
    "JobHeldAdmin": ("held by an administrator", "warn"),
    "launch failed requeued held": ("launch failed; requeued and held", "error"),
    "DependencyNeverSatisfied": ("dependency failed; will never start", "error"),
    "PartitionTimeLimit": ("time limit exceeds the partition's maximum", "error"),
    "PartitionNodeLimit": ("more nodes than the partition allows", "error"),
    "BadConstraints": ("constraints cannot be met", "error"),
    "InvalidQOS": ("invalid QOS", "error"),
    "InvalidAccount": ("invalid account", "error"),
    "None": ("", None),
}
LIMIT_RESOURCES = {
    "Cpu": "CPU",
    "GRES": "GPU",
    "Mem": "memory",
    "Node": "node",
    "Job": "job count",
    "Jobs": "job count",
    "Wall": "time",
    "Billing": "billing",
    "Submit": "submit",
}
FINISHED_STATES = {
    "COMPLETED": ("completed", "ok"),
    "FAILED": ("failed", "error"),
    "TIMEOUT": ("timed out", "error"),
    "OUT_OF_MEMORY": ("out of memory", "error"),
    "NODE_FAIL": ("node failed", "error"),
    "BOOT_FAIL": ("boot failed", "error"),
    "DEADLINE": ("missed deadline", "error"),
    "CANCELLED": ("cancelled", "warn"),
    "PREEMPTED": ("preempted", "warn"),
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
        raise click.ClickException(f"Config {path}: 'host' must be a non-empty string")

    prefixes = config.get("path_prefixes", [])
    if not (
        isinstance(prefixes, list)
        and all(isinstance(prefix, str) and prefix for prefix in prefixes)
    ):
        raise click.ClickException(
            f"Config {path}: 'path_prefixes' must be a list of non-empty strings"
        )

    notify = config.get("desktop_notifications", False)
    if not isinstance(notify, bool):
        raise click.ClickException(
            f"Config {path}: 'desktop_notifications' must be true or false"
        )

    theme = config.get("theme")
    if theme is not None and not (isinstance(theme, str) and theme):
        raise click.ClickException(f"Config {path}: 'theme' must be a non-empty string")

    for key, allowed in (("gpu_types", None), ("columns", list(OPTIONAL_COLUMNS))):
        values = config.get(key, [])
        if not (
            isinstance(values, list)
            and all(isinstance(value, str) and value for value in values)
        ):
            raise click.ClickException(
                f"Config {path}: '{key}' must be a list of non-empty strings"
            )
        unknown = [value for value in values if allowed and value not in allowed]
        if unknown:
            raise click.ClickException(
                f"Config {path}: unknown {key} {', '.join(unknown)} "
                f"(choose from {', '.join(allowed)})"
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


def local_homes():
    """The local home directory, as given and with symlinks resolved."""
    home = Path.home()
    return [str(home), str(home.resolve())]


def path_prefixes(config=None, homes=()):
    """Prefixes stripped from working directories: home directories plus config."""
    prefixes = []
    for prefix in [*homes, *(config or {}).get("path_prefixes", [])]:
        prefix = f"{prefix.rstrip('/')}/"
        if prefix != "/" and prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes


def build_squeue_args(show_all=False, username=None):
    """Build the squeue arguments."""
    args = ["squeue", "--json", "--states=all"]

    if username:
        return args + [f"--user={username}"]

    if show_all:
        return args

    return args + ["--me"]


def optional_section(name, command):
    """Script step that prints a section marker, then a tool's output or why not.

    Its stderr joins its stdout so a failure becomes a note, never an error.
    """
    tool = command.split()[0]
    marker = shlex.quote(SECTION_MARKER + name)
    return (
        f"printf '%s\\n' {marker}; "
        f"if command -v {tool} >/dev/null 2>&1; then {command} 2>&1; "
        f"else printf '%s\\n' '{tool} not found'; fi"
    )


def build_script(show_all=False, username=None):
    """Shell script: home markers, then sshare and sprio, then squeue.

    squeue runs last, so its exit status and stderr decide success and
    everything after its marker is its JSON.
    """
    user = f"-u {shlex.quote(username)}" if username else '-u "$(id -un)"'
    sprio_user = "" if show_all and not username else f" {user}"
    home = shlex.quote(REMOTE_HOME_MARKER)
    return "; ".join(
        [
            (
                f"printf '%s%s\\n%s%s\\n' {home} \"$HOME\" {home} "
                '"$(cd 2>/dev/null && pwd -P)"'
            ),
            optional_section(
                "sshare", f"sshare -P -n -U -o {','.join(SSHARE_FIELDS)} {user}"
            ),
            optional_section(
                "sprio", f"sprio -h -o {shlex.quote(SPRIO_FORMAT)}{sprio_user}"
            ),
            f"printf '%s\\n' {shlex.quote(SECTION_MARKER + 'squeue')}",
            shlex.join(build_squeue_args(show_all=show_all, username=username)),
        ]
    )


def build_cmdline(show_all=False, username=None, host=None):
    """Build the command: the script over SSH if a host is set, else locally."""
    script = build_script(show_all=show_all, username=username)

    if not host:
        if shutil.which("squeue"):
            return ["sh", "-c", script]
        raise click.ClickException(
            "squeue is not on PATH and no cluster host is configured. "
            f"Use --host, set {HOST_ENV_VAR}, or set 'host' in {config_path()}."
        )

    return ["ssh", host, shlex.join(["sh", "-lc", script])]


def run_command(cmdline, description, action):
    """Run a command with a timeout; every failure becomes a ClickException."""
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
            f"`{description}` did not finish within {SQUEUE_TIMEOUT} s"
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = (
            (exc.stderr or "").strip()
            or split_sections(exc.stdout or "")[2].strip()
            or "unknown error"
        )
        raise click.ClickException(
            f"Failed to {action} with `{description}`: {details}"
        ) from exc
    except OSError as exc:
        raise click.ClickException(f"Failed to run `{cmdline[0]}`: {exc}") from exc

    return result.stdout


def run_squeue(show_all=False, username=None, host=None):
    """Run the script and return stdout."""
    cmdline = build_cmdline(show_all=show_all, username=username, host=host)
    description = f"squeue on {host}" if host else "squeue"
    return run_command(cmdline, description, "fetch jobs")


def build_scancel_cmdline(job_id, host=None):
    """scancel for one displayed job ID, locally or over SSH.

    The ID must look like a Slurm job ID, so nothing else reaches a shell.
    """
    if not CANCELABLE_JOB_ID_RE.fullmatch(job_id or ""):
        raise click.ClickException(f"Not a job ID that scancel accepts: {job_id!r}")
    if not host:
        return ["scancel", job_id]
    return ["ssh", host, shlex.join(["sh", "-lc", shlex.join(["scancel", job_id])])]


def run_tail(path, host=None, lines=200):
    """The last lines of a job's output file, read where the job ran its tools."""
    command = ["tail", "-n", str(lines), "--", path]
    if host:
        command = ["ssh", host, shlex.join(["sh", "-lc", shlex.join(command)])]
    description = f"tail {path}" + (f" on {host}" if host else "")
    return run_command(command, description, "read the log")


def run_scancel(job_id, host=None):
    """Cancel one job; failures become a ClickException."""
    cmdline = build_scancel_cmdline(job_id, host)
    description = f"scancel {job_id}" + (f" on {host}" if host else "")
    run_command(cmdline, description, "cancel the job")


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


def strip_prefix(path, prefixes=None):
    """Remove the longest matching known prefix from a path."""
    path = format_value(path)

    if prefixes is None:
        prefixes = path_prefixes(homes=local_homes())

    for prefix in sorted(prefixes, key=len, reverse=True):
        if path.startswith(prefix):
            return path.removeprefix(prefix)

    return path


def truncate_left(text, max_width):
    """Left-truncate text to max_width characters, keeping the tail."""
    if len(text) <= max_width:
        return text

    return "…" + text[-(max_width - 1) :]


def shorten_path(path, prefixes=None, max_width=MAX_CWD_WIDTH):
    """Strip a known prefix, then left-truncate long paths keeping the tail."""
    return truncate_left(strip_prefix(path, prefixes), max_width)


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


def format_gres_detail(detail):
    """Typed GPUs from gres_detail (`gpu:a100:1(IDX:0)`, one entry per node)."""
    counts = {}
    for entry in detail if isinstance(detail, list) else []:
        match = re.match(r"gpu:([^:(]+):(\d+)", format_value(entry))
        if match:
            gpu_type, count = match.groups()
            counts[gpu_type] = counts.get(gpu_type, 0) + int(count)
    return ", ".join(
        f"{count} x {format_gpu_type(gpu_type)}" for gpu_type, count in counts.items()
    )


def gpu_types_from_features(features, known=()):
    """GPU models named in a job's constraint, e.g. `a100|a40&ssd_2tb`.

    Jobs that ask for an untyped `--gpus N` often pick the model this way.
    `known` adds model names that GPU_MODEL_RE does not recognize.
    """
    known = {name.lower() for name in known}
    models = []
    for token in re.split(r"[&|,\[\]()]", format_value(features)):
        token = re.sub(r"\*\d+$", "", token.strip()).lower()
        is_model = token in known or GPU_MODEL_RE.fullmatch(token)
        if token and is_model and token not in models:
            models.append(token)
    return models


def format_gpu_requests(job, gpu_types=()):
    """Show allocated GPUs when known, otherwise each request with its scope.

    The allocation (tres_alloc_str, else gres_detail) names the GPU type
    actually assigned. Before allocation, scoped requests are shown without
    adding overlapping limits; tres_req_str is the last fallback. An untyped
    count takes its models from the job's constraint, if it names any.
    """
    gpus = format_gpu_totals(job.get("tres_alloc_str"))
    if not gpus or " x GPU" in gpus:
        gpus = format_gres_detail(job.get("gres_detail")) or gpus

    if not gpus:
        requests = []
        for scope in ("job", "node", "socket", "task"):
            scoped = format_gpus(job.get(f"tres_per_{scope}"))
            if scoped:
                requests.append(scoped if scope == "job" else f"{scoped} / {scope}")
        gpus = "; ".join(requests) or format_gpu_totals(job.get("tres_req_str"))

    models = gpu_types_from_features(job.get("features"), gpu_types)
    if models and " x GPU" in gpus:
        names = "|".join(format_gpu_type(model) for model in models)
        gpus = re.sub(r" x GPU\b", f" x {names}", gpus)
    return gpus


def sort_number(value):
    """Return a positive Slurm number for sorting, or infinity if unset."""
    value = unwrap_slurm_value(value)
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value < SLURM_NO_VAL
    ):
        return value
    return float("inf")


def epoch(value):
    """Return a set Slurm timestamp in seconds, or None."""
    value = unwrap_slurm_value(value)
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value < SLURM_NO_VAL
    ):
        return value
    return None


def time_limit_seconds(value):
    """Slurm time limit (minutes) in seconds; math.inf if unlimited, None if unset."""
    value = unwrap_slurm_value(value)
    if value == "infinite":
        return math.inf
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value < SLURM_NO_VAL
    ):
        return value * 60
    return None


def format_duration(seconds):
    """Compact duration: 45m, 3h12m, 1d04h."""
    if seconds is None:
        return ""
    if seconds == math.inf:
        return "unlimited"
    minutes = int(max(seconds, 0) // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def format_age(timestamp, now):
    """How long ago a timestamp was, e.g. `2h05m ago`."""
    if timestamp is None or timestamp > now:
        return ""
    return f"{format_duration(now - timestamp)} ago"


def format_until(timestamp, now):
    """How long until a timestamp: `in 3h12m`, `now`; "" if unknown or past."""
    if timestamp is None or timestamp <= now:
        return ""
    if timestamp - now < 60:
        return "now"
    return f"in {format_duration(timestamp - now)}"


def format_when(timestamp, now):
    """Timestamp relative to today: `today 18:05`, `Thu 09:12`, else the date."""
    if timestamp is None:
        return ""
    try:
        when = datetime.fromtimestamp(timestamp)
        today = datetime.fromtimestamp(now).date()
    except (OverflowError, OSError, ValueError):
        return format_value(timestamp)
    days = (when.date() - today).days
    if days == 0:
        return f"today {when:%H:%M}"
    if days == 1:
        return f"tomorrow {when:%H:%M}"
    if days == -1:
        return f"yesterday {when:%H:%M}"
    if abs(days) < 7:
        return f"{when:%a %H:%M}"
    return f"{when:%Y-%m-%d %H:%M}"


def progress_bar(fraction, width=PROGRESS_WIDTH):
    """A thin bar plus percentage, e.g. `━━━───────  25%`."""
    fraction = min(max(fraction, 0.0), 1.0)
    filled = round(fraction * width)
    return (
        f"{BAR_DONE * filled}{BAR_LEFT * (width - filled)} {round(fraction * 100):3d}%"
    )


def gpu_count(tres_str):
    """Number of GPUs in a TRES count string such as tres_alloc_str."""
    generic = 0
    typed = 0
    for item in format_value(tres_str).split(","):
        name, sep, count = item.strip().partition("=")
        if not sep or not count.isdigit():
            continue
        if name == "gres/gpu":
            generic = int(count)
        elif name.startswith("gres/gpu:"):
            typed += int(count)
    return generic or typed


def first_line(text):
    """First non-blank line of text, shortened for a note; None if blank."""
    for line in (text or "").splitlines():
        if line.strip():
            return truncate_right(line.strip(), 80)
    return None


def parse_sshare(text):
    """Parse `sshare -P -n -U -o SSHARE_FIELDS` rows into (rows, problem).

    `problem` is a short reason when the text held output but no rows.
    """
    rows = []
    for line in (text or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) != len(SSHARE_FIELDS) or not parts[1]:
            continue
        try:
            fairshare = float(parts[6])
        except ValueError:
            continue
        rows.append({"account": parts[0], "user": parts[1], "fairshare": fairshare})
    return rows, None if rows else first_line(text)


def parse_sprio(text):
    """Parse `sprio -h -o SPRIO_FORMAT` rows into ({job_id: entry}, problem).

    A job pending in several partitions keeps its highest-priority row.
    """
    priorities = {}
    for line in (text or "").splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != SPRIO_FORMAT.count("|") + 1:
            continue
        job_id = re.match(r"\d+", parts[0])
        if not job_id:
            continue
        try:
            priority = float(parts[3])
            factors = {
                name: float(parts[index] or 0) for name, index in SPRIO_FACTORS.items()
            }
        except ValueError:
            continue
        factors["tres"] = sum(map(float, re.findall(r"=([\d.]+)", parts[9])))
        job_id = int(job_id.group())
        if job_id not in priorities or priority > priorities[job_id]["priority"]:
            priorities[job_id] = {"priority": priority, "factors": factors}
    return priorities, None if priorities else first_line(text)


def format_factors(factors):
    """Each factor's share of the priority, largest first.

    `waiting time 75%, fair share 20%`.
    """
    total = sum(value for value in factors.values() if value > 0)
    if total <= 0:
        return ""
    shares = sorted(
        (-round(100 * value / total), name) for name, value in factors.items()
    )
    return ", ".join(
        f"{FACTOR_NAMES.get(name, name)} {-share}%"
        for share, name in shares
        if share <= -1
    )


def tres_memory_mb(tres_str):
    """Memory in MB from a TRES string such as `cpu=4,mem=16G`; None if absent."""
    for item in format_value(tres_str).split(","):
        name, _sep, value = item.strip().partition("=")
        match = re.fullmatch(r"([\d.]+)([KMGT]?)", value) if name == "mem" else None
        if match:
            scale = {"K": 1 / 1024, "": 1, "M": 1, "G": 1024, "T": 1024**2}
            return float(match.group(1)) * scale[match.group(2)]
    return None


def format_memory(mb):
    """`512M`, `3.9G`, `16G`, `1.5T`."""
    if mb is None:
        return ""
    for unit, size in (("T", 1024**2), ("G", 1024)):
        if mb >= size:
            value = mb / size
            return (
                f"{value:.0f}{unit}"
                if value >= 10 or value.is_integer()
                else (f"{value:.1f}{unit}")
            )
    return f"{mb:.0f}M"


def format_exit_code(code):
    """Exit code of a finished job: `0`, `1`, `SIGKILL`; "" if unknown."""
    if isinstance(code, dict):
        signal = code.get("signal") or {}
        name = format_value(signal.get("name"))
        if name:
            return name
        signal_id = unwrap_slurm_value(signal.get("id"))
        if isinstance(signal_id, int) and 0 < signal_id < SLURM_NO_VAL:
            return f"signal {signal_id}"
        code = code.get("return_code")
    code = unwrap_slurm_value(code)
    if isinstance(code, int) and not isinstance(code, bool) and code < SLURM_NO_VAL:
        return str(code)
    return ""


def describe_dependency(dependency):
    """`afterok:12_*(unfulfilled),afterany:13` -> `after 12_* ok, after 13`."""
    kinds = {"afterok": "ok", "afternotok": "fails", "aftercorr": "ok, per task"}
    parts = []
    for item in format_value(dependency).split(","):
        item = re.sub(r"\((unfulfilled|failed)\)$", "", item.strip())
        kind, _sep, ids = item.partition(":")
        if not ids:
            if item:
                parts.append(item)
            continue
        ids = ids.replace(":", ", ")
        suffix = kinds.get(kind)
        parts.append(f"after {ids} {suffix}" if suffix else f"after {ids}")
    return ", ".join(parts)


def describe_reason(reason, dependency=""):
    """A pending reason in words, and its severity (None, "warn" or "error")."""
    reason = format_value(reason)
    if reason == "Dependency":
        return f"waits for: {describe_dependency(dependency)}", None
    if reason == "DependencyNeverSatisfied" and dependency:
        text = describe_dependency(dependency)
        return f"dependency failed ({text}); will never start", "error"
    if reason in PENDING_REASONS:
        return PENDING_REASONS[reason]

    match = re.fullmatch(
        r"(QOS|Assoc)(Grp|Max)([A-Za-z]+?)(PerUser|PerAccount|PerJob|PerNode)?"
        r"(RunMinutes|Minutes)?(Limit)?",
        reason,
    )
    if match and match.group(3) in LIMIT_RESOURCES:
        owner, kind, resource, scope = match.group(1, 2, 3, 4)
        resource = LIMIT_RESOURCES[resource]
        if match.group(5):
            resource = f"{resource}-minutes"
        whose = {
            "PerUser": "your",
            "PerAccount": "the account's",
            "PerJob": "per-job",
            "PerNode": "per-node",
        }.get(scope, "the group's" if kind == "Grp" else "the")
        where = "QOS" if owner == "QOS" else "account"
        return f"{whose} {resource} limit ({where}) reached", None
    return reason, None


# Status markers in front of each row, by category and severity.
GLYPHS = {
    "RUNNING": ("●", "green"),
    "PENDING": ("○", "dim"),
    "error": ("✗", "bold red"),
    "warn": ("!", "yellow"),
    "ok": ("✓", "green"),
    "OTHER": ("·", "dim"),
}


def status_glyph(category, severity):
    """The marker and its style for a job: ● running, ○ pending, ✓ done, …"""
    if category == "PENDING" and severity:
        return GLYPHS[severity if severity == "error" else "warn"]
    if category == "OTHER" and severity:
        return GLYPHS[severity]
    return GLYPHS[category]


def normalize_job(job, prefixes=None, now=None, priorities=None, gpu_types=()):
    """Normalize one squeue JSON job into display fields.

    `cwd` keeps the full prefix-stripped path; rendering truncates it to fit.
    `priorities` maps job IDs to parsed sprio entries; `gpu_types` adds GPU
    model names to look for in constraints.
    Keys starting with an underscore are sort keys, totals and the raw job
    (`_raw`), not display fields.
    """
    now = time.time() if now is None else now
    start = epoch(job.get("start_time"))
    end = epoch(job.get("end_time"))
    submit = epoch(job.get("submit_time"))
    limit = time_limit_seconds(job.get("time_limit"))
    state = primary_state(job)
    pending = state.upper() in {"PD", "PENDING"}
    finished = state.upper() in FINISHED_STATES
    # Elapsed time, progress and time left describe jobs that are still on a node.
    elapsed = (
        now - start if start is not None and start <= now and not finished else None
    )

    if elapsed is None:
        left = None
    elif end is not None:
        left = max(end - now, 0)
    elif limit is not None and elapsed is not None:
        left = max(limit - elapsed, 0)
    else:
        left = None

    progress = ""
    if elapsed is not None and limit not in (None, math.inf):
        progress = progress_bar(elapsed / limit)

    raw_id = unwrap_slurm_value(job.get("job_id"))
    sprio = (priorities or {}).get(raw_id)
    if sprio:
        priority = sprio["priority"]
    else:
        priority = unwrap_slurm_value(job.get("priority"))
        if not isinstance(priority, (int, float)) or isinstance(priority, bool):
            priority = None
    cpus = unwrap_slurm_value(job.get("cpus"))

    if pending:
        reason, severity = describe_reason(
            job.get("state_reason"), job.get("dependency")
        )
    else:
        reason, severity = format_value(job.get("state_reason")), None
        if reason == "None":
            reason = ""
    state_label, state_severity = FINISHED_STATES.get(
        state.upper(), (state.lower().replace("_", " "), None)
    )
    memory = tres_memory_mb(job.get("tres_alloc_str"))
    if memory is None:
        memory = tres_memory_mb(job.get("tres_req_str"))
    fraction = elapsed / limit if elapsed is not None and limit else None

    priority_text = "" if priority is None else str(int(priority))
    if sprio and format_factors(sprio["factors"]):
        priority_text += f" ({format_factors(sprio['factors'])})"

    normalized = {
        "job_id": format_job_id(job),
        "name": format_value(job.get("name")),
        "user": format_value(job.get("user_name")),
        "cwd": strip_prefix(job.get("current_working_directory"), prefixes),
        "log": strip_prefix(output_path(job, "standard_output"), prefixes),
        "_output": output_path(job, "standard_output"),
        "_errors": output_path(job, "standard_error"),
        "cpus": format_value(job.get("cpus")),
        "mem": format_memory(memory),
        "gpus": format_gpu_requests(job, gpu_types),
        "state": state,
        "state_label": state_label,
        "nodes": format_value(job.get("nodes")),
        "partition": format_value(job.get("partition")),
        "elapsed": format_duration(elapsed),
        "progress": progress,
        "time_left": format_duration(math.inf if limit == math.inf else left),
        "end_time": format_when(end, now),
        "time_limit": format_duration(limit),
        "submitted": format_when(submit, now),
        "waiting": format_duration(now - submit) if submit and pending else "",
        "est_start": format_until(start, now) if pending else "",
        "est_start_at": format_when(start, now) if start and start > now else "",
        "priority": priority_text,
        "reason": reason,
        "exit": format_exit_code(job.get("exit_code")) if finished else "",
        "ran": format_duration(end - start) if finished and start and end else "",
        "ended": format_age(end, now) if finished else "",
        "_job_id": sort_number(job.get("job_id")),
        "_end_time": sort_number(job.get("end_time")),
        "_ended": -end if end is not None else float("inf"),
        "_submit_time": sort_number(job.get("submit_time")),
        "_priority": -priority if priority is not None else float("inf"),
        "_cpus": cpus if isinstance(cpus, int) and not isinstance(cpus, bool) else 0,
        "_gpus": gpu_count(job.get("tres_alloc_str")),
        "_mem": memory or 0,
        "_fraction": fraction,
        "_severity": severity if pending else state_severity,
        "_array_id": array_task_key(job),
        "_raw": job,
    }
    category = classify_job(normalized)
    normalized["glyph"], normalized["_glyph_style"] = status_glyph(
        category, normalized["_severity"]
    )
    if category == "RUNNING" and (fraction or 0) >= NEAR_LIMIT:
        normalized["_glyph_style"] = "yellow"
    return normalized


def output_path(job, key):
    """A job's output or error file, with Slurm's filename patterns filled in.

    squeue usually reports the expanded path, but not always (`out.%J`).
    """
    path = format_value(job.get(key))
    if "%" not in path:
        return path
    array_id = unwrap_slurm_value(job.get("array_job_id"))
    task_id = unwrap_slurm_value(job.get("array_task_id"))
    # The first node of a list such as `gpu[01-02],cpu03`.
    first = re.match(
        r"([^\[,]*)(?:\[(\d+))?",
        format_value(job.get("nodes") or job.get("batch_host")),
    )
    values = {
        "j": format_value(job.get("job_id")),
        "J": format_value(job.get("job_id")),
        "A": format_value(array_id) if array_id else format_value(job.get("job_id")),
        "a": format_value(task_id) if task_id not in (None, "") else "4294967294",
        "x": format_value(job.get("name")),
        "u": format_value(job.get("user_name")),
        "N": first.group(1) + (first.group(2) or ""),
        "n": "0",
        "t": "0",
        "%": "%",
    }

    def fill(match):
        value = values.get(match.group(2))
        if value is None:
            return match.group(0)
        width = match.group(1)
        return value.zfill(int(width)) if width and value.isdigit() else value

    return re.sub(r"%(\d*)([AaJjNntux%])", fill, path)


def array_task_key(job):
    """(array job ID, task ID) of a single array task, else None."""
    array_id = unwrap_slurm_value(job.get("array_job_id"))
    task_id = unwrap_slurm_value(job.get("array_task_id"))
    if (
        isinstance(array_id, int)
        and 0 < array_id < SLURM_NO_VAL
        and isinstance(task_id, int)
        and 0 <= task_id < SLURM_NO_VAL
        and not format_value(job.get("array_task_string"))
    ):
        return array_id, task_id
    return None


def task_ranges(ids):
    """[1, 2, 3, 7] -> `1-3,7`."""
    ids = sorted(set(ids))
    parts = []
    start = previous = ids[0]
    for task in [*ids[1:], None]:
        if task is not None and task == previous + 1:
            previous = task
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        if task is not None:
            start = previous = task
    return ",".join(parts)


SEVERITY_ORDER = {"error": 3, "warn": 2, "ok": 1, None: 0}


def group_tasks(name, jobs, expanded=()):
    """A category's rows, with tasks of the same array job folded into one row.

    Arrays whose ID is in `expanded` keep one row per task. A folded row has
    `_tasks` (its jobs, in display order) and sums or summarizes their fields.
    """
    arrays = {}
    for job in jobs:
        key = job.get("_array_id")
        if key and key[0] not in expanded:
            arrays.setdefault(key[0], []).append(job)
    rows = []
    folded = set()
    for job in jobs:
        key = job.get("_array_id")
        tasks = arrays.get(key[0]) if key else None
        if not tasks or len(tasks) < 2:
            rows.append(job)
        elif key[0] not in folded:
            folded.add(key[0])
            rows.append(fold_tasks(name, key[0], sort_jobs(name, tasks)))
    return rows


def fold_tasks(name, array_id, tasks):
    """One row standing for several tasks of an array job."""
    first = tasks[0]
    row = dict(first)
    count = len(tasks)
    row["job_id"] = f"{array_id}_[{task_ranges(t['_array_id'][1] for t in tasks)}]"
    row["name"] = f"{first['name']} ×{count}"
    row["cpus"] = str(sum(task["_cpus"] for task in tasks))
    row["_cpus"] = sum(task["_cpus"] for task in tasks)
    row["_mem"] = sum(task["_mem"] for task in tasks)
    row["mem"] = format_memory(row["_mem"])
    row["_gpus"] = sum(task["_gpus"] for task in tasks)
    row["gpus"] = f"{first['gpus']} each" if first["gpus"] else ""
    row["nodes"] = ",".join(dict.fromkeys(t["nodes"] for t in tasks if t["nodes"]))
    fractions = [t["_fraction"] for t in tasks if t["_fraction"] is not None]
    if fractions and name == "RUNNING":
        row["progress"] = progress_bar(sum(fractions) / len(fractions))
        row["_fraction"] = max(fractions)
    if name == "OTHER":
        counts = {}
        for task in tasks:
            counts[task["state_label"]] = counts.get(task["state_label"], 0) + 1
        row["state_label"] = ", ".join(f"{n} {label}" for label, n in counts.items())
        row["exit"] = row["ran"] = row["log"] = ""
    row["_severity"] = max(
        (task["_severity"] for task in tasks), key=SEVERITY_ORDER.__getitem__
    )
    row["_tasks"] = tasks
    row["_array_id"] = None
    return row


def job_key(job):
    """A job's identity across refreshes."""
    return job["job_id"]


def job_changes(old_jobs, new_jobs):
    """What happened between two refreshes: [(job, event, severity)].

    Events are "started" and "ended"; jobs seen for the first time count when
    they are already running or finished (they changed between refreshes).
    """
    before = {job_key(job): classify_job(job) for job in old_jobs}
    changes = []
    for job in new_jobs:
        now, was = classify_job(job), before.get(job_key(job))
        if now == was or (was is None and now == "PENDING"):
            continue
        if now == "RUNNING":
            changes.append((job, "started", None))
        elif now == "OTHER" and job["state"].upper() in FINISHED_STATES:
            changes.append((job, "ended", job["_severity"]))
    return changes


def describe_change(job, event):
    """`1234 md-run started on gpu07`, `1234 md-run failed (exit 1)`."""
    name = f"{job['job_id']} {job['name']}".strip()
    if event == "started":
        return f"{name} started" + (f" on {job['nodes']}" if job["nodes"] else "")
    detail = f" (exit {job['exit']})" if job["exit"] not in ("", "0") else ""
    return f"{name} {job['state_label']}{detail}"


def split_sections(stdout):
    """Split the script output into (home directories, {section: text}, JSON).

    Anything a login profile prints before the first marker is dropped.
    Everything after the squeue marker is its JSON. Without markers, the homes
    are unknown and the whole output is treated as JSON.
    """
    lines = stdout.splitlines(keepends=True)
    homes = []
    sections = {}
    current = None
    last = None
    for index, line in enumerate(lines):
        if line.startswith(REMOTE_HOME_MARKER):
            home = line.removeprefix(REMOTE_HOME_MARKER).rstrip("\r\n")
            if home and home not in homes:
                homes.append(home)
            current, last = None, index
        elif line.startswith(SECTION_MARKER):
            name = line.removeprefix(SECTION_MARKER).strip()
            if name == "squeue":
                return homes, sections, "".join(lines[index + 1 :])
            current, last = name, index
            sections[current] = ""
        elif current is not None:
            sections[current] += line
    if last is None:
        return [], {}, stdout
    return homes, sections, "".join(lines[last + 1 :]) if current is None else ""


def fetch_snapshot(show_all=False, username=None, host=None, config=None, now=None):
    """Fetch and normalize all jobs plus fair share and priority data.

    Returns a dict with `jobs`, `shares` and per-tool `problems`.
    """
    now = time.time() if now is None else now
    stdout = run_squeue(show_all=show_all, username=username, host=host)
    homes, sections, stdout = split_sections(stdout)
    if not homes and not host:
        homes = local_homes()
    jobs = parse_squeue_json(stdout)
    priorities, sprio_problem = parse_sprio(sections.get("sprio"))
    shares, sshare_problem = parse_sshare(sections.get("sshare"))
    prefixes = path_prefixes(config, homes)
    gpu_types = (config or {}).get("gpu_types", ())
    return {
        "jobs": [
            normalize_job(job, prefixes, now, priorities, gpu_types) for job in jobs
        ],
        "shares": shares,
        "problems": {"sshare": sshare_problem, "sprio": sprio_problem},
    }


def classify_job(job):
    """Return display category for a job."""
    state = job["state"].upper()

    if state in {"R", "RUNNING"}:
        return "RUNNING"

    if state in {"PD", "PENDING"}:
        return "PENDING"

    return "OTHER"


def sort_jobs(name, jobs):
    """Sort a category's jobs by its sort keys (stable for ties)."""
    keys = CATEGORIES[name]["sort"]
    return sorted(
        jobs, key=lambda job: tuple(job.get(key, float("inf")) for key in keys)
    )


def truncate_right(text, max_width):
    """Right-truncate text to max_width characters, keeping the start."""
    if len(text) <= max_width:
        return text

    return text[: max_width - 1] + "…"


# Columns cut to a maximum width, and how: paths keep their end, the rest
# their start. Wider tables scroll sideways.
CAPPED = {
    "job_id": MAX_JOB_ID_WIDTH,
    "cwd": MAX_CWD_WIDTH,
    "log": MAX_CWD_WIDTH,
    "name": MAX_NAME_WIDTH,
    "reason": MAX_REASON_WIDTH,
}


def truncate_job_id(text, max_width):
    """Shorten an array task list, keeping the job ID and closing bracket."""
    if len(text) <= max_width:
        return text
    if text.endswith("]") and "[" in text:
        return text[: max_width - 2] + "…]"
    return truncate_right(text, max_width)


CUT = {"cwd": truncate_left, "log": truncate_left, "job_id": truncate_job_id}


def category_fields(name, show_user=False, columns=()):
    """A category's columns, with a User column after Name when requested.

    Columns in OPTIONAL_COLUMNS appear only when listed in `columns`.
    """
    fields = [
        (key, header)
        for key, header in CATEGORIES[name]["fields"]
        if key not in OPTIONAL_COLUMNS or key in columns
    ]
    if show_user:
        index = next(i for i, (key, _header) in enumerate(fields) if key == "name")
        fields.insert(index + 1, ("user", "User"))
    return fields


def cell(job, key, widths):
    """One plain-text table cell, cut to its width if it has one."""
    value = job.get(key, "")
    if key in widths and len(value) > widths[key]:
        return CUT.get(key, truncate_right)(value, widths[key])
    return value


def cell_style(job, key):
    """Highlight what needs attention: failures, blocked jobs, near limits."""
    if key == "glyph":
        return job.get("_glyph_style", "")
    severity = job.get("_severity")
    if severity and key in ("reason", "state_label"):
        return SEVERITY_STYLES[severity]
    if severity == "error" and key == "exit":
        return SEVERITY_STYLES[severity]
    if key in ("progress", "time_left") and (job.get("_fraction") or 0) >= NEAR_LIMIT:
        return "yellow"
    return ""


def job_cell(job, key, widths=CAPPED):
    """A styled cell for Rich tables and the interactive view."""
    if key == "progress" and job.get(key):
        # Done part in color (yellow near the limit), the rest faint.
        bar, percent = job[key][:PROGRESS_WIDTH], job[key][PROGRESS_WIDTH:]
        filled = bar.count(BAR_DONE)
        return Text.assemble(
            (bar[:filled], cell_style(job, key) or "green"),
            (bar[filled:], "dim"),
            (percent, cell_style(job, key)),
            no_wrap=True,
        )
    return Text(
        cell(job, key, widths),
        style=cell_style(job, key),
        justify="right" if key in RIGHT_ALIGNED else "left",
        no_wrap=True,
    )


def fit_widths(fields, jobs):
    """Each column's width: its widest cell or header, CAPPED columns cut."""
    return {
        key: max([len(header)] + [len(cell(job, key, CAPPED)) for job in jobs])
        for key, header in fields
    }


def align_widths(tables):
    """Column widths for tables shown one above the other, lined up.

    `tables` maps a name to (fields, rows). A column at the same position, with
    the same columns before it, in several tables gets one width in all of
    them. Returns {name: {key: width}}.
    """
    keys = {
        name: [key for key, _header in fields] for name, (fields, _) in tables.items()
    }
    group_of = {}
    for name in tables:
        for position, key in enumerate(keys[name]):
            prefix = keys[name][: position + 1]
            members = frozenset(
                other for other in tables if keys[other][: position + 1] == prefix
            )
            group_of[name, key] = (position, members) if len(members) > 1 else name
    width = {}
    for name, (fields, rows) in tables.items():
        for key, natural in fit_widths(fields, rows).items():
            group = (group_of[name, key], key)
            width[group] = max(width.get(group, 0), natural)
    return {
        name: {key: width[group_of[name, key], key] for key in keys[name]}
        for name in tables
    }


def category_title(name, shown, total=None):
    """`Running  3`, or `Pending  2 of 12` when filtered."""
    spec = CATEGORIES[name]
    count = f"{shown} of {total}" if total is not None and total != shown else shown
    return Text.assemble(
        (spec["title"], f"bold {spec['color']}"), (f"  {count}", "dim")
    )


def fairshare_verdict(value):
    """A word and a color for a fair share value."""
    if value >= 0.5:
        return "good", "green"
    if value >= 0.2:
        return "fair", "yellow"
    return "low", "red"


def plural(count, noun):
    """`1 GPU`, `2 GPUs`."""
    return f"{count} {noun}{'' if count == 1 else 's'}"


def render_title(host, now):
    """`sjobs · HOST · Wed 14:32`."""
    when = datetime.fromtimestamp(now).strftime("%a %H:%M")
    return Text.assemble(("sjobs", "bold"), f" · {host or 'local'} · {when}")


def attention_lines(grouped, limit=3, own=None):
    """Pending jobs that cannot start without the user doing something.

    `own` limits them to these users' jobs.
    """
    blocked = [
        job
        for job in grouped["PENDING"]
        if job["_severity"] == "error" and (own is None or job["user"] in own)
    ]
    lines = [
        Text.assemble(
            ("! ", "bold red"),
            (job["job_id"], "bold"),
            f" {job['name']}: ",
            (job["reason"], "red"),
        )
        for job in blocked[:limit]
    ]
    if len(blocked) > limit:
        lines.append(Text(f"  and {len(blocked) - limit} more", style="red"))
    return lines


def render_summary(grouped, snapshot, host, now, show_user=False):
    """One header line, then blocked jobs and notes about missing data."""
    line = Text.assemble(("sjobs", "bold"), (f"  {host or 'local'}", "dim"))
    running = grouped["RUNNING"]
    if running:
        totals = [plural(sum(job["_cpus"] for job in running), "CPU")]
        memory = sum(job["_mem"] for job in running)
        if memory:
            totals.append(format_memory(memory))
        gpus = sum(job["_gpus"] for job in running)
        if gpus:
            totals.append(plural(gpus, "GPU"))
        line.append_text(
            Text.assemble(
                "   ",
                (GLYPHS["RUNNING"][0], GLYPHS["RUNNING"][1]),
                f" {len(running)} running ",
                (f"{' · '.join(totals)}", "dim"),
            )
        )
    if grouped["PENDING"]:
        line.append(f"   {GLYPHS['PENDING'][0]} {len(grouped['PENDING'])} pending")
    if grouped["OTHER"]:
        line.append(f"   · {len(grouped['OTHER'])} finished")
    if not any(grouped.values()):
        line.append("   no jobs", style="dim")
    shares = snapshot["shares"]
    for row in shares:
        verdict, color = fairshare_verdict(row["fairshare"])
        label = f" {row['account']}" if len(shares) > 1 else ""
        line.append_text(
            Text.assemble(
                ("   fair share", "dim"),
                label,
                " ",
                (f"{row['fairshare']:.3f} {verdict}", f"bold {color}"),
            )
        )
    line.append(f"   {datetime.fromtimestamp(now):%H:%M}", style="dim")
    # With other users' jobs shown, only your own blocked jobs are news.
    own = {row["user"] for row in shares} if show_user else None
    lines = [line, *attention_lines(grouped, own=own)]

    problems = snapshot["problems"]
    notes = []
    if problems.get("sshare"):
        notes.append(f"fair share unavailable: {problems['sshare']}")
    # sprio reports an error when there is nothing pending; only then is it news.
    if problems.get("sprio") and grouped["PENDING"]:
        notes.append(f"priority factors unavailable: {problems['sprio']}")
    lines.extend(Text(note, style="dim") for note in notes)
    return Text("\n").join(lines)


def group_jobs(jobs):
    """Jobs by category, in CATEGORIES order."""
    grouped = {category: [] for category in CATEGORIES}
    for job in jobs:
        grouped[classify_job(job)].append(job)
    return grouped


def parse_columns(_ctx, _param, value):
    """`cwd,nodes` -> ("cwd", "nodes"); `all` selects every optional column."""
    if not value:
        return ()
    names = [name.strip() for name in value.split(",") if name.strip()]
    if "all" in names:
        return tuple(OPTIONAL_COLUMNS)
    unknown = [name for name in names if name not in OPTIONAL_COLUMNS]
    if unknown:
        raise click.BadParameter(
            f"unknown {', '.join(unknown)} (choose from "
            f"{', '.join(OPTIONAL_COLUMNS)}, or all)"
        )
    return tuple(names)


@click.command()
@click.version_option(package_name="sjobs")
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
@click.option(
    "--columns",
    "-c",
    "extra_columns",
    metavar="LIST",
    callback=parse_columns,
    help=f"Extra columns, comma-separated: {', '.join(OPTIONAL_COLUMNS)}, or all.",
)
@click.option(
    "--interval",
    type=click.IntRange(min=MIN_INTERVAL),
    default=DEFAULT_INTERVAL,
    show_default=True,
    metavar="SECONDS",
    help=f"Seconds between refreshes (at least {MIN_INTERVAL}, to keep load on "
    "the Slurm controller low).",
)
def main(show_all, username, host, extra_columns, interval):
    """Watch your Slurm jobs, why pending jobs wait, and your fair share.

    Press ? in the view for the keys and what the numbers mean.
    """
    config = load_config()
    host = resolve_host(host, config)
    columns = tuple(
        key
        for key in OPTIONAL_COLUMNS
        if key in extra_columns or key in config.get("columns", [])
    )
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise click.ClickException("sjobs is interactive and needs a terminal.")

    from .tui import run_tui

    run_tui(
        show_all=show_all,
        username=username,
        host=host,
        config=config,
        interval=interval,
        columns=columns,
    )


if __name__ == "__main__":
    main()
