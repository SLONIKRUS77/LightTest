# LightTest CLI

**LightTest CLI** is an ultra-lightweight network speed test for low-spec
computers, small servers, and headless Linux machines. It has no graphical
interface or background service: run a test when you need one, then exit.

Each run looks up your public IP and ISP, checks baseline ICMP latency to
Cloudflare (`1.1.1.1`) and Google (`8.8.8.8`), and measures download and upload
speeds using `speedtest-cli`. Results appear in a cyan-and-purple terminal box
and can be saved locally as CSV, JSON, or text.

## Requirements

- Python 3.8 or later
- The system `ping` command for baseline latency checks
- An internet connection

The public IP lookup uses ipinfo's unauthenticated HTTPS endpoint. The speed
measurement connects to a Speedtest.net server selected by `speedtest-cli`.
These services necessarily receive your network's public IP while the test
runs. The public IP lookup can fail independently; a failed lookup is reported
and the speed test continues.

## Install from source

```bash
python3 -m pip install -r requirements.txt
python3 -m pip install .
lighttest
```

You can also run it without installing the command:

```bash
python3 main.py
```

## Linux standalone binary

Build the one-file executable on the Linux machine and CPU architecture where
you intend to run it:

```bash
python3 -m pip install -r requirements.txt
python3 -m PyInstaller --clean --onefile --name lighttest main.py
./dist/lighttest
```

The resulting `dist/lighttest` includes Python and the application packages.
PyInstaller binaries are operating-system and architecture specific; a Linux
binary built on one system is not guaranteed to work on every Linux distribution.

## Options

```text
lighttest [--simple | --json | --share | --compact]
          [--export {csv,json,txt}]
          [--timeout SECONDS]
```

- `--simple` prints a plain-text, multi-line report.
- `--json` writes the complete result, including ping details, as JSON to stdout.
- `--share` prints the uncolored ASCII results box for copying into chat.
- `--compact` prints a single-line result for cron jobs and scripts.
- `--export csv|json|txt` appends the result to `lighttest_history.csv`,
  `lighttest_history.json`, or `lighttest_history.txt` in the current directory.
- `--timeout SECONDS` sets network request and ping timeouts (default: `10`).

Examples:

```bash
lighttest --share
lighttest --simple --export csv
lighttest --json --timeout 5
lighttest --compact >> speed-history.log
```

Output modes are mutually exclusive. Exports can be combined with any output
mode. JSON history is stored as an array of results; CSV and text history are
appended as new rows or framed reports.

## Low-spec design

LightTest is a small, on-demand CLI rather than a dashboard or monitoring
daemon. It keeps the workflow direct and avoids a browser, database, or
always-running process. A speed test still transfers real data and uses
resources in proportion to the selected test server and network connection.