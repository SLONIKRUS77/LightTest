import csv
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import main


def sample_result():
    return {
        "application": "LightTest CLI",
        "ip": "192.168.1.1",
        "isp": "Example ISP",
        "server": "Frankfurt, DE",
        "ping_ms": 12.4,
        "jitter_ms": 1.2,
        "download_mbps": 894.6,
        "upload_mbps": 655.1,
        "timestamp": "2026-09-25 01:34:00",
        "baseline_pings": [
            {
                "name": "Cloudflare",
                "host": "1.1.1.1",
                "method": "icmp",
                "samples_ms": [12.0, 13.0, 12.2],
                "average_ms": 12.4,
                "jitter_ms": 1.0,
                "status": "ok",
                "error": None,
            }
        ],
        "warnings": [],
    }


class ResultBoxTests(unittest.TestCase):
    def test_box_has_requested_ascii_frame_and_fixed_width(self):
        lines = main.raw_result_box(sample_result()).splitlines()

        self.assertEqual(lines[0], "+" + "=" * 40 + "+")
        self.assertEqual(lines[1], "|             LIGHTTEST CLI              |")
        self.assertEqual(lines[2], "|" + "-" * 40 + "|")
        self.assertEqual(lines[-1], lines[0])
        self.assertTrue(all(len(line) == 42 for line in lines))
        self.assertTrue(any(line.startswith("| IP:     192.168.1.1 (Example ISP)") for line in lines))
        self.assertTrue(any(line.startswith("| Ping:   12.4 ms | Jitter: 1.2 ms") for line in lines))
        self.assertTrue(any(line.startswith("| DOWNLOAD: 894.6 Mbps") for line in lines))

    def test_box_contains_only_ascii_characters(self):
        rendered = main.raw_result_box(sample_result())
        self.assertTrue(rendered.isascii())


class PingTests(unittest.TestCase):
    def test_ping_parser_and_jitter(self):
        completed = mock.Mock(
            stdout=(
                "64 bytes from 1.1.1.1: time=10.0 ms\n"
                "64 bytes from 1.1.1.1: time<1 ms\n"
                "64 bytes from 1.1.1.1: time=14.0 ms\n"
            ),
            stderr="",
        )
        with mock.patch("main.shutil.which", return_value="/usr/bin/ping"):
            with mock.patch("main.subprocess.run", return_value=completed):
                result = main.ping_host("Cloudflare", "1.1.1.1", 2)

        self.assertEqual(result["samples_ms"], [10.0, 0.5, 14.0])
        self.assertEqual(result["jitter_ms"], 11.5)
        self.assertEqual(result["status"], "ok")

    def test_ping_summary_ignores_failed_hosts(self):
        average, jitter = main.ping_summary(
            [
                {"samples_ms": [10.0, 14.0], "jitter_ms": 4.0},
                {"samples_ms": [], "jitter_ms": None},
            ]
        )
        self.assertEqual(average, 12.0)
        self.assertEqual(jitter, 4.0)


class ExportTests(unittest.TestCase):
    def test_all_export_formats_append_history(self):
        result = sample_result()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch(
                "main.export_path",
                side_effect=lambda export_format: root
                / "lighttest_history.{}".format(export_format),
            ):
                for export_format in ("csv", "json", "txt"):
                    main.export_result(result, export_format)
                    main.export_result(result, export_format)

            with (root / "lighttest_history.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)

            json_history = json.loads(
                (root / "lighttest_history.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(json_history), 2)
            self.assertEqual(json_history[0]["ip"], "192.168.1.1")

            text_history = (root / "lighttest_history.txt").read_text(encoding="utf-8")
            self.assertEqual(text_history.count("LIGHTTEST CLI"), 2)

    def test_json_export_does_not_overwrite_invalid_history(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "lighttest_history.json"
            destination.write_text("{broken", encoding="utf-8")
            with mock.patch("main.export_path", return_value=destination):
                with self.assertRaises(ValueError):
                    main.export_result(sample_result(), "json")
            self.assertEqual(destination.read_text(encoding="utf-8"), "{broken")


class CliTests(unittest.TestCase):
    def test_json_mode_writes_machine_readable_stdout(self):
        stdout = StringIO()
        with mock.patch("main.run_speed_test", return_value=sample_result()):
            with redirect_stdout(stdout):
                with redirect_stderr(StringIO()):
                    exit_code = main.main(["--json"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["server"], "Frankfurt, DE")

    def test_share_mode_is_raw_ascii(self):
        stdout = StringIO()
        with mock.patch("main.run_speed_test", return_value=sample_result()):
            with redirect_stdout(stdout):
                with redirect_stderr(StringIO()):
                    exit_code = main.main(["--share"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(stdout.getvalue().isascii())
        self.assertIn("+" + "=" * 40 + "+", stdout.getvalue())
        self.assertNotIn("\x1b[", stdout.getvalue())

    def test_simple_and_compact_modes(self):
        for mode in ("--simple", "--compact"):
            stdout = StringIO()
            with mock.patch("main.run_speed_test", return_value=sample_result()):
                with redirect_stdout(stdout):
                    with redirect_stderr(StringIO()):
                        exit_code = main.main([mode])
            self.assertEqual(exit_code, 0)
            if mode == "--compact":
                self.assertEqual(len(stdout.getvalue().splitlines()), 1)
            else:
                self.assertIn("Download: 894.6 Mbps", stdout.getvalue())

    def test_timeout_must_be_positive(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as error:
                main.build_parser().parse_args(["--timeout", "0"])
        self.assertEqual(error.exception.code, 2)

    def test_public_lookup_and_ping_run_before_speed_measurement(self):
        events = []
        tester = mock.Mock()
        tester.get_best_server.side_effect = lambda: (
            events.append("server"),
            {"name": "Frankfurt", "cc": "DE"},
        )[1]
        tester.download.side_effect = lambda: (events.append("download"), 894600000)[1]
        tester.upload.side_effect = lambda: (events.append("upload"), 655100000)[1]

        def lookup(_timeout):
            events.append("lookup")
            return {"ip": "203.0.113.1", "isp": "Example ISP"}, None

        def ping(name, host, _timeout):
            events.append("ping:" + name)
            return {
                "name": name,
                "host": host,
                "method": "icmp",
                "samples_ms": [10.0, 11.0, 12.0],
                "average_ms": 11.0,
                "jitter_ms": 1.0,
                "status": "ok",
                "error": None,
            }

        def make_speedtest(**_kwargs):
            events.append("speedtest")
            return tester

        with mock.patch("main.lookup_public_ip", side_effect=lookup):
            with mock.patch("main.ping_host", side_effect=ping):
                with mock.patch("main.speedtest.Speedtest", side_effect=make_speedtest):
                    result = main.run_speed_test(2)

        self.assertEqual(events, ["lookup", "ping:Cloudflare", "ping:Google", "speedtest", "server", "download", "upload"])
        self.assertEqual(result["server"], "Frankfurt, DE")
        self.assertEqual(result["download_mbps"], 894.6)


if __name__ == "__main__":
    unittest.main()