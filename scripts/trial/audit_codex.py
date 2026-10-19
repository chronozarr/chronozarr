"""Summarise simulated newcomer sessions written by run-codex-newcomer.sh.

    uv run python scripts/trial/audit_codex.py DIR [DIR ...]

DIR is <main checkout>/data/trial/newcomer-N. For each session it writes DIR/summary.json and
prints one table row. Minutes to link is the time from the session start to the first command
output that holds a viewer link to a trycloudflare.com store; the recipient columns come from
recipient.sh. Commands that left the container or read chronozarr's source are rule breaks.
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ZSH_WRAPPER_DOUBLE = re.compile(r'^/bin/(?:ba|z)sh -lc "(.*)"$', re.DOTALL)
ZSH_WRAPPER_SINGLE = re.compile(r"^/bin/(?:ba|z)sh -lc '(.*)'$", re.DOTALL)
BLOCK_HEADER = re.compile(r"^### (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) newcomer-\d+$", re.MULTILINE)
VIEWER_LINK = re.compile(r"chronozarr\.org/demo/\?\S*store=\S*trycloudflare\.com", re.IGNORECASE)
# Reading the package source or looking outside the container breaks the brief's rules.
RULE_BREAKS = [
    (
        re.compile(r"site-packages/chronozarr|inspect\.getsource|\.__file__"),
        "read chronozarr source",
    ),
    (re.compile(r"github\.com|pypi\.org|raw\.githubusercontent"), "left chronozarr.org for help"),
    (re.compile(r"(?<![\w./-])(?:/Users|~/|\$HOME)"), "read the host outside its directory"),
]


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def unwrap(command: str) -> str:
    """The command Codex ran, without the shell wrapper it adds around every call."""
    text = command.strip()
    double = ZSH_WRAPPER_DOUBLE.match(text)
    if double:
        return double.group(1).replace('\\"', '"')
    single = ZSH_WRAPPER_SINGLE.match(text)
    if single:
        return single.group(1).replace("'\\''", "'")
    return text


def load_commands(events_path: Path) -> list[dict]:
    commands = []
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") == "command_execution":
            commands.append(
                {"command": unwrap(item.get("command", "")), "exit_code": item.get("exit_code")}
            )
    return commands


def rule_breaks(commands: list[dict]) -> list[dict]:
    found = []
    for entry in commands:
        text = entry["command"]
        inside = text.startswith("./x ")
        for pattern, label in RULE_BREAKS:
            # A container path such as ~/data is fine inside ./x; on the host it is not.
            if label == "read the host outside its directory" and inside:
                continue
            if pattern.search(text):
                found.append({"rule": label, "command": text[:300]})
        if not inside and not text.startswith(
            ("curl ", "date", "sleep ", "ls", "pwd", "cat x", "cat ./x")
        ):
            found.append({"rule": "command outside the container (review)", "command": text[:300]})
    return found


def first_link_time(session_log: Path) -> datetime | None:
    text = session_log.read_text(errors="replace") if session_log.exists() else ""
    headers = list(BLOCK_HEADER.finditer(text))
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        if VIEWER_LINK.search(text[header.end() : end]):
            return parse_time(header.group(1))
    return None


def start_and_end(start_txt: Path) -> tuple[datetime, datetime | None, str]:
    text = start_txt.read_text()
    started = re.search(r"session:\s+started (\S+)", text)
    ended = re.search(r"session:\s+ended (\S+), codex exit (\S+)", text)
    if not started:
        raise SystemExit(
            f"{start_txt}: no 'session: started' line; was this run by run-codex-newcomer.sh?"
        )
    return (
        parse_time(started.group(1)),
        parse_time(ended.group(1)) if ended else None,
        ended.group(2) if ended else "not ended",
    )


def recipient_view(recipient_txt: Path) -> dict:
    text = recipient_txt.read_text(errors="replace") if recipient_txt.exists() else ""
    painted = re.search(r"first painted (\d+) ms", text)
    viewer = re.search(r"^viewer:\s+(.*)$", text, re.MULTILINE)
    position = re.search(r"^position:\s+(.*)$", text, re.MULTILINE)
    errors = re.search(r"console errors \((\d+)\)", text)
    return {
        "opened": bool(painted) and "frame complete" in text,
        "first_frame_ms": int(painted.group(1)) if painted else None,
        "viewer": viewer.group(1) if viewer else None,
        "position": position.group(1) if position else None,
        "console_errors": int(errors.group(1)) if errors else None,
    }


def month_of(text: str | None) -> str | None:
    if not text:
        return None
    iso = re.search(r"(\d{4})-(\d{2})", text)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}"
    named = re.search(r"([A-Z][a-z]{2})[a-z]*\.? (\d{4})", text)
    if named:
        try:
            return datetime.strptime(f"{named.group(1)} {named.group(2)}", "%b %Y").strftime(
                "%Y-%m"
            )
        except ValueError:
            return None
    return None


def summarise(session_dir: Path) -> dict:
    started, ended, codex_exit = start_and_end(session_dir / "start.txt")
    commands = load_commands(session_dir / "codex-events.jsonl")
    answer_path = session_dir / "final.json"
    answer = (
        json.loads(answer_path.read_text())
        if answer_path.exists() and answer_path.stat().st_size
        else {}
    )
    link_time = first_link_time(session_dir / "session.log")
    recipient = recipient_view(session_dir / "recipient.txt")
    chosen_month = month_of(answer.get("chosen_date"))
    shown_month = month_of(recipient["viewer"])
    tasks = {str(task["task"]): task["status"] for task in answer.get("tasks", [])}
    return {
        "session": session_dir.name,
        "codex_exit": codex_exit,
        "minutes_total": round((ended - started).total_seconds() / 60, 1) if ended else None,
        "minutes_to_link": round((link_time - started).total_seconds() / 60, 1)
        if link_time
        else None,
        "tasks": tasks,
        "commands": len(commands),
        "failed_commands": sum(1 for entry in commands if entry["exit_code"] not in (0, None)),
        "errors_reported": sum(len(task.get("errors", [])) for task in answer.get("tasks", [])),
        "uncertain_moments": sum(
            len(task.get("uncertain_moments", [])) for task in answer.get("tasks", [])
        ),
        "rule_breaks": rule_breaks(commands),
        "share_link": answer.get("share_link"),
        "chosen_date": answer.get("chosen_date"),
        "chosen_place": answer.get("chosen_place"),
        "recipient": recipient,
        "date_arrived": None
        if not (chosen_month and shown_month)
        else chosen_month == shown_month,
        "pixel_result": answer.get("pixel_result"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("sessions", nargs="+", type=Path)
    args = parser.parse_args()
    columns = ["session", "min to link", "tasks", "recipient frame ms", "date arrived"]
    columns += ["failed cmds", "rule breaks"]
    print("| " + " | ".join(columns) + " |")
    print("|---|---:|---|---:|---|---:|---:|")
    for session_dir in args.sessions:
        summary = summarise(session_dir)
        (session_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        tasks = (
            " ".join(f"{key}:{value}" for key, value in summary["tasks"].items()) or "no answer"
        )
        print(
            f"| {summary['session']} | {summary['minutes_to_link']} | {tasks} | "
            f"{summary['recipient']['first_frame_ms']} | {summary['date_arrived']} | "
            f"{summary['failed_commands']} | {len(summary['rule_breaks'])} |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
