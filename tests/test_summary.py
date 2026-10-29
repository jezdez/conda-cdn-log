from __future__ import annotations

import json
from argparse import ArgumentParser

import pytest


def run_summary(argv):
    from conda_cdn_log.plugin import configure_parser
    from conda_cdn_log.summary import execute

    parser = ArgumentParser()
    configure_parser(parser)
    return execute(parser.parse_args(argv))


def test_summary_counts_responses_and_transport_failures_separately():
    from conda_cdn_log.summary import render

    events = [
        {"event": "response", "host": "repo.anaconda.com", "status": status}
        for status in [200, 304, 404, 502, 503]
    ] + [{"event": "transport_error", "host": "repo.anaconda.com"}]
    summary = render(events)
    assert "| repo.anaconda.com | 5 | 1 | 2 | 40.00% | 1 |" in summary
    assert "| repo.anaconda.com | 502 | 1 |" in summary
    assert "HTTP responses" in summary


def test_empty_summary_does_not_claim_zero_failure_rate():
    from conda_cdn_log.summary import render

    summary = render([])
    assert "No HTTP observations" in summary
    assert "0.00%" not in summary


def test_cli_appends_to_github_summary(tmp_path, monkeypatch, capsys):
    log = tmp_path / "events.jsonl"
    log.write_text(
        json.dumps({"event": "response", "host": "example.org", "status": 502}) + "\n"
    )
    summary = tmp_path / "summary.md"
    summary.write_text("Existing summary\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert run_summary([str(log), "--github-summary"]) == 0
    assert summary.read_text().startswith("Existing summary\n")
    assert "example.org" in summary.read_text()
    assert "example.org" in capsys.readouterr().out


def test_truncated_record_is_reported_without_echoing_content(tmp_path, capsys):
    log = tmp_path / "events.jsonl"
    log.write_text('{"private-path": "secret')
    assert run_summary([str(log)]) == 0
    output = capsys.readouterr()
    assert "1 malformed" in output.out
    assert "private-path" not in output.out + output.err
    assert "secret" not in output.out + output.err


def test_overlapping_inputs_count_each_file_once(tmp_path, monkeypatch, capsys):
    log = tmp_path / "events.jsonl"
    log.write_text(
        json.dumps({"event": "response", "host": "example.org", "status": 502}) + "\n"
    )
    monkeypatch.chdir(tmp_path)
    assert run_summary([".", str(log)]) == 0
    assert "| example.org | 1 | 0 | 1 | 100.00% | 0 |" in capsys.readouterr().out


def test_ipv6_host_is_included_in_summary(tmp_path, capsys):
    log = tmp_path / "events.jsonl"
    log.write_text(
        json.dumps({"event": "response", "host": "::1", "status": 502}) + "\n"
    )
    assert run_summary([str(log)]) == 0
    assert "| ::1 | 1 | 0 | 1 | 100.00% | 0 |" in capsys.readouterr().out


@pytest.mark.parametrize("host", ["example.org|forged-row", "::1%<img>"])
def test_invalid_hosts_cannot_inject_summary_markup(tmp_path, capsys, host):
    log = tmp_path / "events.jsonl"
    log.write_text(
        json.dumps({"event": "response", "host": host, "status": 502}) + "\n"
    )
    assert run_summary([str(log)]) == 0
    output = capsys.readouterr().out
    assert "1 malformed" in output
    assert host not in output
