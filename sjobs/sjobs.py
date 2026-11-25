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
import tabulate

MAX_CWD_WIDTH = 56
MAX_NAME_WIDTH = 32
# CWD never shrinks below this when fitting a table to the terminal.
MIN_CWD_WIDTH = 16
# Stop a hung SSH connection or squeue instead of leaving it on the login node.
SQUEUE_TIMEOUT = 60
# Watch mode never polls the Slurm controller more often than this.
MIN_WATCH_INTERVAL = 30
DEFAULT_WATCH_INTERVAL = 60
PROGRESS_WIDTH = 8
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

CATEGORIES = {
    "RUNNING": {
        "fields": [
            ("job_id", "Job ID"),
            ("name", "Name"),
            ("cwd", "CWD"),
            ("cpus", "CPUs"),
            ("gpus", "GPUs"),
            ("nodes", "Nodes"),
            ("elapsed", "Elapsed"),
            ("progress", "Progress"),
            ("time_left", "Left"),
            ("end_time", "Ends"),
        ],
        "color": "green",
        # Soonest to finish first.
        "sort": ("_end_time", "_job_id"),
    },
    "PENDING": {
        "fields": [
            ("job_id", "Job ID"),
            ("name", "Name"),
            ("cwd", "CWD"),
            ("cpus", "CPUs"),
            ("gpus", "GPUs"),
            ("partition", "Partition"),
            ("submitted", "Submitted"),
            ("est_start", "Est. Start"),
            ("priority", "Priority"),
            ("factors", "Factors"),
            ("reason", "Reason"),
            ("dependency", "Dependency"),
        ],
        "color": "cyan",
        # Highest priority first, then oldest submission.
        "sort": ("_priority", "_submit_time", "_job_id"),
    },
    "OTHER": {
        "fields": [
            ("job_id", "Job ID"),
            ("name", "Name"),
            ("cwd", "CWD"),
            ("cpus", "CPUs"),
            ("gpus", "GPUs"),
            ("partition", "Partition"),
            ("state", "State"),
            ("reason", "Reason"),
        ],
        "color": "red",
        "sort": ("_job_id",),
    },
}
RIGHT_ALIGNED = {"job_id", "cpus", "elapsed", "time_left", "priority"}


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


def run_squeue(show_all=False, username=None, host=None):
    """Run the script and return stdout."""
    cmdline = build_cmdline(show_all=show_all, username=username, host=host)
    description = f"squeue on {host}" if host else "squeue"

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
            f"Failed to fetch jobs with `{description}`: {details}"
        ) from exc
    except OSError as exc:
        raise click.ClickException(f"Failed to run `{cmdline[0]}`: {exc}") from exc

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
    """Bar plus percentage, e.g. `██░░░░░░  25%`."""
    fraction = min(max(fraction, 0.0), 1.0)
    filled = round(fraction * width)
    return f"{'█' * filled}{'░' * (width - filled)} {round(fraction * 100):3d}%"


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
    """Parse `sshare -P -n -U` rows into (rows, problem).

    `problem` is a short reason when the text held output but no rows.
    """
    rows = []
    for line in (text or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) != len(SSHARE_FIELDS) or not parts[1]:
            continue
        try:
            norm_shares, usage, fairshare = (float(parts[i]) for i in (3, 5, 6))
        except ValueError:
            continue
        rows.append(
            {
                "account": parts[0],
                "user": parts[1],
                "norm_shares": norm_shares,
                "usage": usage,
                "fairshare": fairshare,
            }
        )
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
    """Each priority factor's share of the total, largest first: `age 75% fs 20%`."""
    total = sum(value for value in factors.values() if value > 0)
    if total <= 0:
        return ""
    shares = sorted(
        (-round(100 * value / total), name) for name, value in factors.items()
    )
    return " ".join(f"{name} {-share}%" for share, name in shares if share <= -1)


