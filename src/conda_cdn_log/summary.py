"""Summarize captured responses without importing conda or its transport."""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from ipaddress import IPv6Address
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from argparse import Namespace


def render(events: Iterable[dict[str, Any]]) -> str:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    statuses: Counter[tuple[str, int]] = Counter()
    for event in events:
        host = event["host"]
        if event["event"] == "transport_error":
            counts[host]["transport"] += 1
        else:
            status = event["status"]
            counts[host]["responses"] += 1
            counts[host][str(status // 100)] += 1
            if status >= 400:
                statuses[host, status] += 1
    lines = ["## Conda HTTP diagnostics", ""]
    if not counts:
        lines += [
            "No HTTP observations were recorded. "
            "Capture may be disabled or no eligible traffic occurred.",
            "",
        ]
    else:
        lines += [
            "| Host | HTTP responses | 4xx | 5xx | 5xx rate | "
            "Terminal transport failures |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for host, count in sorted(counts.items()):
            total = count["responses"]
            rate = f"{count['5'] / total:.2%}" if total else "n/a"
            lines.append(
                f"| {host} | {total} | {count['4']} | {count['5']} | "
                f"{rate} | {count['transport']} |"
            )
        lines += [""]
    if statuses:
        lines += ["| Host | HTTP status | Count |", "| --- | ---: | ---: |"]
        lines += [
            f"| {host} | {status} | {count} |"
            for (host, status), count in sorted(statuses.items())
        ]
        lines += [""]
    lines += [
        "Rates use observed HTTP responses, including retries. "
        "A 502 followed by 200 counts as two responses.",
        "4xx responses can include expected metadata fallback. "
        "These counts do not establish CDN fault.",
        "Terminal transport failures are counted separately. "
        "Native transports and response-body failures may be unobserved.",
        "",
    ]
    return "\n".join(lines)


def execute(args: Namespace) -> int:
    from conda.exceptions import CondaValueError

    files: set[Path] = set()
    for source in map(Path, args.inputs):
        candidates = source.rglob("*.jsonl") if source.is_dir() else [source]
        files.update(path.resolve() for path in candidates)
    events = []
    malformed = 0
    missing = 0
    for path in sorted(files):
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict) or not isinstance(
                            event.get("host"), str
                        ):
                            raise ValueError
                        if ":" in event["host"]:
                            if not re.fullmatch(r"[a-z0-9.:%_-]{1,253}", event["host"]):
                                raise ValueError
                            IPv6Address(event["host"])
                        elif not re.fullmatch(r"[a-z0-9.-]{1,253}", event["host"]):
                            raise ValueError
                        if event.get("event") == "response":
                            status = event.get("status")
                            if type(status) is not int or not 100 <= status <= 599:
                                raise ValueError
                        elif event.get("event") != "transport_error":
                            raise ValueError
                        events.append(event)
                    except (ValueError, TypeError):
                        malformed += 1
        except FileNotFoundError:
            missing += 1
        except (OSError, UnicodeError):
            raise CondaValueError("Unable to read a capture file.") from None
    output = render(events)
    if malformed or missing:
        output += (
            f"\nIncomplete input: {malformed} malformed records skipped, "
            f"{missing} missing inputs. Rates cover only readable records.\n"
        )
    if args.github_summary:
        destination = os.environ.get("GITHUB_STEP_SUMMARY")
        if not destination:
            raise CondaValueError("--github-summary requires GITHUB_STEP_SUMMARY")
        try:
            with Path(destination).open("a", encoding="utf-8") as stream:
                stream.write(output)
        except OSError:
            raise CondaValueError("Unable to append the GitHub job summary.") from None
    print(output)
    return 0
