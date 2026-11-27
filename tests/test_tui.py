"""Offline tests of the interactive watcher, driven with Textual's Pilot."""

import json
import subprocess
import unittest
from unittest.mock import patch

import click
from test_sjobs import (  # noqa: F401
    SNAPSHOT,
    script_output,
    setUpModule,
    tearDownModule,
)

from sjobs import sjobs, tui

SIZE = (160, 40)


def ok(stdout=None):
    return subprocess.CompletedProcess([], 0, stdout=stdout or script_output())


class AppTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        which = patch("sjobs.sjobs.shutil.which", return_value="/usr/bin/squeue")
        which.start()
        self.addCleanup(which.stop)
        run = patch("sjobs.sjobs.subprocess.run", return_value=ok())
        self.run_mock = run.start()
        self.addCleanup(run.stop)

    async def settle(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    def headers(self, app, name):
        table = app.query_one(f"#table-{name}")
        return [str(column.label) for column in table.columns.values()]

    def row_counts(self, app):
        return [table.row_count for table in app.tables()]


class TuiTests(AppTestCase):
    async def test_first_frame_uses_one_fetch(self):
        app = tui.SjobsApp()
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(app, pilot)
            self.run_mock.assert_called_once()
            self.assertEqual(self.row_counts(app), [1, 2, 1])
            summary = str(app.query_one("#summary").render())
            self.assertIn("● 1 running 16 CPUs · 16G · 1 GPU", summary)
            self.assertNotIn("Path", self.headers(app, "RUNNING"))

    async def test_q_quits_without_jobs(self):
        # With every table hidden, focus must not stay on the hidden filter box.
        empty = json.dumps({"jobs": [], "errors": [], "warnings": []})
        self.run_mock.return_value = ok(script_output(squeue=empty))
        app = tui.SjobsApp()
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(app, pilot)
            self.assertEqual(self.row_counts(app), [0, 0, 0])
            await pilot.press("q")
            await pilot.pause()
            self.assertFalse(app.is_running)

    async def test_columns_toggle(self):
        app = tui.SjobsApp(columns=("nodes",))
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(app, pilot)
            self.assertIn("Nodes", self.headers(app, "RUNNING"))
            await pilot.press("o")
            self.assertIsInstance(app.screen, tui.ColumnChooser)
            # The list starts on "cwd", the second entry is "nodes".
            await pilot.press("space", "down", "space", "enter")
            self.assertIn("Path", self.headers(app, "RUNNING"))
            self.assertNotIn("Nodes", self.headers(app, "RUNNING"))
            self.run_mock.assert_called_once()

    async def test_arrows_continue_into_the_next_table(self):
        app = tui.SjobsApp()
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(app, pilot)
            self.assertEqual(app.focused.id, "table-RUNNING")
            await pilot.press("down")
            self.assertEqual(app.focused.id, "table-PENDING")
            self.assertEqual(app.selected_job()["job_id"], "1200")
            await pilot.press("down", "down")
            self.assertEqual(app.focused.id, "table-OTHER")
            await pilot.press("down")
            self.assertEqual(app.focused.id, "table-OTHER")
            await pilot.press("up")
            self.assertEqual(app.focused.id, "table-PENDING")
            self.assertEqual(app.selected_job()["job_id"], "1100_[1-4]")

    async def test_small_window_scrolls_and_widening_shows_more(self):
        app = tui.SjobsApp(columns=("cwd", "nodes", "partition"))
        async with app.run_test(size=(50, 30)) as pilot:
            await self.settle(app, pilot)
            table = app.query_one("#table-RUNNING")
            widths = [column.width for column in table.columns.values()]
            self.assertGreater(table.max_scroll_x, 0)
            await pilot.press("right", "right")
            self.assertGreater(table.scroll_x, 0)
            await pilot.resize_terminal(200, 30)
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(table.max_scroll_x, 0)
            self.assertEqual([c.width for c in table.columns.values()], widths)

    async def test_filter_and_clear(self):
        app = tui.SjobsApp()
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(app, pilot)
            # "q" goes into the filter instead of quitting.
            await pilot.press("slash", "q", "u", "e")
            self.assertEqual(app.filter_text, "que")
            self.assertEqual(self.row_counts(app), [0, 1, 0])
            label = str(app.query_one("#label-PENDING").render())
            self.assertIn("1 of 2", label)
            await pilot.press("escape")
            self.assertEqual(self.row_counts(app), [1, 2, 1])
            self.assertTrue(app.is_running)

    async def test_enter_opens_details(self):
        app = tui.SjobsApp()
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(app, pilot)
            await pilot.press("enter")
            self.assertIsInstance(app.screen, tui.JobDetail)
            text = str(app.screen.query_one("#body").render())
            self.assertIn("A100 (80GB)", text)
            self.assertIn("/scratch_shared/demo/run 01", text)
            await pilot.press("escape")
            self.assertNotIsInstance(app.screen, tui.JobDetail)
            await pilot.press("d")
            self.assertIsInstance(app.screen, tui.JobDetail)

    async def test_cancel_needs_confirmation(self):
        app = tui.SjobsApp(host="cluster")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(app, pilot)
            await pilot.press("c")
            self.assertIsInstance(app.screen, tui.ConfirmCancel)
            await pilot.press("n")
            await self.settle(app, pilot)
            self.assertEqual(self.run_mock.call_count, 1)

            await pilot.press("c", "y")
            await self.settle(app, pilot)
            await self.settle(app, pilot)
            commands = [call.args[0] for call in self.run_mock.call_args_list]
            self.assertEqual(
                commands[1], sjobs.build_scancel_cmdline("1000_0", host="cluster")
            )
            # The cancel is followed by one refresh.
            self.assertEqual(len(commands), 3)

    async def test_failed_refresh_keeps_the_tables(self):
        app = tui.SjobsApp()
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(app, pilot)
            self.run_mock.side_effect = subprocess.TimeoutExpired([], 60)
            app.refresh_jobs()
            await self.settle(app, pilot)
            self.assertEqual(self.row_counts(app), [1, 2, 1])
            self.assertIn("did not finish", str(app.query_one("#summary").render()))

    async def test_manual_refresh_is_rate_limited(self):
        app = tui.SjobsApp()
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(app, pilot)
            await pilot.press("r")
            await self.settle(app, pilot)
            self.run_mock.assert_called_once()
            app.last_request -= tui.MIN_MANUAL_REFRESH
            await pilot.press("r")
            await self.settle(app, pilot)
            self.assertEqual(self.run_mock.call_count, 2)

    async def test_all_users_refetches_without_me(self):
        app = tui.SjobsApp()
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(app, pilot)
            await pilot.press("a")
            await self.settle(app, pilot)
            script = self.run_mock.call_args.args[0][2]
            self.assertNotIn("--me", script)
            self.assertIn("User", self.headers(app, "RUNNING"))


class ChangeTests(AppTestCase):
    async def test_changes_are_announced(self):
        app = tui.SjobsApp(config={"desktop_notifications": True})
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(app, pilot)
            started = script_output(
                SNAPSHOT.replace('"job_state": "PENDING"', '"job_state": "RUNNING"')
            )
            self.run_mock.return_value = ok(started)
            with (
                patch.object(app, "notify") as notify,
                patch.object(app, "desktop_notify") as desktop,
            ):
                app.refresh_jobs()
                await self.settle(app, pilot)
            self.assertIn("1200 queued started", notify.call_args_list[0].args[0])
            desktop.assert_called_once()

    async def test_many_jobs_switch_to_tabs(self):
        app = tui.SjobsApp(show_all=True)
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(app, pilot)
            self.assertTrue(app.tabbed)
            visible = [table.id for table in app.tables() if table.display]
            self.assertEqual(visible, ["table-RUNNING"])
            await pilot.press("2")
            visible = [table.id for table in app.tables() if table.display]
            self.assertEqual(visible, ["table-PENDING"])
            self.assertEqual(app.focused.id, "table-PENDING")


class ThemeTests(unittest.TestCase):
    REPLY = (
        b"\x1b]10;rgb:5858/6e6e/7575\x1b\\"
        b"\x1b]11;rgb:fdfd/f6f6/e3e3\x07"
        + b"".join(b"\x1b]4;%d;rgb:10/20/%02x\x1b\\" % (i, i) for i in range(16))
        + b"\x1b[?62;22c"
    )

    def test_color_reports_are_parsed(self):
        colors = tui.parse_color_reports(self.REPLY)
        self.assertEqual(colors["foreground"], "#586e75")
        self.assertEqual(colors["background"], "#fdf6e3")
        self.assertEqual(colors["palette"][4], "#102004")
        # Nothing reported: nothing known.
        empty = tui.parse_color_reports(b"\x1b[?62c")
        self.assertEqual(empty["palette"], [None] * 16)
        self.assertIsNone(empty["background"])

    def test_theme_from_the_terminal_colors(self):
        theme, ansi = tui.terminal_themes(tui.parse_color_reports(self.REPLY))
        self.assertFalse(theme.dark)
        self.assertEqual(theme.background, "#fdf6e3")
        self.assertEqual(theme.primary, "#102004")
        self.assertIsNotNone(ansi)
        self.assertEqual(tui.terminal_themes(None), (None, None))

    def test_auto_theme_falls_back_to_textual_themes(self):
        for colors, expected in (
            (tui.parse_color_reports(self.REPLY), tui.TERMINAL_THEME),
            (None, "textual-dark"),
            (
                {"background": "#ffffff", "foreground": None, "palette": [None] * 16},
                "textual-light",
            ),
        ):
            with self.subTest(expected=expected):
                app = tui.SjobsApp(terminal_colors=colors)
                app.apply_theme(tui.AUTO_THEME)
                self.assertEqual(app.theme, expected)
        app = tui.SjobsApp(config={"theme": "nord"})
        app.apply_theme("nord")
        self.assertEqual(app.theme, "nord")


class DetailTests(unittest.TestCase):
    def test_help_explains_sjobs(self):
        text = tui.HelpScreen(tui.SjobsApp.KEYS, 60).body.plain
        self.assertIn("Keys", text)
        self.assertIn("What you see", text)
        self.assertIn("refresh every 60 s", text)
        self.assertNotIn("Fair share", text)

    def test_details_skip_empty_fields(self):
        job = sjobs.normalize_job({"job_id": 7, "name": "x", "nodes": "", "a": []})
        text = tui.detail_text(job).plain
        self.assertIn("ID", text)
        self.assertNotIn("Nodes", text)
        self.assertEqual(click.unstyle(text), text)


if __name__ == "__main__":
    unittest.main()
