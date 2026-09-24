#!/usr/bin/env python3
"""LightTest CLI: a compact network speed test for desktop and server use."""

import argparse
import csv
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
import speedtest
from rich.console import Console
from rich.text import Text


IP_LOOKUP_URL = "https://ipinfo.io/json"
PING_TARGETS = (
    ("Cloudflare", "1.1.1.1"),
    ("Google", "8.8.8.8"),
)
BOX_WIDTH = 40
PING_PATTERN = re.compile(r"time\s*([=<])?\s*(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)
CSV_FIELDS = (
    "timestamp",
    "ip",
    "isp",
    "server",
    "ping_ms",
    "jitter_ms",
    "download_mbps",
    "upload_mbps",
    "baseline_pings",
)

OUTPUT = Console()
STATUS = Console(stderr=True)


def positive_timeout(value: str) -> float:
    """Parse a positive timeout in seconds."""
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number of seconds") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return timeout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lighttest",
        description=(
            "A lightweight network speed test with public IP lookup, "
            "baseline ping checks, and local result exports."
        ),
    )
    output_mode = parser.add_mutually_exclusive_group()
    output_mode.add_argument(
        "--simple",
        action="store_true",
        help="print a plain-text report without the framed UI",
    )
    output_mode.add_argument(
        "--json",
        action="store_true",
        help="print the full result as JSON to stdout",
    )
    output_mode.add_argument(
        "--share",
        action="store_true",
        help="print the raw, uncolored ASCII results box",
    )
    output_mode.add_argument(
        "--compact",
        action="store_true",
        help="print one machine-friendly line",
    )
    parser.add_argument(
        "--export",
        choices=("csv", "json", "txt"),
        metavar="{csv,json,txt}",
        help="append the result to lighttest_history.<format>",
    )
    parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=10.0,
        metavar="SECONDS",
        help="network request timeout in seconds (default: 10)",
    )
    return parser


def clean_text(value: Any, fallback: str = "Unknown") -> str:
    """Normalize remote text before displaying it in a terminal."""
    if value is None:
        return fallback
    text = " ".join(str(value).split())
    text = "".join(character for character in text if character.isprintable())
    return text or fallback


