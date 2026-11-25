"""Offline regression tests; no test connects to Slurm or SSH.

The synthetic fixture's schema is modeled on a Slurm 23.11 `squeue --json`
snapshot. It contains no user job data and also exercises scalar field values.
"""

import json
import math
import os
import shlex
import subprocess
import tempfile
import unittest
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

import click
from click.testing import CliRunner

from sjobs import sjobs

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT = (FIXTURES / "squeue.json").read_text()
SSHARE = (FIXTURES / "sshare.txt").read_text()
SPRIO = (FIXTURES / "sprio.txt").read_text()
FIXTURE_PREFIXES = ["/scratch_shared/"]
# Six hours into the fixture's running job (12 h limit).
NOW = 1794482019


def script_output(squeue=SNAPSHOT, sshare=SSHARE, sprio=SPRIO, homes=("/home/a",)):
    """What the sjobs script prints: home markers, tool sections, squeue JSON."""
    lines = [f"{sjobs.REMOTE_HOME_MARKER}{home}\n" for home in homes]
    for name, text in (("sshare", sshare), ("sprio", sprio)):
        lines.append(f"{sjobs.SECTION_MARKER}{name}\n{text}")
    lines.append(f"{sjobs.SECTION_MARKER}squeue\n{squeue}")
    return "".join(lines)


def script_of(command):
    """The shell script inside a local or SSH command line."""
    if command[0] == "ssh":
        shell = shlex.split(command[2])
        assert shell[:2] == ["sh", "-lc"]
        return shell[2]
    assert command[:2] == ["sh", "-c"]
    return command[2]


_env_patch = None
_tmpdir = None


def setUpModule():
    """Isolate every test from the developer's real env vars and config file."""
    global _env_patch, _tmpdir
    _tmpdir = tempfile.TemporaryDirectory()
    _env_patch = patch.dict(
        os.environ,
        {sjobs.CONFIG_ENV_VAR: str(Path(_tmpdir.name) / "missing.toml")},
    )
    _env_patch.start()
    os.environ.pop(sjobs.HOST_ENV_VAR, None)


def tearDownModule():
    _env_patch.stop()
    _tmpdir.cleanup()


def write_config(text):
    """Write a config file and point SJOBS_CONFIG at it for the current test."""
    path = Path(_tmpdir.name) / f"config-{id(text)}.toml"
    path.write_text(text)
    return patch.dict(os.environ, {sjobs.CONFIG_ENV_VAR: str(path)})