def normalize_job(job, prefixes=None, now=None, priorities=None):
    """Normalize one squeue JSON job into display fields.

    `cwd` keeps the full prefix-stripped path; rendering truncates it to fit.
    `priorities` maps job IDs to parsed sprio entries.
    Keys starting with an underscore are sort keys and totals, not display fields.
    """
    now = time.time() if now is None else now
    start = epoch(job.get("start_time"))
    end = epoch(job.get("end_time"))
    submit = epoch(job.get("submit_time"))
    limit = time_limit_seconds(job.get("time_limit"))
    elapsed = now - start if start is not None and start <= now else None

    if end is not None:
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

    return {
        "job_id": format_job_id(job),
        "name": format_value(job.get("name")),
        "user": format_value(job.get("user_name")),
        "cwd": strip_prefix(job.get("current_working_directory"), prefixes),
        "cpus": format_value(job.get("cpus")),
        "gpus": format_gpu_requests(job),
        "state": primary_state(job),
        "nodes": format_value(job.get("nodes")),
        "elapsed": format_duration(elapsed),
        "progress": progress,
        "time_left": format_duration(math.inf if limit == math.inf else left),
        "end_time": format_when(end, now),
        "partition": format_value(job.get("partition")),
        "submitted": format_age(submit, now),
        "est_start": format_when(start, now) if start and start > now else "",
        "priority": "" if priority is None else str(int(priority)),
        "factors": format_factors(sprio["factors"]) if sprio else "",
        "reason": format_value(job.get("state_reason")),
        "dependency": format_value(job.get("dependency")),
        "_job_id": sort_number(job.get("job_id")),
        "_end_time": sort_number(job.get("end_time")),
        "_submit_time": sort_number(job.get("submit_time")),
        "_priority": -priority if priority is not None else float("inf"),
        "_cpus": cpus if isinstance(cpus, int) and not isinstance(cpus, bool) else 0,
        "_gpus": gpu_count(job.get("tres_alloc_str")),
    }


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
    return {
        "jobs": [normalize_job(job, prefixes, now, priorities) for job in jobs],
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


def truncate_job_id(text, max_width):
    """Shorten an array task list, keeping the job ID and closing bracket."""
    if len(text) <= max_width:
        return text

    if text.endswith("]") and "[" in text:
        return text[: max_width - 2] + "…]"

    return truncate_right(text, max_width)


# Columns that may be shortened, with their minimum width and how to cut them.
# CWD and Name are always capped (CAPPED); the others only shrink to fit a terminal.
CAPPED = {"cwd": MAX_CWD_WIDTH, "name": MAX_NAME_WIDTH}
SHRINKABLE = {
    "cwd": (MIN_CWD_WIDTH, truncate_left),
    "job_id": (16, truncate_job_id),
    "name": (10, truncate_right),
    "user": (8, truncate_right),
    "nodes": (10, truncate_right),
    "factors": (10, truncate_right),
    "dependency": (14, truncate_right),
    "partition": (12, truncate_right),
}


def category_fields(name, show_user=False):
    """A category's columns, with a User column after Name when requested."""
    fields = list(CATEGORIES[name]["fields"])
    if show_user:
        index = next(i for i, (key, _header) in enumerate(fields) if key == "name")
        fields.insert(index + 1, ("user", "User"))
    return fields


def format_table(name, jobs, widths=None, show_user=False):
    """Format one category's table, cutting columns to the given widths."""
    fields = category_fields(name, show_user)
    widths = {**CAPPED, **(widths or {})}
    rows = [
        [
            SHRINKABLE[key][1](job.get(key, ""), widths[key])
            if key in widths
            else job.get(key, "")
            for key, _header in fields
        ]
        for job in jobs
    ]
    return tabulate.tabulate(
        rows,
        headers=[header for _key, header in fields],
        tablefmt="simple",
        disable_numparse=True,
        colalign=tuple(
            "right" if key in RIGHT_ALIGNED else "left" for key, _header in fields
        ),
    )


def fit_table(name, jobs, max_width=None, show_user=False):
    """Format a table, shrinking the widest shrinkable column first to fit."""
    table = format_table(name, jobs, show_user=show_user)
    if not max_width:
        return table

    overflow = max(map(len, table.splitlines())) - max_width
    if overflow <= 0:
        return table

    keys = [
        key for key, _header in category_fields(name, show_user) if key in SHRINKABLE
    ]
    widths = {
        key: max(
            len(SHRINKABLE[key][1](job.get(key, ""), CAPPED.get(key, 10**6)))
            for job in jobs
        )
        for key in keys
    }
    while overflow > 0:
        candidates = [key for key in keys if widths[key] > SHRINKABLE[key][0]]
        if not candidates:
            break
        widest = max(candidates, key=lambda key: widths[key])
        widths[widest] -= 1
        overflow -= 1

    return format_table(name, jobs, widths, show_user=show_user)


def terminal_width():
    """Terminal width when stdout is a terminal; None for pipes and files."""
    if not sys.stdout.isatty():
        return None
    return shutil.get_terminal_size().columns


def render_category(name, jobs, max_width=None, show_user=False):
    """One category with a job count, sorted and fitted to max_width; "" if empty."""
    if not jobs:
        return ""

    spec = CATEGORIES[name]
    header = click.style(f">>> {name} ({len(jobs)})", fg=spec["color"], bold=True)
    table = fit_table(name, sort_jobs(name, jobs), max_width, show_user)
    return f"\n{header}\n\n{table}"


def fairshare_color(value):
    """Green when well placed, yellow when middling, red when low."""
    if value >= 0.5:
        return "green"
    if value >= 0.2:
        return "yellow"
    return "red"


def plural(count, noun):
    """`1 GPU`, `2 GPUs`."""
    return f"{count} {noun}{'' if count == 1 else 's'}"


def render_title(host, now):
    """`sjobs · HOST · Wed 14:32`."""
    when = datetime.fromtimestamp(now).strftime("%a %H:%M")
    return f"{click.style('sjobs', bold=True)} · {host or 'local'} · {when}"


def render_summary(grouped, snapshot, host, now, show_user=False):
    """Title and job counts, fair share lines, and notes about missing data."""
    running = grouped["RUNNING"]
    counts = []
    if running:
        totals = [plural(sum(job["_cpus"] for job in running), "CPU")]
        gpus = sum(job["_gpus"] for job in running)
        if gpus:
            totals.append(plural(gpus, "GPU"))
        counts.append(f"{len(running)} running ({', '.join(totals)})")
    if grouped["PENDING"]:
        counts.append(f"{len(grouped['PENDING'])} pending")
    if grouped["OTHER"]:
        counts.append(f"{len(grouped['OTHER'])} other")
    lines = [f"{render_title(host, now)}   {' · '.join(counts) or 'no jobs'}"]

    shares = snapshot["shares"]
    labels = [
        f"{row['user']}@{row['account']}" if show_user else row["account"]
        for row in shares
    ]
    width = max(map(len, labels), default=0)
    for label, row in zip(labels, shares):
        value = click.style(
            f"{row['fairshare']:.3f}", fg=fairshare_color(row["fairshare"]), bold=True
        )
        lines.append(
            f"Fair share  {label.ljust(width)}  {value}  "
            f"(usage {row['usage']:.1%} vs. {row['norm_shares']:.1%} share)"
        )

    problems = snapshot["problems"]
    notes = []
    if problems.get("sshare"):
        notes.append(f"fair share unavailable: {problems['sshare']}")
    # sprio reports an error when there is nothing pending; only then is it news.
    if problems.get("sprio") and grouped["PENDING"]:
        notes.append(f"priority factors unavailable: {problems['sprio']}")
    lines.extend(click.style(note, dim=True) for note in notes)
    return "\n".join(lines)


def render_frame(snapshot, host=None, now=None, max_width=None, show_user=False):
    """The whole output: summary, then each non-empty category."""
    now = time.time() if now is None else now
    grouped = {category: [] for category in CATEGORIES}
    for job in snapshot["jobs"]:
        grouped[classify_job(job)].append(job)

    parts = [render_summary(grouped, snapshot, host, now, show_user)]
    for category in CATEGORIES:
        parts.append(render_category(category, grouped[category], max_width, show_user))
    return "\n".join(part for part in parts if part)


def run_watch(make_frame, interval, host=None):
    """Redraw every interval seconds until Ctrl-C; errors are shown, not fatal."""
    footer = click.style(f"refreshing every {interval} s · Ctrl-C to quit", dim=True)
    try:
        while True:
            try:
                text = make_frame()
            except click.ClickException as exc:
                text = f"{render_title(host, time.time())}\n\n" + click.style(
                    f"Error: {exc.format_message()}", fg="red"
                )
            if sys.stdout.isatty():
                click.clear()
            click.echo(f"{text}\n\n{footer}")
            time.sleep(interval)
    except KeyboardInterrupt:
        click.echo()


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
    "--watch",
    "-w",
    is_flag=True,
    help="Redraw the output every --interval seconds until Ctrl-C.",
)
@click.option(
    "--interval",
    type=click.IntRange(min=MIN_WATCH_INTERVAL),
    default=DEFAULT_WATCH_INTERVAL,
    show_default=True,
    metavar="SECONDS",
    help=f"Seconds between refreshes in watch mode (at least {MIN_WATCH_INTERVAL}, "
    "to keep load on the Slurm controller low).",
)
def main(show_all, username, host, watch, interval):
    """Show Slurm jobs, your fair share, and pending job priorities."""
    config = load_config()
    host = resolve_host(host, config)
    show_user = bool(show_all or username)

    def make_frame():
        now = time.time()
        snapshot = fetch_snapshot(
            show_all=show_all,
            username=username,
            host=host,
            config=config,
            now=now,
        )
        return render_frame(snapshot, host, now, terminal_width(), show_user)

    if watch:
        run_watch(make_frame, interval, host)
    else:
        click.echo(make_frame())


if __name__ == "__main__":
    main()
