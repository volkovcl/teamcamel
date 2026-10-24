#!/usr/bin/env python3
"""Retakes the pictures in docs/ from made-up sessions, at the current version.

    python3 packaging/make_screenshot.py        (macOS, with Google Chrome installed)

    docs/screenshot.png   the page: three computers, each in its own column
    docs/settings.png     the same page with the settings open

A copy of the program runs in a temporary folder with a pretend home folder, so
neither this computer's sessions nor its state folder take part. Every session,
workspace and computer name in the pictures is invented here.
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
SIZE = "1400,900"
WORK = None            # the made-up work folders, created in the temporary folder
# Version numbers are made up too: one computer up to date, one a little behind, one far behind,
# so that the version labels show all three colors.
CLAUDE = {"studio-mac": "2.1.310", "office-pc": "2.1.299", "travel-laptop": "2.0.79"}
CODEX = {"studio-mac": "0.170.0", "office-pc": "0.168.0", "travel-laptop": "0.160.0"}


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")


def iso(mins_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - mins_ago * 60))


def lines(*records):
    return "\n".join(json.dumps(x, separators=(",", ":")) for x in records) + "\n"


def lstart(pid):
    return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                          env=dict(os.environ, LC_ALL="C", TZ="UTC")).stdout.strip()


def folder(name):
    path = WORK / name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def claude_sessions(home, sleepers):
    now = int(time.time() * 1000)
    store = home / "Library/Application Support/Claude/claude-code-sessions/a/o"
    count = [0]

    def session(title, where, mins_ago, ending="done", live=None, archived=False):
        count[0] += 1
        sid, cli = "local_demo%d" % count[0], "cli-demo%d" % count[0]
        record = {"sessionId": sid, "cliSessionId": cli, "title": title, "originCwd": folder(where),
                  "lastActivityAt": now - mins_ago * MIN, "lastTurnEdgeAt": now - mins_ago * MIN}
        if archived:
            record["isArchived"] = True
        write(store / (sid + ".json"), record)
        if live:
            proc = subprocess.Popen(["sleep", "600"], start_new_session=True)
            sleepers.append(proc)
            time.sleep(0.2)
            entry = {"pid": proc.pid, "sessionId": cli, "hostSessionId": sid, "procStart": lstart(proc.pid),
                     "version": CLAUDE["studio-mac"], "name": title, "cwd": folder(where),
                     "statusUpdatedAt": now - mins_ago * MIN, "status": "waiting" if live[0] == "waiting" else "busy"}
            if live[0] == "waiting":
                entry["waitingFor"] = live[1]
            write(home / ".claude/sessions" / ("%d.json" % proc.pid), entry)
            return
        start = {"type": "user", "timestamp": iso(mins_ago + 3), "message": {"content": "go"}}
        done = {"type": "assistant", "timestamp": iso(mins_ago),
                "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}}
        records = [start, done]
        if ending == "dropped":
            records = [start, {"type": "assistant", "timestamp": iso(mins_ago), "isApiErrorMessage": True, "message": {
                "content": [{"type": "text", "text": "API Error: The socket connection was closed unexpectedly"}]}}]
        elif ending.startswith("card:"):
            # The Claude app's closing card: a recap, and a question with answer buttons.
            card = {"recap": "The fix is ready and tested on two phones.", "ask": ending[5:],
                    "actions": [{"label": "Publish now"}, {"label": "Wait"}]}
            records = [start,
                       {"type": "assistant", "timestamp": iso(mins_ago), "message": {"stop_reason": "tool_use", "content": [
                           {"type": "tool_use", "id": "tu1", "name": "mcp__ccd_turn__wrap_up", "input": card}]}},
                       {"type": "user", "timestamp": iso(mins_ago), "message": {"content": [
                           {"type": "tool_result", "tool_use_id": "tu1",
                            "content": [{"type": "text", "text": "Shown in the next-step box."}]}]}},
                       done]
        write(home / ".claude/projects/demo" / (cli + ".jsonl"), lines(*records))

    write(home / ".claude/nodename", "studio-mac\n")
    session("Draft the new pricing page", "website-redesign", 6, live=("waiting", "permission prompt"))
    session("Fix image loading on phones", "website-redesign", 25, ending="card:Publish the fix to the live site?")
    session("Rewrite the About page", "website-redesign", 190)
    session("Set up the contact form", "website-redesign", 2900)
    session("Try a dark color scheme", "website-redesign", 4000, archived=True)
    session("Summarize the Q3 survey answers", "quarterly-report", 4, live=("busy",))
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
    for i, (name, where, records) in enumerate(threads):
        tid = "0000000%d-aaaa-7bbb-8ccc-00000000000%d" % (i, i)
        path = day / ("rollout-demo-%s.jsonl" % tid)
        meta = {"type": "session_meta", "timestamp": iso(60),
                "payload": {"id": tid, "cwd": folder(where), "cli_version": CODEX["studio-mac"]}}
        write(path, lines(meta, *records))
        db.execute("insert into threads values (?,?,?,?,?,0,?,'user')",
                   (tid, name, name, folder(where), str(path), int(time.time() * 1000) - 40 * MIN))
    db.commit()
    db.close()


def other_computers(state, version):
    now = int(time.time() * 1000)
    handled = {}

    def s(node, i, title, ws, st, detail, mins, attn, app=None, archived=False, mark=False):
        d = {"id": "%s-%d" % (node, i), "title": title, "ws": ws, "state": st, "detail": detail,
             "since": now - mins * MIN, "attention": attn, "live": not archived, "link": None}
        if app:
            d["app"] = app
        if archived:
            d["archived"] = True
        if mark:          # ticked "handled" by the user: grey, no longer counted
            handled["%s/%s" % (node, d["id"])] = d["since"]
        return d

    def computer(node, host, system, ago, sessions):
        write(state / ("%s.json" % node), {
            "schema": 1, "node": node, "host": host, "os": system, "version": version,
            "apps": {"claude": CLAUDE[node], "codex": CODEX[node]}, "generatedAt": now - ago, "sessions": sessions})

    computer("office-pc", "OFFICE-PC", "Windows", 25000, [
        s("office-pc", 1, "Add the export button", "client-portal", "input", "input needed", 11, True),
        s("office-pc", 2, "Render the product demo video", "client-portal", "working", "in background (blender)", 35, False),
        s("office-pc", 3, "Tidy up the login screen", "client-portal", "completed", "", 130, False),
        s("office-pc", 4, "Merge the duplicate contacts", "data-cleanup", "working", "", 9, False, "codex"),
        s("office-pc", 5, "Fix the invoice numbering", "data-cleanup", "interrupted", "connection interrupted", 95, True,
          mark=True),
        s("office-pc", 6, "Convert all dates to one format", "data-cleanup", "completed", "", 1450, False)])
    computer("travel-laptop", "travel-laptop", "Linux", 40000, [
        s("travel-laptop", 1, "Back up the photo library", "photo-archive", "working", "in background (rclone)", 20, False),
        s("travel-laptop", 2, "Plan the conference trip", "travel", "completed", "", 600, False),
        s("travel-laptop", 3, "Notes from the client call", "travel", "completed", "archived", 9000, False, archived=True)])
    write(state / "teamcamel.handled", handled)


def shoot(tmp, out, url):
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                    "--user-data-dir=%s" % (tmp / "chrome"), "--window-size=" + SIZE, "--force-device-scale-factor=2",
                    "--virtual-time-budget=6000", "--screenshot=%s" % out, url],
                   capture_output=True, timeout=120)


def main():
    if not os.path.exists(CHROME):
        sys.exit("make_screenshot: Google Chrome is needed at %s" % CHROME)
    global WORK
    tmp = Path(tempfile.mkdtemp(prefix="teamcamel-shot-"))
    WORK = tmp / "work"
    sleepers, server = [], None
    try:
        app, home = tmp / "app", tmp / "home"
        (app / "web").mkdir(parents=True)
        shutil.copy2(str(ROOT / "teamcamel.py"), str(app / "teamcamel.py"))
        page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        (app / "web" / "index.html").write_text(page, encoding="utf-8")
        version = subprocess.run([sys.executable, str(app / "teamcamel.py"), "--version"],
                                 capture_output=True, text=True).stdout.split()[-1]
        claude_sessions(home, sleepers)
        codex_sessions(home)
        other_computers(app / "state", version)
        # No claude or codex command of this computer's own: the versions shown are the made-up ones.
        env = dict(os.environ, HOME=str(home), PATH="/usr/bin:/bin")
        env.pop("CODEX_HOME", None)
        env.pop("CLAUDE_CONFIG_DIR", None)
        server = subprocess.Popen([sys.executable, str(app / "teamcamel.py"), "--port", str(PORT)], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % PORT, timeout=1) as resp:
                    names = [s["title"] for n in json.load(resp)["nodes"] for s in n["sessions"]]
                if len(names) >= 19:
                    break
            except OSError:
                pass
            time.sleep(0.25)
        else:
            sys.exit("make_screenshot: the temporary copy did not start")
        url = "http://127.0.0.1:%d/" % PORT
        shoot(tmp, ROOT / "docs" / "screenshot.png", url)
        # The settings: in the temporary copy only, the page opens them by itself once drawn.
        opener = "<script>setTimeout(() => document.querySelector('.gear').click(), 1500)</script></body>"
        (app / "web" / "index.html").write_text(page.replace("</body>", opener, 1), encoding="utf-8")
        shutil.rmtree(str(tmp / "chrome"), ignore_errors=True)
        shoot(tmp, ROOT / "docs" / "settings.png", url)
        print("docs/screenshot.png and docs/settings.png retaken at version %s with %d made-up sessions"
              % (version, len(names)))
    finally:
        if server:
            server.terminate()
        for proc in sleepers:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