class CommandTests(unittest.TestCase):
    def test_default_requests_all_states_for_current_user(self):
        self.assertEqual(
            sjobs.build_squeue_args(),
            ["squeue", "--json", "--states=all", "--me"],
        )

    def test_all_users_removes_only_user_filter(self):
        self.assertEqual(
            sjobs.build_squeue_args(show_all=True),
            ["squeue", "--json", "--states=all"],
        )

    def test_username_takes_precedence_over_all_users(self):
        self.assertEqual(
            sjobs.build_squeue_args(show_all=True, username="alice"),
            ["squeue", "--json", "--states=all", "--user=alice"],
        )

    @patch("sjobs.sjobs.shutil.which", return_value="/usr/bin/squeue")
    def test_local_runs_the_same_script(self, _which):
        command = sjobs.build_cmdline()
        self.assertEqual(command, ["sh", "-c", sjobs.build_script()])
        tokens = shlex.split(command[2])
        self.assertEqual(
            tokens[tokens.index("--json") - 1 :], sjobs.build_squeue_args()
        )

    def test_script_has_one_squeue_call_and_it_runs_last(self):
        for options in ({}, {"show_all": True}, {"username": "bob"}):
            with self.subTest(**options):
                script = sjobs.build_script(**options)
                self.assertEqual(script.count("squeue --json"), 1)
                self.assertTrue(
                    script.endswith(shlex.join(sjobs.build_squeue_args(**options)))
                )

    def test_extra_tools_follow_the_user_filter(self):
        own = sjobs.build_script()
        self.assertIn("sshare -P -n -U -o", own)
        self.assertEqual(own.count('-u "$(id -un)"'), 2)
        everyone = sjobs.build_script(show_all=True)
        # Fair share stays your own; priorities cover all pending jobs.
        self.assertEqual(everyone.count('-u "$(id -un)"'), 1)
        self.assertEqual(sjobs.build_script(username="bob").count("-u bob"), 2)

    @patch("sjobs.sjobs.shutil.which", return_value=None)
    def test_ssh_keeps_user_input_in_one_argument(self, _which):
        username = "alice; echo $(whoami) 'quoted'"
        command = sjobs.build_cmdline(username=username, host="cluster")
        self.assertEqual(command[:2], ["ssh", "cluster"])
        tokens = shlex.split(script_of(command))
        self.assertEqual(
            tokens[tokens.index("--json") - 1 :],
            ["squeue", "--json", "--states=all", f"--user={username}"],
        )
        # sshare and sprio each get the name as one argument after -u.
        self.assertEqual(
            [tokens[i + 1] for i, token in enumerate(tokens) if token == "-u"],
            [username, username],
        )

    def run_script(self, command, tools=("squeue", "sshare", "sprio")):
        """Run a command's script in a real sh with fake Slurm tools on PATH.

        Each fake tool records its arguments and prints its fixture.
        """
        outputs = {"squeue": '{"jobs": []}', "sshare": SSHARE, "sprio": SPRIO}
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for tool in tools:
            (tmp / f"{tool}.out").write_text(outputs[tool])
            fake = bin_dir / tool
            fake.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$@\" > {shlex.quote(str(tmp / tool))}.args\n"
                f"cat {shlex.quote(str(tmp / tool))}.out\n"
            )
            fake.chmod(0o755)
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp)}
        # ssh hands command[2] to the remote user's shell, like sh -c here.
        result = subprocess.run(
            ["sh", "-c", command[2]] if command[0] == "ssh" else command,
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        args = {tool: (tmp / f"{tool}.args").read_text().splitlines() for tool in tools}
        return tmp, result.stdout, args

    @patch("sjobs.sjobs.shutil.which", return_value=None)
    def test_remote_script_runs_in_a_real_shell(self, _which):
        username = "alice; echo $(whoami) 'quoted' \"x\""
        command = sjobs.build_cmdline(username=username, host="cluster")
        tmp, stdout, args = self.run_script(command)
        homes, sections, json_text = sjobs.split_sections(stdout)
        self.assertEqual(homes, list(dict.fromkeys([str(tmp), str(tmp.resolve())])))
        self.assertEqual(sections, {"sshare": SSHARE, "sprio": SPRIO})
        self.assertEqual(sjobs.parse_squeue_json(json_text), [])
        self.assertEqual(
            args["squeue"], ["--json", "--states=all", f"--user={username}"]
        )
        self.assertEqual(args["sshare"][-2:], ["-u", username])
        self.assertEqual(
            args["sprio"], ["-h", "-o", sjobs.SPRIO_FORMAT, "-u", username]
        )

    @patch("sjobs.sjobs.shutil.which", return_value="/usr/bin/squeue")
    def test_missing_extra_tools_leave_squeue_working(self, _which):
        _tmp, stdout, _args = self.run_script(sjobs.build_cmdline(), tools=("squeue",))
        _homes, sections, json_text = sjobs.split_sections(stdout)
        self.assertEqual(sjobs.parse_squeue_json(json_text), [])
        self.assertEqual(
            sjobs.parse_sshare(sections["sshare"]), ([], "sshare not found")
        )
        self.assertEqual(sjobs.parse_sprio(sections["sprio"]), ({}, "sprio not found"))

    @patch("sjobs.sjobs.shutil.which", return_value="/usr/bin/squeue")
    def test_explicit_host_uses_ssh_even_with_local_squeue(self, _which):
        self.assertEqual(sjobs.build_cmdline(host="cluster")[:2], ["ssh", "cluster"])

    @patch("sjobs.sjobs.shutil.which", return_value=None)
    def test_no_host_and_no_squeue_names_every_option(self, _which):
        with self.assertRaises(click.ClickException) as ctx:
            sjobs.build_cmdline()
        for text in ("--host", "SJOBS_HOST", str(sjobs.config_path())):
            self.assertIn(text, ctx.exception.message)


class ConfigTests(unittest.TestCase):
    def test_missing_config_is_empty(self):
        self.assertEqual(sjobs.load_config(), {})
        self.assertIsNone(sjobs.resolve_host(None, {}))

    def test_xdg_config_home_is_respected(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/xdg"}):
            os.environ.pop(sjobs.CONFIG_ENV_VAR)
            self.assertEqual(sjobs.config_path(), Path("/xdg/sjobs/config.toml"))

    def test_host_precedence_flag_env_file(self):
        with write_config('host = "from-file"\n'):
            config = sjobs.load_config()
        self.assertEqual(sjobs.resolve_host(None, config), "from-file")
        with patch.dict(os.environ, {sjobs.HOST_ENV_VAR: "from-env"}):
            self.assertEqual(sjobs.resolve_host(None, config), "from-env")
            self.assertEqual(sjobs.resolve_host("from-flag", config), "from-flag")

    def test_invalid_config_is_a_click_error(self):
        for text, expected in (
            ("host = ", "Failed to read config"),
            ("host = 42", "host"),
            ('host = ""', "host"),
            ('path_prefixes = "/scratch"', "path_prefixes"),
            ("path_prefixes = [1]", "path_prefixes"),
        ):
            with (
                self.subTest(text=text),
                write_config(text),
                self.assertRaisesRegex(click.ClickException, expected),
            ):
                sjobs.load_config()

    def test_path_prefixes_include_home_and_config(self):
        config = {"path_prefixes": ["/scratch", "/work/"]}
        prefixes = sjobs.path_prefixes(config, ["/home/alice", "/nfs/alice/", "/"])
        self.assertEqual(
            prefixes, ["/home/alice/", "/nfs/alice/", "/scratch/", "/work/"]
        )
        self.assertEqual(sjobs.path_prefixes(config), ["/scratch/", "/work/"])
        self.assertEqual(sjobs.shorten_path("/work/a/b", prefixes), "a/b")
        self.assertEqual(sjobs.shorten_path("/other/a", prefixes), "/other/a")


class SectionTests(unittest.TestCase):
    def test_marker_lines_carry_logical_and_physical_home(self):
        stdout = 'SJOBS_HOME=/home/alice\nSJOBS_HOME=/nfs/alice\n{"jobs": []}'
        self.assertEqual(
            sjobs.split_sections(stdout),
            (["/home/alice", "/nfs/alice"], {}, '{"jobs": []}'),
        )

    def test_profile_noise_before_the_markers_is_dropped(self):
        stdout = (
            'Welcome!\nSJOBS_HOME=/home/alice\r\nSJOBS_HOME=/home/alice\n{"jobs": []}\n'
        )
        self.assertEqual(
            sjobs.split_sections(stdout), (["/home/alice"], {}, '{"jobs": []}\n')
        )

    def test_missing_marker_or_empty_home(self):
        self.assertEqual(sjobs.split_sections('{"jobs": []}'), ([], {}, '{"jobs": []}'))
        self.assertEqual(
            sjobs.split_sections("SJOBS_HOME=\nSJOBS_HOME=\n{}"), ([], {}, "{}")
        )

    def test_tool_sections_and_squeue_json(self):
        stdout = script_output(squeue='{"jobs": []}\nSJOBS_SECTION=fake\n')
        homes, sections, json_text = sjobs.split_sections(stdout)
        self.assertEqual(homes, ["/home/a"])
        self.assertEqual(sections, {"sshare": SSHARE, "sprio": SPRIO})
        # Nothing after the squeue marker is read as a marker.
        self.assertEqual(json_text, '{"jobs": []}\nSJOBS_SECTION=fake\n')


class ExtraToolTests(unittest.TestCase):
    def test_sshare_rows(self):
        rows, problem = sjobs.parse_sshare(SSHARE)
        self.assertIsNone(problem)
        self.assertEqual(
            rows,
            [
                {
                    "account": "acct_a",
                    "user": "alice",
                    "norm_shares": 0.2,
                    "usage": 0.332631,
                    "fairshare": 0.016209,
                }
            ],
        )

    def test_unusable_output_becomes_a_problem_not_an_error(self):
        for parse in (sjobs.parse_sshare, sjobs.parse_sprio):
            with self.subTest(parse=parse.__name__):
                empty, problem = parse(
                    "\n  sshare: error: Problem talking to slurmdbd\n"
                )
                self.assertFalse(empty)
                self.assertEqual(problem, "sshare: error: Problem talking to slurmdbd")
                self.assertEqual(parse("")[1], None)
                self.assertEqual(parse(None)[1], None)
        # Rows without a user (account lines) or with bad numbers are skipped.
        self.assertEqual(
            sjobs.parse_sshare("acct||1|0.2|1|0.1|0.5\nacct|u|1|x|1|0.1|0.5\n")[0], []
        )

    def test_sprio_keeps_the_best_partition(self):
        priorities, problem = sjobs.parse_sprio(SPRIO + "garbage|line\n")
        self.assertIsNone(problem)
        self.assertEqual(
            priorities,
            {
                1200: {
                    "priority": 5000.0,
                    "factors": {
                        "age": 1000.0,
                        "fs": 3000.0,
                        "size": 0.0,
                        "part": 1000.0,
                        "qos": 0.0,
                        "tres": 0.0,
                    },
                }
            },
        )

    def test_sprio_tres_and_array_ids(self):
        priorities, _ = sjobs.parse_sprio(
            "1100_[1-4]|a|p|30|10|0|0|0|0|cpu=15,mem=5|0\n"
        )
        self.assertEqual(priorities[1100]["factors"]["tres"], 20.0)

    def test_factor_shares_largest_first(self):
        self.assertEqual(
            sjobs.format_factors({"age": 100000, "fs": 62, "part": 33333, "qos": 0}),
            "age 75% part 25%",
        )
        self.assertEqual(sjobs.format_factors({"age": 0, "fs": 0}), "")


class TimeTests(unittest.TestCase):
    def test_durations(self):
        for seconds, expected in (
            (None, ""),
            (0, "0m"),
            (-5, "0m"),
            (59, "0m"),
            (45 * 60, "45m"),
            (3 * 3600 + 12 * 60, "3h12m"),
            (28 * 3600, "1d04h"),
            (math.inf, "unlimited"),
        ):
            with self.subTest(seconds=seconds):
                self.assertEqual(sjobs.format_duration(seconds), expected)

    def test_relative_dates(self):
        now = datetime(2026, 11, 25, 12, 0).timestamp()
        for when, expected in (
            (datetime(2026, 11, 25, 18, 5), "today 18:05"),
            (datetime(2026, 11, 26, 0, 10), "tomorrow 00:10"),
            (datetime(2026, 11, 24, 9, 0), "yesterday 09:00"),
            (datetime(2026, 11, 28, 9, 12), "Sat 09:12"),
            (datetime(2026, 12, 9, 14, 30), "2026-12-09 14:30"),
        ):
            with self.subTest(when=when):
                self.assertEqual(sjobs.format_when(when.timestamp(), now), expected)
        self.assertEqual(sjobs.format_when(None, now), "")

    def test_ages(self):
        self.assertEqual(sjobs.format_age(NOW - 7500, NOW), "2h05m ago")
        self.assertEqual(sjobs.format_age(NOW + 60, NOW), "")
        self.assertEqual(sjobs.format_age(None, NOW), "")

    def test_progress_bar(self):
        self.assertEqual(sjobs.progress_bar(0.25), "██░░░░░░  25%")
        self.assertEqual(sjobs.progress_bar(1.5), "████████ 100%")
        self.assertEqual(sjobs.progress_bar(-1), "░░░░░░░░   0%")

    def test_time_limits(self):
        self.assertEqual(sjobs.time_limit_seconds(60), 3600)
        self.assertEqual(
            sjobs.time_limit_seconds({"set": True, "infinite": True, "number": 0}),
            math.inf,
        )
        self.assertIsNone(sjobs.time_limit_seconds(None))
        self.assertIsNone(
            sjobs.time_limit_seconds({"set": False, "infinite": False, "number": 0})
        )


class ParsingTests(unittest.TestCase):
    def test_fixture_and_empty_queue_are_valid(self):
        self.assertEqual(len(sjobs.parse_squeue_json(SNAPSHOT)), 4)
        self.assertEqual(sjobs.parse_squeue_json('{"jobs": []}'), [])

    def test_invalid_json_is_a_click_error(self):
        with self.assertRaisesRegex(click.ClickException, "parse squeue JSON"):
            sjobs.parse_squeue_json("not JSON")

    def test_top_level_must_be_an_object(self):
        for payload in ("[]", "null", "42", '"string"'):
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(click.ClickException, "object"),
            ):
                sjobs.parse_squeue_json(payload)

    def test_jobs_must_exist_and_be_a_list(self):
        for payload in ("{}", '{"jobs": null}', '{"jobs": {}}'):
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(click.ClickException, "jobs"),
            ):
                sjobs.parse_squeue_json(payload)

    def test_each_job_must_be_an_object(self):
        for job in (None, 42, "RUNNING", []):
            with (
                self.subTest(job=job),
                self.assertRaisesRegex(click.ClickException, "job.*object"),
            ):
                sjobs.parse_squeue_json(json.dumps({"jobs": [{}, job]}))

    def test_slurm_errors_cannot_look_like_an_empty_queue(self):
        payload = {
            "jobs": [],
            "errors": [{"error": "Unable to contact slurm controller"}],
        }
        with self.assertRaisesRegex(click.ClickException, "Unable to contact"):
            sjobs.parse_squeue_json(json.dumps(payload))

    def test_slurm_error_description_is_preserved(self):
        payload = {
            "errors": [{"description": "job data unavailable", "error_number": 1}],
        }
        with self.assertRaisesRegex(click.ClickException, "job data unavailable"):
            sjobs.parse_squeue_json(json.dumps(payload))

    def test_slurm_error_keeps_both_message_and_context(self):
        payload = {
            "errors": [{"error": "Socket timed out", "description": "Loading jobs"}],
        }
        with self.assertRaisesRegex(
            click.ClickException, "Socket timed out: Loading jobs"
        ):
            sjobs.parse_squeue_json(json.dumps(payload))


