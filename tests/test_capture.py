from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest


@pytest.fixture
def server(request):
    statuses = iter(getattr(request, "param", [502, 200]))
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status = next(statuses, 200)
            seen.append(status)
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("CF-Ray", "test-ray-FRA")
            self.send_header("Set-Cookie", "secret-cookie")
            location = (
                f"http://localhost:{self.server.server_port}/secret-location"
                if status == 302
                else "/secret-location"
            )
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", seen
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def run_capture(
    tmp_path,
    server,
    *,
    hosts="127.0.0.1",
    enabled=True,
    ordinary=False,
    expected=200,
    blocked_output=False,
):
    url, _ = server
    script = (
        """
import requests
from conda.gateways.connection.session import CondaSession
from conda_cdn_log.plugin import start
from urllib3.util.retry import Retry
start("create")
start("create")
session = requests.Session() if ORDINARY else CondaSession()
if not ORDINARY:
    session.get_adapter(URL).max_retries = Retry(
        total=1, status_forcelist=[502], backoff_factor=0, raise_on_status=False
    )
session.trust_env = False
response = session.get(URL + "/private-package?token=secret-query", headers={
    "Authorization": "Bearer secret-auth",
    "X-Custom-Token": "secret-custom",
    "Accept": "application/json",
})
assert response.status_code == EXPECTED, response.status_code
""".replace("URL", repr(url))
        .replace("ORDINARY", repr(ordinary))
        .replace("EXPECTED", "502" if ordinary else str(expected))
    )
    if blocked_output:
        (tmp_path / "events").write_text("not a directory")
    env = {
        **os.environ,
        "CONDA_CDN_LOG_DIR": str(tmp_path / "events") if enabled else "",
        "CONDA_CDN_LOG_HOSTS": hosts,
        "CONDA_REMOTE_BACKOFF_FACTOR": "0",
        "CONDA_REMOTE_MAX_RETRIES": "1",
        "CONDA_OFFLINE": "false",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    records = [
        json.loads(line)
        for path in (tmp_path / "events").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    return records


def test_records_recovered_retry_without_changing_request(tmp_path, server):
    records = run_capture(tmp_path, server)
    assert server[1] == [502, 200]
    assert [event["status"] for event in records] == [502, 200]
    assert all(event["host"] == "127.0.0.1" for event in records)
    assert records[0]["request_headers"] == {"accept": "application/json"}
    assert records[0]["response_headers"]["cf-ray"] == "test-ray-FRA"
    assert "request_headers" not in records[1]
    serialized = json.dumps(records)
    assert "secret" not in serialized
    assert "private-package" not in serialized
    assert all(event["timestamp"].endswith("+00:00") for event in records)
    assert len({event["request_id"] for event in records}) == 1


@pytest.mark.parametrize("server", [[502, 502]], indirect=True)
def test_exhausted_status_retries_are_counted_once_each(tmp_path, server):
    records = run_capture(tmp_path, server, expected=502)
    assert server[1] == [502, 502]
    assert [event["status"] for event in records] == [502, 502]


def test_write_failure_preserves_successful_response(tmp_path, server):
    assert run_capture(tmp_path, server, blocked_output=True) == []
    assert server[1] == [502, 200]


@pytest.mark.parametrize("server", [[302, 200]], indirect=True)
@pytest.mark.parametrize(
    ("hosts", "statuses"),
    [("127.0.0.1", [302]), ("localhost", [200]), ("127.0.0.1.example.org", [])],
)
def test_redirects_recheck_exact_host(tmp_path, server, hosts, statuses):
    records = run_capture(tmp_path, server, hosts=hosts)
    assert server[1] == [302, 200]
    assert [event["status"] for event in records] == statuses
    assert all(event["host"] == hosts for event in records)


@pytest.mark.parametrize("server", [[502] * 100], indirect=True)
def test_real_conda_failure_activates_capture(tmp_path, server):
    url, _ = server
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "conda",
            "search",
            "--override-channels",
            "-c",
            url,
            "--json",
            "zlib",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "CONDA_CDN_LOG_DIR": str(tmp_path / "events"),
            "CONDA_CDN_LOG_HOSTS": "127.0.0.1",
            "CONDA_REMOTE_MAX_RETRIES": "0",
            "CONDA_REPORT_ERRORS": "false",
            "CONDA_NOTIFY_OUTDATED_CONDA": "false",
            "CONDA_PKGS_DIRS": str(tmp_path / "pkgs"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, result.stderr
    assert json.loads(result.stdout)["exception_name"] == "CondaHTTPError"
    records = [
        json.loads(line)
        for path in (tmp_path / "events").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert records
    assert all(event["status"] == 502 for event in records)


def test_connection_failure_is_preserved_and_recorded(tmp_path):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{reserved.getsockname()[1]}/secret-path?secret-query"
        script = f"""
from requests.exceptions import ConnectionError, Timeout
from conda.gateways.connection.session import CondaSession
from conda_cdn_log.plugin import start
start("create")
session = CondaSession()
session.trust_env = False
try:
    session.get({url!r}, timeout=0.1)
except ConnectionError as error:
    print("Timeout" if isinstance(error, Timeout) else "ConnectionError")
else:
    raise AssertionError("Expected ConnectionError")
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            env={
                **os.environ,
                "CONDA_CDN_LOG_DIR": str(tmp_path),
                "CONDA_CDN_LOG_HOSTS": "127.0.0.1",
                "CONDA_REMOTE_MAX_RETRIES": "0",
            },
            capture_output=True,
            text=True,
        )
    assert result.returncode == 0, result.stderr
    records = [
        json.loads(line)
        for path in tmp_path.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert len(records) == 1
    assert records[0]["event"] == "transport_error"
    assert records[0]["error_type"] == result.stdout.strip()
    assert "status" not in records[0]
    assert "secret" not in json.dumps(records)


@pytest.mark.parametrize(
    "options",
    [{"hosts": "example.org"}, {"enabled": False}, {"ordinary": True}],
)
def test_skips_unselected_traffic(tmp_path, server, options):
    assert run_capture(tmp_path, server, **options) == []


def test_writer_keeps_complete_records_across_threads(tmp_path):
    from conda_cdn_log.capture import Recorder

    recorder = Recorder(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda n: recorder.write({"number": n}), range(100)))
    records = [
        json.loads(line)
        for path in tmp_path.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert sorted(event["number"] for event in records) == list(range(100))


def test_write_failure_warns_once_without_exposing_path(tmp_path, capsys):
    from conda_cdn_log.capture import Recorder

    directory = tmp_path / "secret-path"
    directory.write_text("not a directory")
    recorder = Recorder(directory)
    recorder.write({"status": 502})
    recorder.write({"status": 200})
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.count("capture disabled") == 1
    assert "secret-path" not in output.err


def test_installed_discovery_is_lazy(tmp_path):
    script = """
import sys
from importlib.metadata import distribution
from conda.plugins.hookspec import CondaSpecs
from conda.plugins.manager import CondaPluginManager
all_entries = distribution("conda-cdn-log").entry_points
assert not [e for e in all_entries if e.group == "console_scripts"]
entries = [e for e in all_entries if e.group == "conda"]
assert len(entries) == 1
manager = CondaPluginManager()
manager.add_hookspecs(CondaSpecs)
assert manager.load_entrypoints("conda", name="conda-cdn-log") == 1
assert len(manager.get_hook_results("pre_commands")) == 1
import argparse
commands = manager.get_hook_results("subcommands")
assert [command.name for command in commands] == ["cdn-log"]
commands[0].configure_parser(argparse.ArgumentParser())
assert "conda_cdn_log.capture" not in sys.modules
assert "conda_cdn_log.summary" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_installed_conda_subcommand(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "conda", "cdn-log", str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "No HTTP observations" in result.stdout