def lookup_public_ip(timeout: float) -> Tuple[Dict[str, str], Optional[str]]:
    """Look up public IP and ISP metadata using ipinfo's unauthenticated HTTPS API."""
    try:
        response = requests.get(
            IP_LOOKUP_URL,
            timeout=timeout,
            headers={"User-Agent": "LightTest-CLI/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
        ip = clean_text(payload.get("ip"), "")
        org = clean_text(payload.get("org"), "Unknown ISP")
        if not ip:
            return {"ip": "Unknown", "isp": org}, "IP lookup did not return an IP address."
        # ipinfo's org field commonly includes an AS number before the provider.
        isp = re.sub(r"^AS\d+\s+", "", org)
        return {"ip": ip, "isp": isp or "Unknown ISP"}, None
    except (requests.RequestException, ValueError) as exc:
        return (
            {"ip": "Unknown", "isp": "Unknown ISP"},
            "Public IP lookup failed: {}".format(exc),
        )


def _ping_command(host: str, timeout: float) -> List[str]:
    """Build a one-invocation, three-packet ping command for the current OS."""
    if platform.system().lower() == "windows":
        return [
            "ping",
            "-n",
            "3",
            "-w",
            str(max(1, int(timeout * 1000))),
            host,
        ]
    return ["ping", "-n", "-c", "3", "-W", str(max(1, int(math.ceil(timeout)))), host]


def ping_host(name: str, host: str, timeout: float) -> Dict[str, Any]:
    """Measure ICMP response times and per-host jitter using the OS ping utility."""
    if shutil.which("ping") is None:
        return {
            "name": name,
            "host": host,
            "method": "icmp",
            "samples_ms": [],
            "average_ms": None,
            "jitter_ms": None,
            "status": "unavailable",
            "error": "The system 'ping' command was not found.",
        }
    try:
        completed = subprocess.run(
            _ping_command(host, timeout),
            capture_output=True,
            text=True,
            check=False,
            timeout=max(2.0, min(timeout * 3 + 2, 60.0)),
        )
    except subprocess.TimeoutExpired:
        return {
            "name": name,
            "host": host,
            "method": "icmp",
            "samples_ms": [],
            "average_ms": None,
            "jitter_ms": None,
            "status": "timeout",
            "error": "Ping timed out after {:.1f} seconds.".format(timeout),
        }
    except OSError as exc:
        return {
            "name": name,
            "host": host,
            "method": "icmp",
            "samples_ms": [],
            "average_ms": None,
            "jitter_ms": None,
            "status": "error",
            "error": str(exc),
        }

    output = "{}\n{}".format(completed.stdout or "", completed.stderr or "")
    samples = []
    for match in PING_PATTERN.finditer(output):
        milliseconds = float(match.group(2))
        # Preserve sub-millisecond replies as a readable estimate instead of 0.
        if match.group(1) == "<" and milliseconds == 1:
            milliseconds = 0.5
        samples.append(round(milliseconds, 3))
    differences = [
        abs(right - left) for left, right in zip(samples, samples[1:])
    ]
    return {
        "name": name,
        "host": host,
        "method": "icmp",
        "samples_ms": samples,
        "average_ms": round(statistics.mean(samples), 3) if samples else None,
        "jitter_ms": round(statistics.mean(differences), 3) if differences else None,
        "status": "ok" if samples else "unreachable",
        "error": None if samples else "No ICMP replies received.",
    }


def ping_summary(pings: Sequence[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    """Aggregate successful baseline measurements into ping and jitter values."""
    samples = [
        value
        for result in pings
        for value in result.get("samples_ms", [])
    ]
    jitter_values = [
        result["jitter_ms"]
        for result in pings
        if result.get("jitter_ms") is not None
    ]
    average = round(statistics.mean(samples), 1) if samples else None
    jitter = round(statistics.mean(jitter_values), 1) if jitter_values else None
    return average, jitter


def server_label(server: Dict[str, Any]) -> str:
    city = clean_text(server.get("name"), "")
    country_code = clean_text(server.get("cc"), "")
    if city and country_code:
        return "{}, {}".format(city, country_code)
    if city:
        return city
    return clean_text(server.get("country"), "Unknown server")


def run_speed_test(timeout: float) -> Dict[str, Any]:
    """Collect metadata, ICMP baselines, and a speedtest-cli measurement."""
    warnings = []
    ip_info, ip_error = lookup_public_ip(timeout)
    if ip_error:
        warnings.append(ip_error)

    pings = [ping_host(name, host, timeout) for name, host in PING_TARGETS]
    for result in pings:
        if result["status"] != "ok":
            warnings.append(
                "{} ping check {}: {}".format(
                    result["name"], result["status"], result.get("error", "unknown error")
                )
            )

    tester = speedtest.Speedtest(secure=True, timeout=timeout)
    server = tester.get_best_server()
    download_mbps = tester.download() / 1_000_000
    upload_mbps = tester.upload() / 1_000_000
    ping_ms, jitter_ms = ping_summary(pings)

    return {
        "application": "LightTest CLI",
        "ip": ip_info["ip"],
        "isp": ip_info["isp"],
        "server": server_label(server),
        "ping_ms": ping_ms,
        "jitter_ms": jitter_ms,
        "download_mbps": round(download_mbps, 1),
        "upload_mbps": round(upload_mbps, 1),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "baseline_pings": pings,
        "warnings": warnings,
    }


def _row(parts: Sequence[Tuple[str, Optional[str]]]) -> Tuple[str, List[Tuple[int, int, str]]]:
    """Create a fixed-width frame row and styled character spans."""
    content = " "
    spans = []
    for value, style in parts:
        start = len(content)
        content += value
        if style:
            spans.append((start + 1, min(start + len(value), BOX_WIDTH - 1) + 1, style))
    content = content[: BOX_WIDTH - 1].ljust(BOX_WIDTH - 1) + " "
    return "|" + content + "|", spans


def _standard_row(label: str, value: str) -> Tuple[str, List[Tuple[int, int, str]]]:
    ascii_value = clean_text(value).encode("ascii", "replace").decode("ascii")
    return _row(((label, "cyan"), (ascii_value, "bright_magenta")))


def format_result_box(result: Dict[str, Any]) -> List[Tuple[str, List[Tuple[int, int, str]]]]:
    """Return exact-width ASCII result lines plus Rich style spans."""
    ping = result.get("ping_ms")
    jitter = result.get("jitter_ms")
    ping_text = "{:.1f}".format(ping) if ping is not None else "N/A"
    jitter_text = "{:.1f}".format(jitter) if jitter is not None else "N/A"
    download = result.get("download_mbps")
    upload = result.get("upload_mbps")
    download_text = "{:.1f}".format(download) if download is not None else "N/A"
    upload_text = "{:.1f}".format(upload) if upload is not None else "N/A"

    top = "+" + ("=" * BOX_WIDTH) + "+"
    separator = "|" + ("-" * BOX_WIDTH) + "|"
    header_content = "LIGHTTEST CLI".center(BOX_WIDTH)
    header = "|" + header_content + "|"
    return [
        (top, [(0, len(top), "bold cyan")]),
        (header, [(0, len(header), "bold cyan")]),
        (separator, [(0, len(separator), "cyan")]),
        _standard_row("IP:     ", "{} ({})".format(result["ip"], result["isp"])),
        _standard_row("Server: ", result["server"]),
        _row(
            (
                ("Ping:   ", "cyan"),
                ("{} ms".format(ping_text), "bright_magenta"),
                (" | ", "cyan"),
                ("Jitter: ", "cyan"),
                ("{} ms".format(jitter_text), "bright_magenta"),
            )
        ),
        (separator, [(0, len(separator), "cyan")]),
        _standard_row("DOWNLOAD: ", "{} Mbps".format(download_text)),
        _standard_row("UPLOAD:   ", "{} Mbps".format(upload_text)),
        (separator, [(0, len(separator), "cyan")]),
        _standard_row("Time:   ", result["timestamp"]),
        (top, [(0, len(top), "bold cyan")]),
    ]


def raw_result_box(result: Dict[str, Any]) -> str:
    return "\n".join(line for line, _ in format_result_box(result))


def print_colored_box(result: Dict[str, Any]) -> None:
    for line, spans in format_result_box(result):
        text = Text(line)
        for start, end, style in spans:
            if start < end:
                text.stylize(style, start, end)
        OUTPUT.print(text, soft_wrap=True)


def _format_value(value: Any) -> str:
    return "N/A" if value is None else "{:.1f}".format(value)


def print_simple(result: Dict[str, Any]) -> None:
    print("LIGHTTEST CLI")
    print("IP: {} ({})".format(result["ip"], result["isp"]))
    print("Server: {}".format(result["server"]))
    print(
        "Ping: {} ms | Jitter: {} ms".format(
            _format_value(result["ping_ms"]), _format_value(result["jitter_ms"])
        )
    )
    print("Download: {} Mbps".format(_format_value(result["download_mbps"])))
    print("Upload: {} Mbps".format(_format_value(result["upload_mbps"])))
    print("Time: {}".format(result["timestamp"]))


def print_compact(result: Dict[str, Any]) -> None:
    print(
        "ip={} isp={!r} server={!r} ping_ms={} jitter_ms={} "
        "download_mbps={} upload_mbps={} time={}".format(
            result["ip"],
            result["isp"],
            result["server"],
            _format_value(result["ping_ms"]),
            _format_value(result["jitter_ms"]),
            _format_value(result["download_mbps"]),
            _format_value(result["upload_mbps"]),
            result["timestamp"],
        )
    )


def export_path(export_format: str, directory: Path = Path.cwd()) -> Path:
    return directory / "lighttest_history.{}".format(export_format)


def export_result(result: Dict[str, Any], export_format: str) -> Path:
    """Append a result to a local CSV, JSON-array, or plain-text history file."""
    destination = export_path(export_format)
    if export_format == "csv":
        row = dict(result)
        row["baseline_pings"] = json.dumps(result["baseline_pings"], ensure_ascii=False)
        with destination.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            if handle.tell() == 0:
                writer.writeheader()
            writer.writerow({field: row.get(field) for field in CSV_FIELDS})
        return destination

    if export_format == "txt":
        with destination.open("a", encoding="utf-8") as handle:
            if handle.tell() > 0:
                handle.write("\n")
            handle.write(raw_result_box(result))
            handle.write("\n")
        return destination

    if export_format == "json":
        if destination.exists():
            try:
                history = json.loads(destination.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("Existing JSON history is unreadable: {}".format(exc)) from exc
            if not isinstance(history, list):
                raise ValueError("Existing JSON history must contain an array of results.")
        else:
            history = []
        history.append(result)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(destination.parent),
                prefix=destination.name + ".",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_name = handle.name
                json.dump(history, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(temp_name, destination)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)
        return destination

    raise ValueError("Unsupported export format: {}".format(export_format))


def _report_progress(function, message: str, enabled: bool):
    if not enabled:
        return function()
    with STATUS.status("[cyan]{}[/cyan]".format(message), spinner="dots"):
        return function()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    show_progress = not any((args.simple, args.json, args.share, args.compact))
    try:
        # Keep the requested phase order visible: metadata, baseline, then speed test.
        result = _report_progress(
            lambda: run_speed_test(args.timeout),
            "Looking up IP, checking baseline latency, and measuring speed",
            show_progress,
        )
    except (speedtest.SpeedtestException, OSError, ValueError) as exc:
        print("LightTest could not complete the speed test: {}".format(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print("LightTest encountered an unexpected error: {}".format(exc), file=sys.stderr)
        return 1

    for warning in result["warnings"]:
        print("Warning: {}".format(warning), file=sys.stderr)

    if args.export:
        try:
            saved_path = export_result(result, args.export)
            print("Saved result to {}".format(saved_path), file=sys.stderr)
        except (OSError, ValueError) as exc:
            print("Could not export results: {}".format(exc), file=sys.stderr)
            return 1

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.simple:
        print_simple(result)
    elif args.compact:
        print_compact(result)
    elif args.share:
        print(raw_result_box(result))
    else:
        print_colored_box(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