class JobTests(unittest.TestCase):
    def test_live_schema_preserves_arrays_and_gpu_requests(self):
        running, pending, regular, completing = [
            sjobs.normalize_job(job, FIXTURE_PREFIXES)
            for job in sjobs.parse_squeue_json(SNAPSHOT)
        ]
        self.assertEqual(running["job_id"], "1000_0")
        self.assertEqual(running["gpus"], "1 x A100 (80GB)")
        self.assertEqual(running["cpus"], "16")
        self.assertEqual(running["cwd"], "demo/run 01")
        self.assertEqual(pending["job_id"], "1100_[1-4]")
        self.assertEqual(pending["dependency"], "afterok:1000_*")
        self.assertEqual(regular["job_id"], "1200")
        self.assertEqual(sjobs.classify_job(completing), "OTHER")

    def test_running_and_pending_times_and_priority(self):
        priorities, _ = sjobs.parse_sprio(SPRIO)
        running, array, queued, _completing = [
            sjobs.normalize_job(job, FIXTURE_PREFIXES, NOW, priorities)
            for job in sjobs.parse_squeue_json(SNAPSHOT)
        ]
        self.assertEqual(running["name"], "md run")
        self.assertEqual(running["user"], "alice")
        self.assertEqual(running["elapsed"], "6h00m")
        self.assertEqual(running["progress"], "████░░░░  50%")
        self.assertEqual(running["time_left"], "6h00m")
        self.assertEqual(running["_cpus"], 16)
        self.assertEqual(running["_gpus"], 1)
        self.assertEqual(queued["submitted"], "1h56m ago")
        self.assertEqual(queued["est_start"], sjobs.format_when(1794485619, NOW))
        # sprio's best partition wins over squeue's own priority.
        self.assertEqual(queued["priority"], "5000")
        self.assertEqual(queued["factors"], "fs 60% age 20% part 20%")
        # Without sprio data, squeue's priority is shown and no factors.
        self.assertEqual(
            sjobs.normalize_job(sjobs.parse_squeue_json(SNAPSHOT)[2], now=NOW)[
                "priority"
            ],
            "4000",
        )
        self.assertEqual(array["priority"], "")
        self.assertEqual(array["est_start"], "")

    def test_past_start_time_is_not_an_estimate(self):
        job = sjobs.normalize_job(
            {"job_state": ["PENDING"], "start_time": NOW - 60}, now=NOW
        )
        self.assertEqual(job["est_start"], "")

    def test_unlimited_and_unknown_time_limits(self):
        unlimited = sjobs.normalize_job(
            {
                "job_state": ["RUNNING"],
                "start_time": NOW - 3600,
                "time_limit": {"set": True, "infinite": True, "number": 0},
            },
            now=NOW,
        )
        self.assertEqual(
            (unlimited["elapsed"], unlimited["progress"], unlimited["time_left"]),
            ("1h00m", "", "unlimited"),
        )
        bare = sjobs.normalize_job({"job_state": ["RUNNING"]}, now=NOW)
        self.assertEqual((bare["elapsed"], bare["time_left"]), ("", ""))

    def test_scalar_array_identifiers(self):
        job = {"job_id": 1005, "array_job_id": 1000, "array_task_id": 4}
        self.assertEqual(sjobs.normalize_job(job)["job_id"], "1000_4")

    def test_unset_array_task_does_not_become_task_zero(self):
        for task_id in (
            None,
            4294967294,
            4294967295,
            {"set": False, "infinite": False, "number": 0},
        ):
            with self.subTest(task_id=task_id):
                job = {"job_id": 1005, "array_job_id": 1000, "array_task_id": task_id}
                self.assertEqual(sjobs.normalize_job(job)["job_id"], "1005")

    def test_heterogeneous_job_component_including_zero(self):
        for offset in (0, 1):
            with self.subTest(offset=offset):
                job = {
                    "job_id": 1000 + offset,
                    "het_job_id": {"set": True, "infinite": False, "number": 1000},
                    "het_job_offset": {
                        "set": True,
                        "infinite": False,
                        "number": offset,
                    },
                }
                self.assertEqual(sjobs.normalize_job(job)["job_id"], f"1000+{offset}")

    def test_state_flags_override_base_state(self):
        for states, expected in (
            (["RUNNING", "COMPLETING"], "COMPLETING"),
            (["RUNNING", "CONFIGURING"], "CONFIGURING"),
            (["PENDING", "REQUEUE_HOLD"], "REQUEUE_HOLD"),
            (["RUNNING", "CONFIGURING", "COMPLETING"], "COMPLETING"),
        ):
            with self.subTest(states=states):
                job = sjobs.normalize_job({"job_state": states})
                self.assertEqual(job["state"], expected)
                self.assertEqual(sjobs.classify_job(job), "OTHER")

    def test_ancillary_state_flag_keeps_running_category(self):
        job = sjobs.normalize_job({"job_state": ["RUNNING", "POWER_UP_NODE"]})
        self.assertEqual(sjobs.classify_job(job), "RUNNING")

    def test_scalar_empty_and_unknown_states(self):
        for state, category in (
            ("RUNNING", "RUNNING"),
            ("R", "RUNNING"),
            ("PD", "PENDING"),
            ([], "OTHER"),
            (None, "OTHER"),
            (["FUTURE_STATE"], "OTHER"),
        ):
            with self.subTest(state=state):
                self.assertEqual(
                    sjobs.classify_job(sjobs.normalize_job({"job_state": state})),
                    category,
                )

    def test_gpu_types_and_unrelated_resources(self):
        self.assertEqual(
            sjobs.format_gpus("cpu:4,gres/gpu:a100_80gb:2,gres/gpu:1"),
            "2 x A100 (80GB), 1 x GPU",
        )
        self.assertEqual(sjobs.format_gpus("gres/gpumem:80,gres/gpuutil:100"), "")
        self.assertEqual(sjobs.format_gpus("gpu:rtx_3090:2"), "2 x RTX 3090")
        self.assertEqual(sjobs.format_gpus(None), "")

    def test_gpu_request_scopes_are_not_added_together(self):
        job = {
            "tres_per_job": "gres/gpu:8",
            "tres_per_node": "gres/gpu:4",
            "tres_per_socket": "gres/gpu:2",
            "tres_per_task": "cpu:4,gres/gpu:1",
        }
        self.assertEqual(
            sjobs.normalize_job(job)["gpus"],
            "8 x GPU; 4 x GPU / node; 2 x GPU / socket; 1 x GPU / task",
        )

    def test_gres_per_node_requests_are_shown(self):
        # --gres=gpu:1 and --gres=gpu:h200:1 fill only tres_per_node.
        for tres, expected in (
            ("gres/gpu:1", "1 x GPU / node"),
            ("gres/gpu:h200:1", "1 x H200 / node"),
        ):
            with self.subTest(tres=tres):
                job = {"tres_per_job": "", "tres_per_node": tres}
                self.assertEqual(sjobs.normalize_job(job)["gpus"], expected)

    def test_allocation_names_the_assigned_gpu_type(self):
        job = {
            "tres_per_node": "gres/gpu:1",
            "tres_alloc_str": "cpu=12,mem=16G,node=1,billing=20,"
            "gres/gpu=1,gres/gpu:a100_80gb=1",
        }
        self.assertEqual(sjobs.normalize_job(job)["gpus"], "1 x A100 (80GB)")

    def test_allocation_totals(self):
        for tres, expected in (
            ("cpu=4,gres/gpu=8", "8 x GPU"),
            ("gres/gpu=3,gres/gpu:a40=2,gres/gpu:h200=1", "2 x A40, 1 x H200"),
            ("cpu=4,gres/gpumem=80G,gres/gpu=0", ""),
            ("", ""),
            (None, ""),
        ):
            with self.subTest(tres=tres):
                self.assertEqual(sjobs.format_gpu_totals(tres), expected)

    def test_request_totals_are_the_last_fallback(self):
        job = {"tres_req_str": "cpu=2,node=1,gres/gpu=2"}
        self.assertEqual(sjobs.normalize_job(job)["gpus"], "2 x GPU")
        job["tres_per_task"] = "gres/gpu:1"
        self.assertEqual(sjobs.normalize_job(job)["gpus"], "1 x GPU / task")

    def test_wrapped_and_scalar_timestamps(self):
        timestamp = 1794328380
        expected = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
        self.assertEqual(sjobs.format_timestamp(timestamp), expected)
        self.assertEqual(
            sjobs.format_timestamp(
                {
                    "set": True,
                    "infinite": False,
                    "number": timestamp,
                }
            ),
            expected,
        )
        self.assertEqual(
            sjobs.format_timestamp(
                {
                    "set": False,
                    "infinite": False,
                    "number": timestamp,
                }
            ),
            "",
        )
        self.assertEqual(
            sjobs.format_timestamp(
                {
                    "set": True,
                    "infinite": True,
                    "number": 0,
                }
            ),
            "infinite",
        )

    @patch("sjobs.sjobs.datetime")
    def test_platform_timestamp_errors_do_not_crash_display(self, datetime_mock):
        datetime_mock.fromtimestamp.side_effect = OSError("timestamp out of range")
        self.assertEqual(sjobs.format_timestamp(999999999999), "999999999999")


