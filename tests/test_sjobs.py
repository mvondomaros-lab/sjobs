"""Offline regression tests; no test connects to Slurm or SSH.

The synthetic fixture's schema is modeled on a Slurm 23.11 `squeue --json`
snapshot. It contains no user job data and also exercises scalar field values.
"""

import json
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

SNAPSHOT = (Path(__file__).parent / "fixtures" / "squeue.json").read_text()
FIXTURE_PREFIXES = ["/scratch_shared/"]

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
    def test_local_squeue_needs_no_shell(self, _which):
        self.assertEqual(sjobs.build_cmdline(), sjobs.build_squeue_args())

    @patch("sjobs.sjobs.shutil.which", return_value=None)
    def test_ssh_keeps_user_input_in_one_argument(self, _which):
        username = "alice; echo $(whoami) 'quoted'"
        command = sjobs.build_cmdline(username=username, host="cluster")
        self.assertEqual(command[:2], ["ssh", "cluster"])
        shell = shlex.split(command[2])
        self.assertEqual(shell[:2], ["sh", "-lc"])
        script = shlex.split(shell[2])
        self.assertEqual(
            script[script.index("&&") + 1 :],
            ["squeue", "--json", "--states=all", f"--user={username}"],
        )

    @patch("sjobs.sjobs.shutil.which", return_value=None)
    def test_remote_script_runs_in_a_real_shell(self, _which):
        """Run the remote command through sh with a fake squeue on PATH."""
        username = "alice; echo $(whoami) 'quoted' \"x\""
        command = sjobs.build_cmdline(username=username, host="cluster")
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            args_file = Path(tmp) / "args"
            fake = bin_dir / "squeue"
            fake.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$@\" > {shlex.quote(str(args_file))}\n"
                "printf '{\"jobs\": []}'\n"
            )
            fake.chmod(0o755)
            env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": tmp}
            # ssh hands command[2] to the remote user's shell, like sh -c here.
            result = subprocess.run(
                ["sh", "-c", command[2]],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            homes, stdout = sjobs.split_remote_output(result.stdout)
            self.assertEqual(homes, list(dict.fromkeys([tmp, str(Path(tmp).resolve())])))
            self.assertEqual(sjobs.parse_squeue_json(stdout), [])
            self.assertEqual(
                args_file.read_text().splitlines(),
                ["--json", "--states=all", f"--user={username}"],
            )


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
            self.assertEqual(
                sjobs.config_path(), Path("/xdg/sjobs/config.toml")
            )

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
        self.assertEqual(prefixes, ["/home/alice/", "/nfs/alice/", "/scratch/", "/work/"])
        self.assertEqual(sjobs.path_prefixes(config), ["/scratch/", "/work/"])
        self.assertEqual(sjobs.shorten_path("/work/a/b", prefixes), "a/b")
        self.assertEqual(sjobs.shorten_path("/other/a", prefixes), "/other/a")


class RemoteOutputTests(unittest.TestCase):
    def test_marker_lines_carry_logical_and_physical_home(self):
        stdout = 'SJOBS_HOME=/home/alice\nSJOBS_HOME=/nfs/alice\n{"jobs": []}'
        self.assertEqual(
            sjobs.split_remote_output(stdout),
            (["/home/alice", "/nfs/alice"], '{"jobs": []}'),
        )

    def test_profile_noise_before_the_markers_is_dropped(self):
        stdout = (
            "Welcome!\nSJOBS_HOME=/home/alice\r\nSJOBS_HOME=/home/alice\n"
            '{"jobs": []}\n'
        )
        self.assertEqual(
            sjobs.split_remote_output(stdout), (["/home/alice"], '{"jobs": []}\n')
        )

    def test_missing_marker_or_empty_home(self):
        self.assertEqual(sjobs.split_remote_output('{"jobs": []}'), ([], '{"jobs": []}'))
        self.assertEqual(
            sjobs.split_remote_output("SJOBS_HOME=\nSJOBS_HOME=\n{}"), ([], "{}")
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


class CliTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        which = patch("sjobs.sjobs.shutil.which", return_value="/usr/bin/squeue")
        which.start()
        self.addCleanup(which.stop)

    @patch("sjobs.sjobs.subprocess.run")
    @patch("sjobs.sjobs.shutil.which", return_value=None)
    def test_one_fetch_renders_all_categories_and_preserves_text(self, _which, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout=SNAPSHOT)
        config = 'host = "cluster"\npath_prefixes = ["/scratch_shared"]\n'
        with write_config(config):
            result = self.runner.invoke(sjobs.main)
        self.assertEqual(result.exit_code, 0, result.output)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][:2], ["ssh", "cluster"])
        for text in (
            ">>> RUNNING",
            ">>> PENDING",
            ">>> OTHER",
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
                {"job_state": ["RUNNING"], "current_working_directory": "/nfs/alice/phys"},
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
