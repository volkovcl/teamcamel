#!/usr/bin/env python3
"""Retakes docs/screenshot.png from made-up sessions, at the current version.

    python3 packaging/make_screenshot.py        (macOS, with Google Chrome installed)

A copy of the program runs in a temporary folder with a pretend home folder, so
neither this computer's sessions nor its state folder take part. Every session,
workspace and computer name in the picture is invented here.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 2266
MIN = 60000


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")


def iso(mins_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - mins_ago * 60))


def lstart(pid):
    return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                          env=dict(os.environ, LC_ALL="C", TZ="UTC")).stdout.strip()


def claude_sessions(home, sleepers):
    now = int(time.time() * 1000)
    store = home / "Library/Application Support/Claude/claude-code-sessions/a/o"
    count = [0]

    def session(title, folder, mins_ago, ending="done", live=None):
        count[0] += 1
        sid, cli = "local_demo%d" % count[0], "cli-demo%d" % count[0]
        write(store / (sid + ".json"), {"sessionId": sid, "cliSessionId": cli, "title": title,
                                        "originCwd": "/Users/sam/work/" + folder, "lastActivityAt": now - mins_ago * MIN})
        if live:
            proc = subprocess.Popen(["sleep", "600"], start_new_session=True)
            sleepers.append(proc)
            time.sleep(0.2)
            entry = {"pid": proc.pid, "sessionId": cli, "hostSessionId": sid, "procStart": lstart(proc.pid),
                     "name": title, "cwd": "/Users/sam/work/" + folder, "statusUpdatedAt": now - mins_ago * MIN,
                     "status": "waiting" if live[0] == "waiting" else "busy"}
            if live[0] == "waiting":
                entry["waitingFor"] = live[1]
            write(home / ".claude/sessions" / ("%d.json" % proc.pid), entry)
            return
        last = {"type": "assistant", "timestamp": iso(mins_ago),
                "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}}
        if ending == "dropped":
            last = {"type": "assistant", "timestamp": iso(mins_ago), "isApiErrorMessage": True,
                    "message": {"content": [{"type": "text", "text": "API Error: The socket connection was closed unexpectedly"}]}}
        lines = [{"type": "user", "timestamp": iso(mins_ago + 3), "message": {"content": "go"}}, last]
        write(home / ".claude/projects/demo" / (cli + ".jsonl"),
              "\n".join(json.dumps(x, separators=(",", ":")) for x in lines) + "\n")

    write(home / ".claude/nodename", "studio-mac\n")
    session("Draft the new pricing page", "website-redesign", 6, live=("waiting", "permission prompt"))
    session("Fix image loading on phones", "website-redesign", 25)
    session("Rewrite the About page", "website-redesign", 190)
    session("Set up the contact form", "website-redesign", 2900)
    session("Summarize the Q3 survey answers", "quarterly-report", 4, live=("busy",))
    session("Check the totals in the budget sheet", "quarterly-report", 50)
    session("Proofread the October issue", "newsletter", 14, ending="dropped")
    session("Collect links for the reading list", "newsletter", 300)


def codex_sessions(home):
    cx = home / ".codex"
    day = cx / "sessions" / time.strftime("%Y/%m/%d")
    day.mkdir(parents=True)
    plain = 'text(await tools.exec_command({cmd:"ls"}))'
    threads = [
        ("Chart the survey results", "quarterly-report", [
            {"type": "event_msg", "timestamp": iso(9), "payload": {"type": "task_started"}},
            {"type": "response_item", "timestamp": iso(8), "payload": {
                "type": "custom_tool_call", "name": "exec", "call_id": "a",
                "input": 'text(await tools.exec_command({cmd:"pip install","sandbox_permissions":"require_escalated"}))'}}]),
        ("Sort the photo scans by year", "photo-archive", [
            {"type": "event_msg", "timestamp": iso(42), "payload": {"type": "task_started"}},
            {"type": "response_item", "timestamp": iso(41), "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "a", "input": plain}},
            {"type": "response_item", "timestamp": iso(41), "payload": {"type": "custom_tool_call_output", "call_id": "a"}},
            {"type": "event_msg", "timestamp": iso(40), "payload": {"type": "task_complete"}}]),
    ]
    db = sqlite3.connect(str(cx / "state_5.sqlite"))
    db.execute("create table threads (id text, name text, title text, cwd text, rollout_path text, archived integer, "
               "updated_at_ms integer, thread_source text)")
    for i, (name, folder, records) in enumerate(threads):
        tid = "0000000%d-aaaa-7bbb-8ccc-00000000000%d" % (i, i)
        path = day / ("rollout-demo-%s.jsonl" % tid)
        meta = {"type": "session_meta", "timestamp": iso(60), "payload": {"id": tid, "cwd": "/Users/sam/work/" + folder}}
        write(path, "\n".join(json.dumps(r, separators=(",", ":")) for r in [meta] + records) + "\n")
        db.execute("insert into threads values (?,?,?,?,?,0,?,'user')",
                   (tid, name, name, "/Users/sam/work/" + folder, str(path), int(time.time() * 1000) - 40 * MIN))
    db.commit()
    db.close()


def other_computer(state, version):
    now = int(time.time() * 1000)

    def s(i, title, ws, st, detail, mins, attn, app=None):
        d = {"id": "office-%d" % i, "title": title, "ws": ws, "state": st, "detail": detail,
             "since": now - mins * MIN, "attention": attn, "live": True, "link": None}
        if app:
            d["app"] = app
        return d

    write(state / "office-pc.json", {
        "schema": 1, "node": "office-pc", "host": "OFFICE-PC", "os": "Windows", "version": version,
        "generatedAt": now - 25000, "sessions": [
            s(1, "Add the export button", "client-portal", "input", "input needed", 11, True),
            s(2, "Tidy up the login screen", "client-portal", "completed", "", 130, False),
            s(3, "Write the help page", "client-portal", "completed", "", 1500, False),
            s(4, "Merge the duplicate contacts", "data-cleanup", "working", "", 9, False, "codex"),
            s(5, "Convert all dates to one format", "data-cleanup", "completed", "", 1450, False)]})


def main():
    if not os.path.exists(CHROME):
        sys.exit("make_screenshot: Google Chrome is needed at %s" % CHROME)
    tmp = Path(tempfile.mkdtemp(prefix="teamcamel-shot-"))
    sleepers, server = [], None
    try:
        app, home = tmp / "app", tmp / "home"
        (app / "web").mkdir(parents=True)
        shutil.copy2(str(ROOT / "teamcamel.py"), str(app / "teamcamel.py"))
        shutil.copy2(str(ROOT / "web" / "index.html"), str(app / "web" / "index.html"))
        version = subprocess.run([sys.executable, str(app / "teamcamel.py"), "--version"],
                                 capture_output=True, text=True).stdout.split()[-1]
        claude_sessions(home, sleepers)
        codex_sessions(home)
        other_computer(app / "state", version)
        env = dict(os.environ, HOME=str(home))
        env.pop("CODEX_HOME", None)
        env.pop("CLAUDE_CONFIG_DIR", None)
        server = subprocess.Popen([sys.executable, str(app / "teamcamel.py"), "--port", str(PORT)], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % PORT, timeout=1) as resp:
                    names = [s["title"] for n in json.load(resp)["nodes"] for s in n["sessions"]]
                break
            except OSError:
                time.sleep(0.25)
        else:
            sys.exit("make_screenshot: the temporary copy did not start")
        out = ROOT / "docs" / "screenshot.png"
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                        "--user-data-dir=%s" % (tmp / "chrome"), "--window-size=1180,700", "--force-device-scale-factor=2",
                        "--virtual-time-budget=5000", "--screenshot=%s" % out, "http://127.0.0.1:%d/" % PORT],
                       capture_output=True, timeout=120)
        print("docs/screenshot.png retaken at version %s with %d made-up sessions" % (version, len(names)))
    finally:
        if server:
            server.terminate()
        for proc in sleepers:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