class RenderTests(unittest.TestCase):
    def running(self, job_id, end_time, cwd="run"):
        return sjobs.normalize_job(
            {
                "job_id": job_id,
                "job_state": ["RUNNING"],
                "end_time": end_time,
                "current_working_directory": cwd,
            },
            [],
        )

    def test_running_jobs_sort_by_end_time_then_id_unset_last(self):
        jobs = [
            self.running(3, 300),
            self.running(5, None),
            self.running(4, 100),
            self.running(2, 300),
        ]
        order = [job["job_id"] for job in sjobs.sort_jobs("RUNNING", jobs)]
        self.assertEqual(order, ["4", "2", "3", "5"])

    def test_pending_jobs_sort_by_submit_time(self):
        jobs = [
            sjobs.normalize_job({"job_id": 1, "submit_time": 20}),
            sjobs.normalize_job(
                {
                    "job_id": 2,
                    "submit_time": {"set": True, "infinite": False, "number": 10},
                }
            ),
        ]
        order = [job["job_id"] for job in sjobs.sort_jobs("PENDING", jobs)]
        self.assertEqual(order, ["2", "1"])

    def test_pending_jobs_sort_by_priority_then_submit_time(self):
        jobs = [
            sjobs.normalize_job({"job_id": 1, "submit_time": 10}),
            sjobs.normalize_job({"job_id": 2, "submit_time": 30, "priority": 900}),
            sjobs.normalize_job({"job_id": 3, "submit_time": 20, "priority": 900}),
            sjobs.normalize_job({"job_id": 4, "submit_time": 40, "priority": 5000}),
        ]
        order = [job["job_id"] for job in sjobs.sort_jobs("PENDING", jobs)]
        self.assertEqual(order, ["4", "3", "2", "1"])

    def test_long_names_are_always_capped(self):
        job = sjobs.normalize_job({"job_id": 1, "name": "n" * 80})
        self.assertIn(
            "n" * (sjobs.MAX_NAME_WIDTH - 1) + "…", sjobs.fit_table("OTHER", [job])
        )
        self.assertNotIn("n" * sjobs.MAX_NAME_WIDTH, sjobs.fit_table("OTHER", [job]))

    def test_user_column_only_when_requested(self):
        job = sjobs.normalize_job({"job_id": 1, "name": "x", "user_name": "bob"})
        self.assertNotIn("User", sjobs.fit_table("RUNNING", [job]))
        table = sjobs.fit_table("RUNNING", [job], show_user=True)
        header = table.splitlines()[0].split()
        self.assertEqual(header[:4], ["Job", "ID", "Name", "User"])
        self.assertIn("bob", table)

    def test_summary_counts_totals_and_fair_share(self):
        snapshot = {
            "jobs": [
                sjobs.normalize_job(job, now=NOW)
                for job in sjobs.parse_squeue_json(SNAPSHOT)
            ],
            "shares": sjobs.parse_sshare(SSHARE)[0],
            "problems": {"sshare": None, "sprio": None},
        }
        frame = click.unstyle(sjobs.render_frame(snapshot, "cluster", NOW))
        first = frame.splitlines()[0]
        self.assertIn("sjobs · cluster · ", first)
        self.assertIn("1 running (16 CPUs, 1 GPU) · 2 pending · 1 other", first)
        self.assertIn("Fair share  acct_a  0.016  (usage 33.3% vs. 20.0% share)", frame)
        shown = sjobs.render_frame(snapshot, "cluster", NOW, show_user=True)
        self.assertIn("alice@acct_a", click.unstyle(shown))

    def test_fair_share_colors(self):
        self.assertEqual(
            [sjobs.fairshare_color(v) for v in (0.9, 0.5, 0.3, 0.01)],
            ["green", "green", "yellow", "red"],
        )

    def test_missing_data_notes(self):
        pending = sjobs.normalize_job({"job_state": ["PENDING"]}, now=NOW)
        problems = {"sshare": "sshare not found", "sprio": "sprio not found"}
        frame = sjobs.render_frame(
            {"jobs": [pending], "shares": [], "problems": problems}, now=NOW
        )
        self.assertIn("fair share unavailable: sshare not found", frame)
        self.assertIn("priority factors unavailable: sprio not found", frame)
        # sprio complains when nothing is pending; that is not worth a note.
        quiet = sjobs.render_frame(
            {"jobs": [], "shares": [], "problems": problems}, now=NOW
        )
        self.assertNotIn("priority factors", quiet)
        self.assertIn("no jobs", quiet)

    def test_cwd_is_kept_whole_until_rendering(self):
        job = self.running(1, 100, cwd="/x/" + "d" * 80)
        self.assertEqual(len(job["cwd"]), 83)
        table = sjobs.fit_table("RUNNING", [job])
        self.assertIn("…" + "d" * (sjobs.MAX_CWD_WIDTH - 1), table)

    def test_table_shrinks_cwd_to_fit_the_terminal(self):
        jobs = [self.running(1, 100, cwd="/x/" + "d" * 80)]
        wide = max(map(len, sjobs.fit_table("RUNNING", jobs).splitlines()))
        fitted = sjobs.fit_table("RUNNING", jobs, max_width=wide - 10)
        self.assertEqual(max(map(len, fitted.splitlines())), wide - 10)

    def test_cwd_never_shrinks_below_minimum(self):
        jobs = [self.running(1, 100, cwd="/x/" + "d" * 80)]
        fitted = sjobs.fit_table("RUNNING", jobs, max_width=10)
        self.assertIn("…" + "d" * (sjobs.MIN_CWD_WIDTH - 1) + " ", fitted)

    def test_long_job_ids_and_dependencies_shrink_widest_first(self):
        job = sjobs.normalize_job(
            {
                "job_id": 1,
                "array_job_id": 1000,
                "array_task_string": ",".join(str(i) for i in range(2, 40, 3)),
                "job_state": ["PENDING"],
                "dependency": "afterany:2_*(unfulfilled),afterany:3_*(unfulfilled)",
            },
            [],
        )
        wide = sjobs.fit_table("PENDING", [job])
        self.assertIn(job["job_id"], wide)
        self.assertIn(job["dependency"], wide)
        width = max(map(len, wide.splitlines())) - 40
        fitted = sjobs.fit_table("PENDING", [job], max_width=width)
        self.assertEqual(max(map(len, fitted.splitlines())), width)
        self.assertRegex(fitted, r"1000_\[2,5,8,[0-9,]*…\]")
        self.assertIn("afterany:2_*", fitted)
        self.assertIn("…", fitted.splitlines()[-1].rstrip()[-1])

    def test_job_id_truncation(self):
        self.assertEqual(sjobs.truncate_job_id("1000_[1-4]", 20), "1000_[1-4]")
        self.assertEqual(sjobs.truncate_job_id("1000_[1,3,5,7,9]", 12), "1000_[1,3,…]")
        self.assertEqual(sjobs.truncate_job_id("123456789", 5), "1234…")

    def test_short_tables_are_not_changed(self):
        jobs = [self.running(1, 100)]
        self.assertEqual(
            sjobs.fit_table("RUNNING", jobs, max_width=500),
            sjobs.fit_table("RUNNING", jobs),
        )

    @patch("sjobs.sjobs.sys.stdout")
    def test_width_is_only_used_for_terminals(self, stdout):
        stdout.isatty.return_value = False
        self.assertIsNone(sjobs.terminal_width())
        stdout.isatty.return_value = True
        with patch("sjobs.sjobs.shutil.get_terminal_size") as size:
            size.return_value = os.terminal_size((90, 40))
            self.assertEqual(sjobs.terminal_width(), 90)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        which = patch("sjobs.sjobs.shutil.which", return_value="/usr/bin/squeue")
        which.start()
        self.addCleanup(which.stop)

    @patch("sjobs.sjobs.subprocess.run")
    @patch("sjobs.sjobs.shutil.which", return_value=None)
    def test_one_fetch_renders_all_categories_and_preserves_text(self, _which, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout=script_output())
        config = 'host = "cluster"\npath_prefixes = ["/scratch_shared"]\n'
        with write_config(config):
            result = self.runner.invoke(sjobs.main)
        self.assertEqual(result.exit_code, 0, result.output)
        # One process (one SSH round trip) with exactly one squeue call in it.
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][:2], ["ssh", "cluster"])
        self.assertEqual(script_of(run.call_args.args[0]).count("squeue --json"), 1)
        for text in (
            "sjobs · cluster · ",
            "Fair share  acct_a  0.016",
            "md run",
            "fs 60% age 20% part 20%",
            ">>> RUNNING (1)",
            ">>> PENDING (2)",
            ">>> OTHER (1)",
            "1000_0",
            "1100_[1-4]",
            "1 x A100 (80GB)",
            "COMPLETING",
            "demo/run 01",
            "001234",
        ):
            self.assertIn(text, result.output)

    @patch("sjobs.sjobs.subprocess.run")
    def test_empty_queue_is_successful(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout='{"jobs": []}')
        result = self.runner.invoke(sjobs.main)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(">>>", result.output)
        self.assertIn("no jobs", result.output)

    @patch("sjobs.sjobs.subprocess.run")
    def test_all_users_adds_the_user_column(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout=script_output())
        result = self.runner.invoke(sjobs.main, ["--all"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("User", result.output)
        self.assertIn("alice@acct_a", result.output)

    @patch("sjobs.sjobs.time.sleep", side_effect=KeyboardInterrupt)
    @patch("sjobs.sjobs.subprocess.run")
    def test_watch_redraws_until_ctrl_c(self, run, sleep):
        run.return_value = subprocess.CompletedProcess([], 0, stdout=script_output())
        result = self.runner.invoke(sjobs.main, ["--watch"])
        self.assertEqual(result.exit_code, 0, result.output)
        run.assert_called_once()
        sleep.assert_called_once_with(sjobs.DEFAULT_WATCH_INTERVAL)
        self.assertEqual(result.output.count(">>> RUNNING"), 1)
        self.assertIn("refreshing every 60 s · Ctrl-C to quit", result.output)

    @patch("sjobs.sjobs.time.sleep", side_effect=[None, KeyboardInterrupt])
    @patch("sjobs.sjobs.subprocess.run")
    def test_watch_shows_errors_and_keeps_going(self, run, sleep):
        run.side_effect = [
            subprocess.CalledProcessError(255, ["ssh"], output="", stderr="timeout"),
            subprocess.CompletedProcess([], 0, stdout=script_output()),
        ]
        result = self.runner.invoke(sjobs.main, ["-w", "--interval", "45"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(
            "Error: Failed to fetch jobs with `squeue`: timeout", result.output
        )
        self.assertIn(">>> RUNNING", result.output)
        self.assertEqual(sleep.call_args.args, (45,))

    def test_watch_interval_has_a_minimum(self):
        result = self.runner.invoke(sjobs.main, ["-w", "--interval", "5"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--interval", result.output)

    @patch("sjobs.sjobs.subprocess.run", side_effect=FileNotFoundError("ssh missing"))
    def test_missing_executable_has_a_readable_error(self, _run):
        result = self.runner.invoke(sjobs.main)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Error:", result.output)
        self.assertIn("ssh missing", result.output)
        self.assertNotIn("Traceback", result.output)

    @patch("sjobs.sjobs.subprocess.run")
    def test_command_error_prefers_stderr_and_falls_back_to_stdout(self, run):
        for stderr, stdout, expected in (
            ("permission denied", "other output", "permission denied"),
            ("", "command unavailable", "command unavailable"),
            (None, None, "unknown error"),
        ):
            with self.subTest(stderr=stderr, stdout=stdout):
                run.side_effect = subprocess.CalledProcessError(
                    1,
                    ["squeue"],
                    output=stdout,
                    stderr=stderr,
                )
                result = self.runner.invoke(sjobs.main)
                self.assertEqual(result.exit_code, 1)
                self.assertIn(expected, result.output)

    @patch(
        "sjobs.sjobs.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["squeue"], 60),
    )
    def test_hung_command_times_out_with_a_readable_error(self, run):
        result = self.runner.invoke(sjobs.main)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("did not finish within", result.output)
        self.assertEqual(run.call_args.kwargs["timeout"], sjobs.SQUEUE_TIMEOUT)

    def test_version(self):
        result = self.runner.invoke(sjobs.main, ["--version"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f"version {version('sjobs')}", result.output)

    @patch("sjobs.sjobs.subprocess.run")
    def test_remote_home_is_stripped_and_local_home_is_not(self, run):
        local = f"{Path.home()}/local-only"
        payload = {
            "jobs": [
                {"job_state": ["RUNNING"], "current_working_directory": "/u/alice/run"},
                {
                    "job_state": ["RUNNING"],
                    "current_working_directory": "/nfs/alice/phys",
                },
                {"job_state": ["RUNNING"], "current_working_directory": local},
            ]
        }
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout=f"SJOBS_HOME=/u/alice\nSJOBS_HOME=/nfs/alice\n{json.dumps(payload)}",
        )
        result = self.runner.invoke(sjobs.main, ["-H", "cluster"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("/u/alice", result.output)
        self.assertNotIn("/nfs/alice", result.output)
        self.assertRegex(result.output, r"\n +phys\n")
        self.assertRegex(result.output, r"\n +run\n")
        self.assertIn(local, result.output)

    @patch("sjobs.sjobs.subprocess.run")
    def test_host_flag_overrides_env(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout='{"jobs": []}')
        with patch.dict(os.environ, {sjobs.HOST_ENV_VAR: "from-env"}):
            result = self.runner.invoke(sjobs.main, ["-H", "from-flag"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(run.call_args.args[0][:2], ["ssh", "from-flag"])

    @patch("sjobs.sjobs.subprocess.run")
    def test_invalid_response_reports_an_error_without_partial_tables(self, run):
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout='{"jobs": [{"job_state": "RUNNING"}, null]}',
        )
        result = self.runner.invoke(sjobs.main)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Error:", result.output)
        self.assertNotIn(">>>", result.output)


if __name__ == "__main__":
    unittest.main()
