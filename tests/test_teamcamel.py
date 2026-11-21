"""Tests for TeamCamel. Run before each release:  python3 tests/test_teamcamel.py

Runs on macOS and Linux. The first part uses this computer's real Claude files; the
rest runs the same code with the platform switched and made-up Claude files in a
temporary folder, backed by real processes of this system. Nothing in the project
or in ~/.claude is changed."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request as urllib_request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import teamcamel as tc

REAL = dict(HOME=tc.HOME, CLAUDE=tc.CLAUDE, REGISTRY=tc.REGISTRY, PROJECTS=tc.PROJECTS, SETTINGS=tc.SETTINGS, CODEX=tc.CODEX,
            SELF=tc.SELF, STATE=tc.STATE, WEB=tc.WEB, MACOS=tc.MACOS, WINDOWS=tc.WINDOWS)
passed = 0


def ok(cond, what):
    global passed
    if not cond:
        raise AssertionError(what)
    passed += 1
    print("  ok  " + what)


def platform(name, home):
    tc.MACOS, tc.WINDOWS = name == "mac", name == "win"
    tc.HOME = home
    tc.CLAUDE = home / ".claude"
    tc.REGISTRY, tc.PROJECTS, tc.SETTINGS = tc.CLAUDE / "sessions", tc.CLAUDE / "projects", tc.CLAUDE / "settings.json"
    tc.CODEX = home / ".codex"
    clear_caches()


def clear_caches():
    for cache in (tc._desktop_cache, tc._transcript_paths, tc._verdict_cache, tc._codex_turns):
        cache.clear()
    tc._codex_threads = (None, [])


def restore():
    for k, v in REAL.items():
        setattr(tc, k, v)
    clear_caches()


def lstart(pid):
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                         env=dict(os.environ, LC_ALL="C", TZ="UTC")).stdout
    return out.strip()   # what Claude Code stores: trimmed, inner padding kept


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")


def transcript(path, last):
    lines = [{"type": "user", "timestamp": "2026-10-10T10:00:00.000Z", "message": {"content": "do it"}}, last]
    write(path, "\n".join(json.dumps(x, separators=(",", ":")) for x in lines) + "\n")


tmp = Path(tempfile.mkdtemp(prefix="tc-test-"))
tc._activity_file = lambda: tmp / "activity.json"      # never the real TeamCamel's own file
sleepers = [subprocess.Popen(["sleep", "120"]) for _ in range(4)]
gone = subprocess.Popen(["sleep", "0"]); gone.wait()
now = tc.now_ms()

try:
    print("this computer, real data (%s)" % tc.os_label())
    mine = {s["id"]: (s["state"], s["ws"], s["title"]) for s in tc.collect()}
    running = tc.monitor_info(tc.PORT)
    if running and running.get("version") != tc.__version__:
        print("  --  the monitor running here is version %s, this code is %s: comparison skipped (%d sessions collected)"
              % (running.get("version"), tc.__version__, len(mine)))
    elif running:
        live = running["nodes"][0]
        theirs = {s["id"]: (s["state"], s["ws"], s["title"]) for s in live["sessions"]}
        same = [i for i in mine if i in theirs and mine[i] == theirs[i]]
        ok(set(mine) == set(theirs) and len(same) >= len(mine) - 1,
           "this code lists the same %d sessions as the monitor running now (%d identical)" % (len(mine), len(same)))
    else:
        print("  --  monitor not running here: comparison with it skipped (%d sessions collected)" % len(mine))
    try:
        ours = tc.our_hooks(tc.read_json(REAL["SETTINGS"]))[1]
    except (OSError, ValueError):
        ours = []
    if any(str(tc.SELF) in h.get("command", "") for h in ours):
        ok(tc.hook_entry() in ours, "the hook this code would install equals the one installed on this computer")
    else:
        print("  --  the installed hook (if any) is for another copy of TeamCamel: comparison skipped")
    if tc.MACOS:
        ok(tc.desktop_roots() == [REAL["HOME"] / "Library/Application Support/Claude"], "macOS desktop app folder")
    token = tc.running_processes([os.getpid()]).get(os.getpid())
    ok(bool(token) and tc.same_process({"pid": os.getpid(), "procStart": lstart(os.getpid())}, {os.getpid(): token}),
       "this system's ps gives a start time, and it matches what Claude Code would record: " + str(token).strip())

    print("start-time comparison")
    ok(tc._same_start("Fri Oct  9 19:02:41 2026", "Fri Oct  9 19:02:41 2026"), "identical text matches")
    ok(tc._same_start("Fri Oct  9 19:02:41 2026", "Fri Oct 9 19:02:41 2026"), "one-digit day: padding ignored")
    ok(tc._same_start("Fri Oct  9 19:02:41 2026", "Fri Oct  9 19:02:42 2026") is False, "different second: not the same")
    ok(tc._same_start("133456789012345678", 133456789012345678), "Windows creation time as text vs number matches")
    ok(tc._same_start(133456789012345678, 133456789012345678 + 50_000_000) is False, "5 s apart: not the same")
    ok(tc._same_start("Fri Oct  9 19:02:41 2026", 133456789012345678) is None, "text vs number: cannot compare")
    ok(tc.same_process({"pid": 7, "procStart": "x"}, {}) is False, "pid not running: not live")
    ok(tc.same_process({"pid": 7, "procStart": "x"}, {7: None}), "running, start unknown: live")
    ok(tc.same_process({"pid": 7, "procStartFt": "133456789012345678", "procStart": "Sat Oct 10 01:00:00 2026"},
                       {7: 133456789012345678}), "Windows entry with both fields: matched on the numeric one")

    print("names and paths from any system")
    ok(tc.ws_name("C:\\Users\\me\\work\\wp-gamma") == "gamma", "Windows folder -> ws name")
    ok(tc.ws_name("C:\\Users\\me\\repo\\.claude\\worktrees\\fix-1") == "repo", "Windows worktree -> its project")
    ok(tc.ws_name("/home/u/work/z_wp-archived") == "z_archived", "set-aside marker kept")
    ok(tc.ws_name("/Users/a/work/wp-office/wp-office-reports") == "office-reports", "nested Mac folder")
    ok(tc.ws_name(None) == "(no folder)", "no folder")
    tc.STATE = Path("/x/state")
    names = {n: tc.state_file(n).name for n in ["laptop", "desk-2", "Sam\u2019s Laptop (2)", "\u041e\u0424\u0418\u0421-\u041f\u041a", 'a/b:c*?"<>|', " .. "]}
    ok(names["laptop"] == "laptop.json" and names["desk-2"] == "desk-2.json", "plain names are kept as they are")
    ok(names["Sam\u2019s Laptop (2)"] == "Sam_s Laptop (2).json", "curly apostrophe made safe")
    ok(names["\u041e\u0424\u0418\u0421-\u041f\u041a"] == "\u041e\u0424\u0418\u0421-\u041f\u041a.json", "Cyrillic computer name kept")
    ok(not set('/\\:*?"<>|') & set(names['a/b:c*?"<>|']) and names[" .. "] == "node.json", "characters Windows forbids removed")
    tc.STATE = REAL["STATE"]

    print("Linux, simulated with made-up Claude files")
    home = tmp / "linux-home"
    platform("linux", home)
    os.environ.pop("XDG_CONFIG_HOME", None); os.environ.pop("XDG_STATE_HOME", None)
    ok(tc.desktop_roots() == [home / ".config" / "Claude"], "Linux desktop app folder")
    ok(tc.log_path() == home / ".local/state/teamcamel/teamcamel.log", "Linux log location")
    ok(tc.os_label() and not tc.os_label().startswith(("macOS", "Windows")), "system label: " + tc.os_label())
    store = home / ".config/Claude/claude-code-sessions/acct/org"
    write(store / "local_A.json", {"sessionId": "local_A", "cliSessionId": "cliA", "title": "Waiting job",
                                   "originCwd": "/home/u/projects/wp-alpha", "lastActivityAt": now - 60000})
    write(store / "local_B.json", {"sessionId": "local_B", "cliSessionId": "cliB", "title": "Finished job",
                                   "cwd": "/home/u/projects/wp-alpha", "lastActivityAt": now - 3600000})
    write(store / "local_C.json", {"sessionId": "local_C", "cliSessionId": "cliC", "title": "Dropped job",
                                   "cwd": "/home/u/projects/beta", "lastActivityAt": now - 600000})
    write(store / "local_D.json", {"sessionId": "local_D", "cliSessionId": "cliD", "title": "Archived",
                                   "cwd": "/home/u/x", "isArchived": True})
    write(store / "local_E.json", {"sessionId": "local_E", "cliSessionId": "cliE", "title": "Pid reused by another program",
                                   "cwd": "/home/u/projects/beta", "lastActivityAt": now - 100000})
    transcript(home / ".claude/projects/p1/cliB.jsonl", {"type": "assistant", "timestamp": "2026-10-10T10:05:00.000Z",
                                                         "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}})
    transcript(home / ".claude/projects/p1/cliC.jsonl", {"type": "assistant", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                                                         "isApiErrorMessage": True, "message": {"content": [{"type": "text", "text": "API Error: The socket connection was closed unexpectedly"}]}})
    transcript(home / ".claude/projects/p1/cliE.jsonl", {"type": "assistant", "timestamp": "2026-10-10T10:05:00.000Z",
                                                         "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}})
    reg = home / ".claude/sessions"
    a, t, e, n = (p.pid for p in sleepers)
    write(reg / ("%d.json" % a), {"pid": a, "sessionId": "cliA", "hostSessionId": "local_A", "procStart": lstart(a),
                                  "status": "waiting", "waitingFor": "permission prompt", "statusUpdatedAt": now - 30000})
    write(reg / ("%d.json" % t), {"pid": t, "sessionId": "term1", "procStart": lstart(t), "name": "Terminal job",
                                  "cwd": "/home/u/code/gamma", "status": "busy", "statusUpdatedAt": now - 5000})
    write(reg / ("%d.json" % e), {"pid": e, "sessionId": "cliE", "hostSessionId": "local_E",
                                  "procStart": "Thu Jan  1 00:00:00 1970", "status": "busy"})
    write(reg / ("%d.json" % n), {"pid": n, "sessionId": "term2", "procStart": 123456, "name": "Unknown start token",
                                  "cwd": "/home/u/code/gamma", "status": "waiting"})
    write(reg / ("%d.json" % gone.pid), {"pid": gone.pid, "sessionId": "term3", "procStart": "Sat Oct 10 10:00:00 2026",
                                         "name": "Ended terminal job", "cwd": "/home/u/code/gamma", "status": "busy"})
    write(reg / "broken.json", "{ half written")
    got = {s["title"]: s for s in tc.collect()}
    ok(set(got) == {"Waiting job", "Finished job", "Dropped job", "Pid reused by another program", "Terminal job", "Unknown start token",
                    "Archived"}, "sessions found: desktop, terminal, archived; ended terminal ones left out")
    ok(got["Archived"].get("archived") and not got["Archived"]["attention"] and got["Archived"]["detail"] == "archived",
       "an archived session is kept, grey")
    ok(got["Waiting job"]["state"] == "input" and got["Waiting job"]["detail"] == "permission prompt" and got["Waiting job"]["attention"]
       and got["Waiting job"]["ws"] == "alpha", "live desktop session awaiting input, ws from its folder")
    ok(got["Terminal job"]["state"] == "working" and got["Terminal job"]["ws"] == "gamma" and got["Terminal job"]["link"] is None,
       "live terminal session working")
    ok(got["Finished job"]["state"] == "completed" and not got["Finished job"]["live"], "dormant session, finished turn")
    ok(got["Dropped job"]["state"] == "interrupted" and got["Dropped job"]["detail"] == "connection interrupted"
       and got["Dropped job"]["attention"], "dormant session ending in a connection error: needs you")
    ok(got["Pid reused by another program"]["state"] == "completed" and not got["Pid reused by another program"]["live"],
       "pid now belongs to another program: not treated as live")
    ok(got["Unknown start token"]["state"] == "input", "start token of an unknown kind: running pid accepted")
    orig_run = subprocess.run
    subprocess.run = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no ps"))
    try:
        alive = {x["sessionId"] for x in tc.live_registry()}
    finally:
        subprocess.run = orig_run
    ok(alive == {"cliA", "term1", "cliE", "term2"}, "without a usable ps: falls back to 'does the pid exist'")
    os.environ["XDG_CONFIG_HOME"] = str(tmp / "xdg")
    ok(tc.desktop_roots() == [tmp / "xdg" / "Claude"], "XDG_CONFIG_HOME respected")
    os.environ.pop("XDG_CONFIG_HOME")
    ok(tc.hook_entry()["command"].endswith('--ensure 2>/dev/null || true') and "shell" not in tc.hook_entry(), "Linux hook form")

    print("Windows, simulated (paths, hook text, duplicate stores)")
    home = tmp / "win-home"
    platform("win", home)
    os.environ["APPDATA"], os.environ["LOCALAPPDATA"] = str(home / "AppData/Roaming"), str(home / "AppData/Local")
    exe_store = home / "AppData/Roaming/Claude"
    msix_store = home / "AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude"
    msix_store.mkdir(parents=True)
    ok(tc.desktop_roots() == [exe_store, msix_store], "Windows desktop app folders: installer and Store (MSIX)")
    ok(tc.log_path() == home / "AppData/Local/TeamCamel/teamcamel.log", "Windows log location")
    ok(tc.os_label() == "Windows", "system label")
    rec = {"sessionId": "local_W", "cliSessionId": "cliW", "title": "Windows job", "cwd": "C:\\Users\\me\\work\\wp-delta",
           "lastActivityAt": now - 1000}
    write(msix_store / "claude-code-sessions/a/o/local_W.json", "\ufeff" + json.dumps(rec))
    write(exe_store / "claude-code-sessions/a/o/local_W.json", rec)
    found = tc.desktop_sessions()
    ok(len(found) == 1 and found[0]["title"] == "Windows job", "same session in both stores listed once; file with a BOM read")
    ok(tc.live_registry() == [], "no registry on this made-up Windows home: no live sessions, no error")
    ok(tc._running_windows([1, 2]) == {}, "Windows process check on a non-Windows system: fails soft")
    real_exe, real_which = sys.executable, shutil.which
    tc.SELF = "C:\\Users\\me\\Dropbox\\Team Camel\\teamcamel.py"
    sys.executable = "C:\\Users\\me\\AppData\\Local\\Programs\\Python\\Python312\\python.exe"
    ok(tc.hook_entry() == {"type": "command", "timeout": 15, "command":
       'C:/Users/me/AppData/Local/Programs/Python/Python312/python.exe "C:/Users/me/Dropbox/Team Camel/teamcamel.py" --ensure'},
       "Windows hook: interpreter path without spaces used directly")
    sys.executable = "C:\\Program Files\\Python312\\python.exe"
    shutil.which = lambda name: "C:\\Windows\\py.exe" if name == "py" else None
    ok(tc.hook_entry()["command"] == 'py "C:/Users/me/Dropbox/Team Camel/teamcamel.py" --ensure' and "shell" not in tc.hook_entry(),
       "Windows hook: interpreter path with a space -> the py launcher")
    shutil.which = lambda name: None
    ok(tc.hook_entry() == {"type": "command", "shell": "powershell", "timeout": 15, "command":
       '& "C:/Program Files/Python312/python.exe" "C:/Users/me/Dropbox/Team Camel/teamcamel.py" --ensure'},
       "Windows hook: no launcher on PATH -> PowerShell form")
    sys.executable, shutil.which = real_exe, real_which

    print("hook installer")
    restore()
    home = tmp / "hook-home"
    platform("mac", home)
    s = tc.SETTINGS

    def run():
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                tc.install_hook()
        except SystemExit as err:
            return "EXIT " + str(err)
        return buf.getvalue()

    orig = {"model": "opus", "permissions": {"allow": ["Bash(git *)"]},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/bin/bash backup.sh"}]}],
                      "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo hi"}]}]}}
    write(s, "\ufeff" + json.dumps(orig, indent=2))
    out = run(); d = json.loads(s.read_text(encoding="utf-8"))
    ok("Hook added" in out and d["model"] == "opus" and d["permissions"] == orig["permissions"] and d["hooks"]["Stop"] == orig["hooks"]["Stop"]
       and d["hooks"]["SessionStart"][0] == orig["hooks"]["SessionStart"][0] and d["hooks"]["SessionStart"][1]["hooks"] == [tc.hook_entry()],
       "added beside existing settings and hooks (settings file with a BOM)")
    ok(len(list((tc.CLAUDE / "backups").glob("settings.json.*.bak"))) == 1, "backup written first")
    before = s.read_text(encoding="utf-8")
    ok("already present" in run() and s.read_text(encoding="utf-8") == before, "second run changes nothing")
    d["hooks"]["SessionStart"][1]["hooks"][0] = {"type": "command", "shell": "powershell", "command": '& "x" "D:/old/teamcamel.py" --ensure'}
    write(s, d); run(); d = json.loads(s.read_text(encoding="utf-8"))
    ok([h for g in d["hooks"]["SessionStart"] for h in g["hooks"] if "teamcamel.py" in h["command"]] == [tc.hook_entry()],
       "moved folder: old hook replaced, not duplicated")
    write(s, "{ not json")
    ok(run().startswith("EXIT") and s.read_text() == "{ not json", "broken settings file: refused, left alone")
    s.unlink()
    ok("Hook added" in run() and json.loads(s.read_text())["hooks"]["SessionStart"][0]["hooks"] == [tc.hook_entry()], "no settings file yet: created")

    ok(tc.hook_entry(2300)["command"].endswith('--ensure --port 2300 2>/dev/null || true')
       and tc.hook_entry()["command"].endswith('--ensure 2>/dev/null || true'), "hook carries a port only when it is not the usual one")

    print("uninstall")
    def un():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tc.uninstall(47312)                      # nothing listens there: only the settings are touched
        return buf.getvalue()

    other = {"type": "command", "command": '/usr/bin/python3 "/somewhere/else/teamcamel.py" --ensure 2>/dev/null || true'}
    keep = {"type": "command", "command": "echo hi"}
    write(s, {"model": "opus", "hooks": {"Stop": [{"hooks": [keep]}],
                                         "SessionStart": [{"hooks": [keep, tc.hook_entry()]}, {"hooks": [tc.hook_entry(2300)]},
                                                          {"hooks": [other]}]}})
    n_backups = len(list((tc.CLAUDE / "backups").glob("settings.json.*.bak")))
    time.sleep(1.1)                                   # backups are named to the second
    out = un(); d = json.loads(s.read_text(encoding="utf-8"))
    ok("Start hook removed" in out and d["hooks"]["SessionStart"] == [{"hooks": [keep]}, {"hooks": [other]}]
       and d["hooks"]["Stop"] == [{"hooks": [keep]}] and d["model"] == "opus",
       "removes this copy's hooks only; other hooks and another TeamCamel copy stay")
    ok(len(list((tc.CLAUDE / "backups").glob("settings.json.*.bak"))) == n_backups + 1, "settings backed up before the change")
    before = s.read_text(encoding="utf-8")
    ok("No start hook for this copy" in un() and s.read_text(encoding="utf-8") == before, "second run changes nothing")
    write(s, {"hooks": {"SessionStart": [{"hooks": [tc.hook_entry()]}]}})
    un()
    ok(json.loads(s.read_text(encoding="utf-8")) == {}, "nothing but the TeamCamel hook: the hooks section is removed entirely")
    write(s, "{ not json")
    try:
        un(); refused = False
    except SystemExit:
        refused = True
    ok(refused and s.read_text() == "{ not json", "broken settings file: refused, left alone")
    s.unlink()
    ok("No start hook for this copy" in un() and not s.exists(), "no settings file: nothing created")

    print("precheck")
    def pre(port=tc.PORT):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            count = tc.precheck(port)
        return count, buf.getvalue()

    platform("linux", tmp / "nobody-home")          # a user who has never run Claude Code
    count, out = pre(47311)
    ok(count == 1 and "[MISSING] Claude Code" in out and "-> install the Claude desktop app" in out
       and "Result: 1 item missing" in out, "no Claude Code: reported as missing, with what to do")
    ok(not (tmp / "nobody-home").exists(), "precheck created nothing")
    platform("linux", tmp / "linux-home")           # the made-up Linux home from above, with live sessions
    count, out = pre(47311)
    ok(count == 0 and "live session status" in out and "[MISSING]" not in out and "To install:" in out,
       "Claude Code with live sessions: nothing missing, next step given")
    shutil.rmtree(tmp / "linux-home" / ".claude" / "sessions")
    count, out = pre(47311)
    ok(count == 1 and "[MISSING] live session status" in out and "update Claude Code" in out, "Claude Code too old for live status: reported")
    write(tmp / "linux-home" / ".claude" / "settings.json", "{ damaged")
    (tmp / "linux-home" / ".claude" / "sessions").mkdir()
    count, out = pre(47311)
    ok(count == 1 and "[MISSING] a readable Claude settings file" in out and "no Claude session is running right now" in out,
       "damaged settings file reported; no running session is only a note")
    (tmp / "linux-home" / ".claude" / "settings.json").unlink()
    blocker = tc.socket.socket(); blocker.bind(("127.0.0.1", 0)); blocker.listen(1)
    count, out = pre(blocker.getsockname()[1])
    blocker.close()
    ok(count == 1 and "another program is using it" in out and "--install-hook --port" in out, "page port taken by another program: reported")
    real_web = tc.WEB
    tc.WEB = tmp / "no-web"
    count, out = pre(47311)
    tc.WEB = real_web
    ok(count == 1 and "[MISSING] the page file web/index.html" in out, "incomplete download (no page file): reported")
    restore()
    count, out = pre()
    ok("[MISSING]" not in out or count > 0, "precheck on this computer ran: %d missing" % count)

    print("Codex sessions, made-up files")
    restore()
    import sqlite3
    cx = tmp / "codex-home"
    real_codex, real_running = tc.CODEX, tc.codex_running
    tc.CODEX = cx
    tc._codex_threads = (None, []); tc._codex_turns.clear()
    day = cx / "sessions" / "2026" / "10" / "11"
    day.mkdir(parents=True)

    def iso(mins_ago):
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - mins_ago * 60))

    # The layout Codex writes: timestamp first, and the payload's type first inside the payload.
    def ev(kind, mins_ago, **extra):
        return {"timestamp": iso(mins_ago), "ordinal": 1, "type": "event_msg", "payload": dict({"type": kind}, **extra)}

    def call(cid, mins_ago, text):
        return {"timestamp": iso(mins_ago), "ordinal": 1, "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "exec", "call_id": cid, "input": text}}

    def result(cid, mins_ago):
        return {"timestamp": iso(mins_ago), "ordinal": 1, "type": "response_item",
                "payload": {"type": "custom_tool_call_output", "call_id": cid, "output": "x" * 300000}}

    plain = 'text(await tools.exec_command({cmd:"ls"}))'
    threads = {
        "done": [ev("task_started", 30), call("a", 29, plain), result("a", 29), ev("task_complete", 28)],
        "approve": [ev("task_started", 9), call("a", 8, plain), result("a", 8),
                    call("b", 7, 'text(await tools.exec_command({cmd:"/usr/bin/x","sandbox_permissions":"require_escalated"}))')],
        "busy": [ev("task_started", 3), call("a", 2, plain)],
        "stopped": [ev("task_started", 20), call("a", 19, plain), ev("turn_aborted", 18, reason="interrupted")],
        "asking": [ev("task_started", 6), call("a", 5, 'text(await tools.request_user_input({questions:[]}))')],
        "second-turn": [ev("task_started", 50), ev("task_complete", 49), ev("task_started", 4), call("a", 3, plain), result("a", 3), ev("task_complete", 2)],
        "orphan": [ev("task_started", 40), call("a", 39, plain), result("a", 39)],
        "fresh": [],
        # another layout (keys in a different order), read the slow way
        "other-layout": [{"type": "event_msg", "payload": {"turn_id": "t", "type": "task_started"}, "timestamp": iso(7)},
                         {"type": "response_item", "payload": {"call_id": "z", "name": "exec", "type": "custom_tool_call",
                                                               "input": 'run({"sandbox_permissions":"require_escalated"})'}, "timestamp": iso(6)}],
        # a turn so long that its beginning lies before the part of the file that is read
        "long-turn": [ev("task_started", 25)] + [x for i in range(4) for x in (call("c%d" % i, 20 - i, plain), result("c%d" % i, 20 - i))]
                     + [call("last", 2, 'text(await tools.exec_command({cmd:"x","sandbox_permissions":"require_escalated"}))')],
        "archived": [ev("task_started", 5), call("a", 4, plain)],
        "helper": [ev("task_started", 5), call("a", 4, plain)],
    }
    db = sqlite3.connect(str(cx / "state_5.sqlite"))
    db.execute("create table threads (id text, name text, title text, cwd text, rollout_path text, archived integer, updated_at_ms integer, thread_source text, originator text)")
    index = []
    for i, (name, records) in enumerate(threads.items()):
        tid = "0000000%d-aaaa-7bbb-8ccc-00000000000%d" % (i, i)
        path = day / ("rollout-2026-10-11T10-00-0%d-%s.jsonl" % (i, tid))
        made_by = "codex_cli_rs" if name == "busy" else "Codex Desktop"
        meta = {"type": "session_meta", "timestamp": iso(60), "payload": {"id": tid, "cwd": "/Users/sam/work/wp-" + name, "originator": made_by}}
        write(path, "\n".join(json.dumps(r, separators=(",", ":")) for r in [meta] + records) + "\n")
        db.execute("insert into threads values (?,?,?,?,?,?,?,?,?)", (tid, "Thread " + name, "first message of " + name, "/Users/sam/work/wp-" + name,
                                                                     str(path), 1 if name == "archived" else 0, now - 60000 * 45,
                                                                     "subagent" if name == "helper" else "user", made_by))
        index.append(json.dumps({"id": tid, "thread_name": "Thread " + name}))
        if name == "orphan":
            old_time = time.time() - 20 * 60
            os.utime(path, (old_time, old_time))
    db.commit(); db.close()
    write(cx / "session_index.jsonl", "\n".join(index) + "\n")

    tc.codex_running = lambda: True
    got = {s["title"]: s for s in tc.codex_sessions()}
    ok(set(got) == {"Thread " + n for n in threads if n != "helper"}, "threads listed, archived ones too; helper threads left out")
    ok(got["Thread archived"].get("archived") and not got["Thread archived"]["attention"]
       and got["Thread archived"]["detail"] == "archived", "an archived Codex thread is kept, grey")
    ok(all(s["app"] == "codex" and s["id"].startswith("codex-") for s in got.values()), "each marked as a Codex session")
    ok(got["Thread done"]["link"] == "codex://threads/00000000-aaaa-7bbb-8ccc-000000000000" and got["Thread busy"]["link"] is None,
       "a thread from the Codex desktop app gets a link that opens it there; one from the terminal command gets none")
    ok(got["Thread done"]["state"] == "completed" and got["Thread done"]["ws"] == "done", "finished turn: completed; ws from its folder")
    ok(got["Thread approve"]["state"] == "input" and got["Thread approve"]["detail"] == "approval" and got["Thread approve"]["attention"],
       "command asking for wider permissions, no result yet: awaits input (approval)")
    ok(got["Thread asking"]["state"] == "input" and got["Thread asking"]["detail"] == "input needed", "Codex asked a question: awaits input")
    ok(got["Thread busy"]["state"] == "working" and not got["Thread busy"]["attention"], "ordinary command still running: working")
    ok(got["Thread stopped"]["state"] == "interrupted" and "stopped by you" in got["Thread stopped"]["detail"], "turn stopped by the user: interrupted")
    ok(got["Thread second-turn"]["state"] == "completed" and now - got["Thread second-turn"]["since"] < 5 * 60000, "only the last turn counts")
    ok(got["Thread fresh"]["state"] == "completed", "thread with no turn yet: nothing to do")
    ok(got["Thread other-layout"]["state"] == "input" and got["Thread other-layout"]["detail"] == "approval",
       "transcript lines in another layout are still understood")
    ok(got["Thread long-turn"]["state"] == "input" and os.path.getsize([str(day / f) for f in os.listdir(day) if "-00000000000" in f and f.endswith("9.jsonl")][0]) > 1000000,
       "a turn longer than the part of the file that is read: its end still tells the state")
    ok(got["Thread orphan"]["state"] == "interrupted" and got["Thread orphan"]["detail"] == "interrupted mid-task",
       "open turn with no activity for a long time: interrupted")
    real_seconds, real_days = tc.CODEX_READ_SECONDS, tc.CODEX_READ_DAYS
    tc._codex_turns.clear()
    tc.CODEX_READ_SECONDS = 0
    started = time.time()
    unread = tc.codex_sessions()
    ok(all(s["state"] == "completed" for s in unread) and len(unread) == len(got) and time.time() - started < 1,
       "no time allowance: every thread is listed at once, as completed, without reading a transcript")
    tc.CODEX_READ_SECONDS = real_seconds
    ok({s["title"]: s for s in tc.codex_sessions()}["Thread approve"]["state"] == "input", "next cycle, with time: the real state")
    tc._codex_turns.clear()
    tc.CODEX_READ_DAYS = 0
    ok(all(s["state"] == "completed" for s in tc.codex_sessions()) and not tc._codex_turns,
       "threads idle longer than the limit: listed as completed, transcript not read")
    tc.CODEX_READ_DAYS = real_days

    tc.codex_running = lambda: False
    tc._codex_turns.clear()
    got = {s["title"]: s for s in tc.codex_sessions()}
    ok(got["Thread busy"]["state"] == "working" and got["Thread approve"]["state"] == "input",
       "Codex not seen running, but the turn was active moments ago: still trusted")
    os.utime(str(day / [f for f in os.listdir(day) if f.endswith("-000000000002.jsonl")][0]), (time.time() - 900, time.time() - 900))
    tc._codex_turns.clear()
    ok({s["title"]: s for s in tc.codex_sessions()}["Thread busy"]["state"] == "interrupted", "Codex not running and the turn long idle: interrupted")
    tc.codex_running = lambda: True
    (cx / "state_5.sqlite").unlink()
    tc._codex_threads = (None, []); tc._codex_turns.clear()
    got = {s["title"]: s for s in tc.codex_sessions()}
    ok(len(got) == len(threads) and got["Thread approve"]["state"] == "input" and got["Thread approve"]["ws"] == "approve",
       "without the database: threads read from the transcripts and the name index")
    ok(got["Thread done"]["link"] and got["Thread busy"]["link"] is None, "without the database: the link still follows where the thread was made")
    write(cx / "state_9.sqlite", "not a database")
    tc._codex_threads = (None, [])
    ok(len(tc.codex_sessions()) == len(threads), "unreadable database: falls back to the files")
    tc.CODEX = tmp / "no-codex"
    tc._codex_threads = (None, [])
    ok(tc.codex_sessions() == [], "no Codex on the computer: no Codex sessions, no error")
    tc.CODEX, tc.codex_running = real_codex, real_running
    tc._codex_threads = (None, []); tc._codex_turns.clear()

    print("work behind a finished turn")
    restore()
    home = tmp / "bg-home"
    platform("mac" if REAL["MACOS"] else "linux", home)
    ok(tc.work_detail(["blender"]) == "in background (blender)", "detail names the busy program")
    ok(tc.work_detail(["agent", "command", "agent"]) == "in background (2 agents, command)", "detail counts agents and commands")
    ok(tc.work_detail(["Blender", "command"]) == "in background (Blender)", "a busy program stands for the command that started it")

    def iso(ms):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000)) + ".%03dZ" % (ms % 1000)

    def result(ms, res, text="ok"):
        return {"type": "user", "timestamp": iso(ms), "toolUseResult": res,
                "message": {"content": [{"type": "tool_result", "content": text}]}}

    def notice(ms, tid, status):
        return {"type": "queue-operation", "operation": "enqueue", "timestamp": iso(ms),
                "content": "<task-notification>\n<task-id>%s</task-id>\n<status>%s</status>\n</task-notification>" % (tid, status)}

    started = now - 600000                      # the session's running process started 10 min ago
    lines = [
        result(now - 7200000, {"backgroundTaskId": "old"}),                          # earlier run of the session
        result(now - 300000, {"backgroundTaskId": "cmdX", "stdout": ""}),
        result(now - 240000, {"status": "async_launched", "agentId": "agentY", "isAsync": True}),
        result(now - 180000, {"status": "async_launched", "agentId": "agentZ", "isAsync": True}),
        notice(now - 170000, "agentZ", "completed"),
        result(now - 120000, {"backgroundTaskId": "cmdW"}),
        result(now - 110000, {"task_id": "cmdW", "message": "Successfully stopped task: cmdW (sleep 99)"}),
        result(now - 100000, {"stdout": '"backgroundTaskId": "fake" <task-notification><task-id>agentY</task-id><status>completed</status>'},
               '<task-notification><task-id>agentY</task-id><status>completed</status>'),       # a command's output, not a record
        notice(now - 90000, "cmdX", "blocked"),
    ]
    tpath = home / ".claude/projects/p1/cliF.jsonl"
    write(tpath, "\n".join(json.dumps(x, separators=(",", ":")) for x in lines) + "\n")
    got = sorted(tc.open_tasks("cliF", started))
    ok(got == [("agent", now - 240000), ("command", now - 300000)],
       "transcript: open background command and agent found; ended, stopped and earlier-run ones left out")
    with open(tpath, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(notice(now - 60000, "cmdX", "completed"), separators=(",", ":")) + "\n")
        fh.write('{"type":"user","timestamp":"' + iso(now))           # a line still being written
    ok(tc.open_tasks("cliF", started) == [("agent", now - 240000)], "transcript read on from where it stopped; half line left for later")
    ok(tc.open_tasks("cliF", now) == [], "work started before the running process is not counted")

    # Sessions: F idle with an open agent; G idle with only an idle leftover program; H closed, its
    # detached program busy; I awaiting input with a busy program; J idle with an open command, no shell left.
    store = home / ("Library/Application Support/Claude" if REAL["MACOS"] else ".config/Claude") / "claude-code-sessions/acct/org"
    done = {"type": "assistant", "timestamp": iso(now - 200000), "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}}
    for key, title in (("F", "Job with a helper agent"), ("G", "Job with an idle leftover"), ("H", "Closed job still rendering"),
                       ("I", "Waiting job still rendering"), ("J", "Job whose command ended unnoticed")):
        write(store / ("local_%s.json" % key), {"sessionId": "local_" + key, "cliSessionId": "cli" + key, "title": title,
                                                "cwd": "/u/wp-render", "lastActivityAt": now - 200000})
        if key != "F":
            transcript(home / ".claude/projects/p1" / ("cli%s.jsonl" % key), done)
    with open(tpath, "a", encoding="utf-8") as fh:
        fh.write("\n" + json.dumps(done, separators=(",", ":")) + "\n")
    write(home / ".claude/projects/p1/cliJ.jsonl", json.dumps(result(now - 60000, {"backgroundTaskId": "cmdJ"}), separators=(",", ":"))
          + "\n" + json.dumps(done, separators=(",", ":")) + "\n")
    reg = home / ".claude/sessions"
    f_pid, g_pid, i_pid, j_pid = (p.pid for p in sleepers)
    for pid, key, status in ((f_pid, "F", "idle"), (g_pid, "G", "idle"), (i_pid, "I", "waiting"), (j_pid, "J", "idle")):
        write(reg / ("%d.json" % pid), {"pid": pid, "sessionId": "cli" + key, "hostSessionId": "local_" + key, "procStart": lstart(pid),
                                        "startedAt": started, "status": status, "waitingFor": "permission prompt"})
    busy = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    busy2 = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    idle = subprocess.Popen(["sleep", "120"])
    table = [
        (busy.pid, 1, "python3 render.py CLAUDE_PID=99999 CLAUDE_CODE_SESSION_ID=cliH"),            # detached, its session closed
        (busy2.pid, 1, "python3 render.py CLAUDE_PID=%d CLAUDE_CODE_SESSION_ID=cliI" % i_pid),
        (idle.pid, 1, "helperd CLAUDE_PID=%d CLAUDE_CODE_SESSION_ID=cliG" % g_pid),                 # idle leftover
        (os.getpid(), 1, "python3 teamcamel.py CLAUDE_PID=%d CLAUDE_CODE_SESSION_ID=cliG" % g_pid),  # TeamCamel itself
    ]
    real_table = tc.process_table
    tc.process_table = lambda: table
    try:
        deadline = time.time() + 15
        while time.time() < deadline and sum(p["cpu"] >= tc.BUSY_CPU for p in tc.marked_processes(table)) < 2:
            time.sleep(0.5)
        procs = {p["pid"]: p for p in tc.marked_processes(table)}
        ok(set(procs) == {busy.pid, busy2.pid, idle.pid} and procs[busy.pid]["cpu"] >= tc.BUSY_CPU and procs[idle.pid]["cpu"] < tc.BUSY_CPU,
           "processes carrying a session's marks found, busy told from idle; TeamCamel itself left out")
        # A session's command quit the Claude app and opened it again: the app and all it starts carry
        # that session's mark. Only a command of a Claude Code session running inside it counts.
        app, helper, panel, launcher, cli, command = extra = [subprocess.Popen(["sleep", "120"]) for _ in range(6)]
        try:
            stale = "CLAUDE_PID=%d CLAUDE_CODE_SESSION_ID=cliK" % gone.pid
            base = "/Applications/Claude.app/Contents/"
            reopened = [
                (app.pid, 1, base + "MacOS/Claude " + stale),
                (helper.pid, app.pid, base + "Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper --type=gpu " + stale),
                (panel.pid, helper.pid, "/bin/zsh -l " + stale),                       # its Terminal panel
                (launcher.pid, app.pid, base + "Helpers/disclaimer --pgroup -- claude " + stale),
                (cli.pid, launcher.pid, "/Users/u/Library/Application Support/Claude/claude-code/2.1.308/x/claude.app/"
                                        "Contents/MacOS/claude --output-format stream-json " + stale),
                (command.pid, cli.pid, "python3 build.py CLAUDE_PID=%d CLAUDE_CODE_SESSION_ID=cliL" % cli.pid),
                (busy.pid, 1, "python3 render.py CLAUDE_PID=99999 CLAUDE_CODE_SESSION_ID=cliH")]
            got = {p["pid"]: p for p in tc.marked_processes(reopened)}
            ok(not {app.pid, helper.pid, panel.pid, launcher.pid, cli.pid} & set(got),
               "the Claude app reopened by a session's command, its helpers and what they start: not that session's work")
            ok(command.pid in got and got[command.pid]["session"] == "cliL" and busy.pid in got,
               "a command of a Claude Code session running in that app, and a detached program: still counted")
        finally:
            for x in extra:
                x.kill()
        got = {s["title"]: s for s in tc.collect()}
        f, g, h, i, j = (got[t] for t in ("Job with a helper agent", "Job with an idle leftover", "Closed job still rendering",
                                          "Waiting job still rendering", "Job whose command ended unnoticed"))
        ok(f["state"] == "working" and f["detail"] == "in background (agent)" and f["since"] == now - 240000 and not f["attention"],
           "finished turn, helper agent still running: working in background")
        ok(g["state"] == "completed", "an idle leftover program does not count")
        ok(h["state"] == "working" and h["detail"].startswith("in background (") and not h["live"],
           "session closed, the program it started still busy: working in background")
        ok(i["state"] == "input", "awaiting input stays awaiting input")
        ok(j["state"] == "completed", "a background command with no shell left is over, notice or not")
        table.append((424242, j_pid, "/bin/zsh -c source /u/.claude/shell-snapshots/snapshot-zsh-1.sh && sleep 300"))
        ok({s["title"]: s for s in tc.collect()}["Job whose command ended unnoticed"]["detail"] == "in background (command)",
           "a background command whose shell still runs: working in background")
        tc.process_table = lambda: None
        ok({s["title"]: s for s in tc.collect()}["Job whose command ended unnoticed"]["state"] == "working",
           "where processes cannot be read (Windows): the transcript alone decides")
        busy.kill(); busy2.kill(); busy.wait(); busy2.wait()
        tc.process_table = lambda: table
        got = {s["title"]: s for s in tc.collect()}
        ok(got["Closed job still rendering"]["state"] == "completed", "the program ended: completed again")
    finally:
        tc.process_table = real_table
        for p in (busy, busy2, idle):
            p.kill()
    rows = tc.process_table()
    ok(rows is not None and any(pid == os.getpid() for pid, _, _ in rows), "process table of this computer read")

    print("copying and uploading count as working")
    clock = [1000.0]
    real_time, real_moved = tc.time.time, tc.bytes_moved
    tc.time.time = lambda: clock[0]
    moved = {}
    tc.bytes_moved = lambda pids: {pid: moved[pid] for pid in pids if pid in moved}
    tc._activity.clear()
    try:
        prog = lambda pid, cpu=0.0, since=1: {"pid": pid, "cpu": cpu, "since": since, "program": "rclone", "claude": 1, "session": "s"}
        moved.update({11: 10_000_000, 12: 10_000_000})
        ok(tc.busy_processes([prog(11), prog(12)]) == [], "first look: nothing to compare with yet")
        clock[0] += 15; moved[11] += 2_000_000; moved[12] += 20_000
        ok([p["pid"] for p in tc.busy_processes([prog(11), prog(12)])] == [11],
           "a program moving 2 MB in 15 s counts as working, one trickling 20 KB does not")
        clock[0] += 15
        ok([p["pid"] for p in tc.busy_processes([prog(11), prog(12)])] == [11], "a short pause: still working")
        clock[0] += tc.BUSY_GRACE
        ok(tc.busy_processes([prog(11), prog(12)]) == [], "quiet for two minutes: no longer counted")
        clock[0] += 15; moved[11] += 2_000_000
        ok(tc.busy_processes([prog(11, since=2)]) == [], "the same process number, another program: starts afresh")
        ok([p["pid"] for p in tc.busy_processes([prog(13, cpu=55.0)])] == [13], "busy on the processor: working at once")
    finally:
        tc.time.time, tc.bytes_moved = real_time, real_moved
        tc._activity.clear()
    writer = subprocess.Popen([sys.executable, "-c", "import os, time\n"
                               "f = open(%r, 'wb')\n"
                               "while True:\n    f.write(b'\\0' * 2097152); f.flush(); os.fsync(f.fileno()); time.sleep(0.3)" % str(tmp / "writer.bin")])
    try:
        time.sleep(1.5)
        a1 = tc.bytes_moved([writer.pid]).get(writer.pid)
        time.sleep(3)
        a2 = tc.bytes_moved([writer.pid]).get(writer.pid)
        ok(a1 is not None and a2 is not None and a2 - a1 > 2_000_000,
           "a real program writing to disk with little processor use: its data moved is seen (%.1f MB in 3 s)"
           % ((a2 - a1) / 1e6 if a1 is not None and a2 is not None else -1))
    finally:
        writer.kill()

    print("programs a session set running, without its mark")
    restore()
    ok(tc.paths_in('cd "/Volumes/T7/Old PC Drive" && nohup /bin/bash /Volumes/T7/_rclone/run_uploads.sh t7 >> ~/up.log &')
       >= {"/Volumes/T7/Old PC Drive", "/Volumes/T7/_rclone/run_uploads.sh", str(tc.HOME) + "/up.log"}
       and not any(p.startswith("/bin/") for p in tc.paths_in("/bin/bash /usr/bin/rsync")),
       "the user's own paths read from a command, quoted or not, with ~; system tools left out")
    restore()
    home = tmp / "named-home"
    platform("mac" if REAL["MACOS"] else "linux", home)
    script = tmp / "uploader.py"
    script.write_text("import subprocess, time\nsubprocess.Popen(['sleep', '60'])\ntime.sleep(60)\n")
    other = tmp / "unrelated.py"
    other.write_text("import time\ntime.sleep(60)\n")
    tool = tmp / "mytool"                         # a program file, like uv or rclone
    tool.symlink_to(os.path.realpath(sys.executable))   # (a copy of a system program would not run on macOS)
    launcher = tmp / "Claude.app/Contents/Helpers/disclaimer"     # what the Claude app starts programs with
    panel = tmp / "Claude.app/Contents/Frameworks/Claude Helper.app/panel.py"   # its Terminal panel
    for f in (launcher, panel):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("import subprocess, sys, time\nsubprocess.Popen([sys.executable, %r])\ntime.sleep(60)\n" % str(script))
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}        # no session's mark

    def detached(*args):
        """Start a program the way a resumed upload runs: on its own, not under any Claude (parent 1)."""
        out = subprocess.run(["/bin/sh", "-c", 'nohup "$@" >/dev/null 2>&1 & echo $!', "sh"] + [str(a) for a in args],
                             env=clean, capture_output=True, text=True).stdout
        return int(out.strip())

    started = []
    try:
        up = detached(sys.executable, script)
        un = detached(sys.executable, other)
        tl = detached(tool, "-c", "import time; time.sleep(61)")
        started += [up, un, tl]
        time.sleep(1.5)
        lines = [{"type": "assistant", "timestamp": iso(now - 3600000), "message": {"content": [
                     {"type": "tool_use", "name": "Write", "input": {"file_path": str(script), "content": "..."}}]}},
                 {"type": "assistant", "timestamp": iso(now - 600000), "message": {"content": [
                     {"type": "tool_use", "name": "Bash", "input": {"command": "nohup python3 '%s' &" % script}}]}},
                 {"type": "assistant", "timestamp": iso(now - 500000), "message": {"content": [
                     {"type": "tool_use", "name": "Bash", "input": {"command": "%s 1" % tool}}]}}]
        write(home / ".claude/projects/p1/cliN-old.jsonl", "\n".join(json.dumps(x) for x in lines) + "\n")
        desk = [{"sessionId": "local_N", "cliSessionId": "cliN", "priorCliSessionIds": ["cliN-old"],
                 "title": "Uploads", "cwd": "/u/wp-up", "lastActivityAt": now - 600000}]
        got = {p["pid"]: p for p in tc.named_programs([], desk)}
        ok(up in got and got[up]["session"] == "cliN" and got[up]["program"] == "uploader.py",
           "a program started without the session's mark, running a script the session named: the session's, under the script's name")
        kids = [p for p in got.values() if p["program"] == "sleep"]
        ok(kids and all(p["session"] == "cliN" for p in kids), "what that program started belongs to the same session")
        ok(un not in got, "a program running a file no session named: nobody's")
        ok(any(pid == tl for pid, _, _ in tc.command_lines()) and tl not in got,
           "a program file the session once ran by its path (like uv): not the session's every later run of it")
        ok(not any(p["pid"] == os.getpid() for p in got.values()), "TeamCamel itself: never")
        ok(up not in {p["pid"] for p in tc.named_programs([{"pid": up, "sessionId": "cliZ"}], desk)},
           "under a running Claude Code: left to the session's mark, not counted again")
        by_app = detached(sys.executable, launcher)
        in_panel = detached(sys.executable, panel)
        started += [by_app, in_panel]
        time.sleep(1.5)
        rows = tc.command_lines()
        kids_of = lambda pid: [c for c, pp, _ in rows if pp == pid]
        got = {p["pid"]: p for p in tc.named_programs([], desk)}
        ok(kids_of(by_app) and not any(k in got for k in kids_of(by_app)),
           "the script started by the Claude app's launcher (as its connectors are): not counted")
        ok(kids_of(in_panel) and all(k in got for k in kids_of(in_panel)),
           "the script started again in the Claude app's Terminal panel: counted")
        old = [dict(desk[0], lastActivityAt=now - 20 * 86400 * 1000)]
        tc._named_cache.clear()
        write(home / ".claude/projects/p1/cliN-old.jsonl", json.dumps(dict(lines[1], timestamp=iso(now - 20 * 86400 * 1000))) + "\n")
        ok(up not in {p["pid"] for p in tc.named_programs([], old)}, "named more than 14 days ago: no longer tied")
    finally:
        for pid in started:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        subprocess.run(["pkill", "-f", str(script)], capture_output=True)
        subprocess.run(["pkill", "-f", "^sleep 60$"], capture_output=True)
        tc._named_cache.clear()

    print("programs a session left running: names, may be stuck, stopping")
    restore()
    home = tmp / "stuck-home"
    platform("mac" if REAL["MACOS"] else "linux", home)
    tc.STATE = tmp / "stuck-state"; tc.STATE.mkdir()
    ok(tc.program_name("Python", "/Library/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python "
                                 "-I tools/qa3_billscan.py tools tools/qa3_billscan_result.txt") == "qa3_billscan.py"
       and tc.program_name("bash", "/bin/bash /Volumes/T7/_rclone/run_uploads.sh t7") == "run_uploads.sh"
       and tc.program_name("python3", "python3 -m http.server 8000") == "http.server"
       and tc.program_name("node", "node /u/app/server.js --port 3") == "server.js"
       and tc.program_name("blender", "blender -b scene.blend -a") == "blender"
       and tc.program_name("Python", "python3 -c while True: pass") == "Python",
       "a program running a script is named by the script; other programs keep their own name")
    ok(tc.short_command(str(tc.HOME) + "/x/run.py  --all") == "~/x/run.py --all" and len(tc.short_command("a " * 400)) == 240,
       "a command line shown with the home folder as ~, cut to a readable length")
    # The judgment, step by step: what each look sees is set here.
    real_moved, real_after = tc.bytes_moved, tc.STUCK_AFTER
    tc.STUCK_AFTER = 1
    seen = {}
    tc.bytes_moved = lambda pids: {p: seen[p] for p in pids if p in seen}
    try:
        spin = {"pid": 900001, "since": now - 7200000, "cpu": 99.0, "program": "spin.py"}
        work = {"pid": 900002, "since": now - 7200000, "cpu": 99.0, "program": "render.py"}
        blind = {"pid": 900003, "since": now - 7200000, "cpu": 99.0, "program": "other"}
        seen.update({900001: 5000, 900002: 5000})
        tc.busy_processes([spin, work, blind])
        time.sleep(1.2)
        seen[900002] = 9000                                  # the render wrote something since
        tc.busy_processes([spin, work, blind])
        ok(tc.may_be_stuck(spin) and not tc.may_be_stuck(work),
           "nonstop on the processor with nothing read, written or sent: may be stuck; one that writes: working")
        ok(not tc.may_be_stuck(blind), "a program whose data cannot be read is never judged stuck")
        tc.busy_processes([dict(spin, cpu=3.0)])
        ok(not tc.may_be_stuck(dict(spin, cpu=3.0)), "one that lets go of the processor is not spinning: not stuck")
        tc._activity.clear(); tc._activity_seed = None
        (tmp / "activity.json").write_text(json.dumps({"900001:%d" % spin["since"]: time.time() - 7200}))
        tc.busy_processes([spin])
        ok(tc.quiet_seconds(spin) >= 7000, "the quiet time is kept over a restart of TeamCamel, in a local file")
    finally:
        tc.bytes_moved, tc.STUCK_AFTER = real_moved, real_after
        tc._activity.clear(); tc._activity_seed = None

    # The real thing: programs a session started that run on their own (as a resumed command does).
    marks = dict({k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")},
                 CLAUDE_PID=str(gone.pid), CLAUDE_CODE_SESSION_ID="cliS")

    def detached(*args, env=marks):
        out = subprocess.run(["/bin/sh", "-c", 'nohup "$@" >/dev/null 2>&1 & echo $!', "sh"] + [str(a) for a in args],
                             env=env, capture_output=True, text=True).stdout
        return int(out.strip())

    def running(pid):
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(stat) and not stat.startswith("Z")

    (tmp / "spin.py").write_text("while True:\n    pass\n")
    (tmp / "writer.py").write_text("import os, time\nf = open(%r, 'wb')\nwhile True:\n    t = time.time()\n"
                                   "    while time.time() - t < 0.2:\n        pass\n    f.write(b'x' * 65536); f.flush(); "
                                   "os.fsync(f.fileno())\n" % str(tmp / "written.bin"))
    (tmp / "stubborn.py").write_text("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(300)\n")
    (tmp / "parent.py").write_text("import subprocess, time\nsubprocess.Popen(['sleep', '301'])\ntime.sleep(300)\n")
    started = []
    real_after, real_wait = tc.STUCK_AFTER, tc.STOP_WAIT
    tc.STUCK_AFTER, tc.STOP_WAIT = 3, 2
    try:
        sp, wr = detached(sys.executable, tmp / "spin.py"), detached(sys.executable, tmp / "writer.py")
        started += [sp, wr]
        deadline = time.time() + 20
        work = {}
        while time.time() < deadline:
            work = tc.background_work([], [])
            if work.get("cliS", {}).get("stuck"):
                break
            time.sleep(1)
        got = work.get("cliS") or {}
        names = {g["name"]: g for g in got.get("programs", [])}
        ok(got.get("stuck") == ["spin.py"] and set(names) == {"spin.py", "writer.py"},
           "a program spinning silently: may be stuck; one spinning and writing: working, both named by their scripts")
        ok(names.get("spin.py", {}).get("stuck") and names["spin.py"]["pid"] == sp and "spin.py" in names["spin.py"]["cmd"],
           "each program sent with its number, start time and command line, for the page")
        state, detail, since, attention = tc.classify(None, "cliS", now, work)
        ok(state == "stuck" and detail == "spin.py" and attention, "the session: may be stuck, and it needs you")
        state, detail, since, attention = tc.classify(None, "cliS", now, {"cliS": dict(got, stuck=[])})
        ok(state == "working" and "writer.py" in detail and not attention, "without a stuck program: working, as before")

        # Stopping: only what the page shows, matched by number and start time.
        sp_since = names["spin.py"]["since"]
        ok(tc.stop_program(sp, sp_since + 1000)[0] == "refused" and tc.stop_program(os.getpid(), now)[0] == "refused",
           "a program the page does not show, or a number now used by another program: refused")
        result, words = tc.stop_program(sp, sp_since)
        ok(result == "stopped" and not running(sp) and words == "spin.py stopped", "a program the page shows: stopped")
        par = detached(sys.executable, tmp / "parent.py")
        stub = detached(sys.executable, tmp / "stubborn.py")
        started += [par, stub]
        time.sleep(1)
        kid = int(subprocess.run(["pgrep", "-P", str(par)], capture_output=True, text=True).stdout.split()[0])
        started.append(kid)
        for pid in (par, stub):
            tc._stoppable[(pid, tc.describe([pid])[pid][1])] = {"name": "x.py", "session": "cliS"}
        result, _ = tc.stop_program(par, tc.describe([par])[par][1])
        time.sleep(0.5)
        ok(result == "stopped" and not running(kid), "what the stopped program had started is stopped with it")
        stub_since = tc.describe([stub])[stub][1]
        result, words = tc.stop_program(stub, stub_since)
        ok(result == "running" and running(stub) and words == "x.py is still running",
           "one that ignores being asked: still running, said so")
        ok(tc.stop_program(stub, stub_since, force=True)[0] == "stopped" and not running(stub), "stopped by force on a second yes")
        tc._stoppable[(stub, stub_since)] = {"name": "x.py", "session": "cliS"}
        ok(tc.stop_program(stub, stub_since)[0] == "gone", "one that has already ended: said so")

        # From another computer, through the shared folder.
        laptop, desk = tc.Node(), tc.Node()
        laptop.name, desk.name = "laptop", "desk"
        target = detached(sys.executable, tmp / "spin.py")
        started.append(target)
        t_since = tc.describe([target])[target][1]
        (tc.STATE / "desk.json").write_text(json.dumps({"node": "desk", "sessions": [], "generatedAt": now, "stopFromOthers": False}))
        try:
            laptop.send_stop("desk", target, t_since, False)
            refused = False
        except ValueError:
            refused = True
        ok(refused and not list(tc.STATE.glob("teamcamel.stop.*")),
           "a computer whose settings do not take stop requests: not even asked")
        (tc.STATE / "desk.json").write_text(json.dumps({"node": "desk", "sessions": [], "generatedAt": now + 1, "stopFromOthers": True}))
        laptop.others.clear()
        rid = laptop.send_stop("desk", target, t_since, False)
        ok((tc.STATE / "teamcamel.stop.laptop.to.desk").is_file(), "the request goes into the shared folder, written by the asker only")
        tc._stoppable[(target, t_since)] = {"name": "spin.py", "session": "cliS"}
        ok(desk.carry_out_stops() and desk.stops[rid]["result"] == "refused" and running(target),
           "the other computer refuses while its own settings say no")
        desk.stops.clear()
        tc.save_settings({"stopFromOthers": True}, "desk")
        ok(desk.carry_out_stops() and desk.stops[rid]["result"] == "stopped" and not running(target),
           "and carries it out once they say yes, reporting the outcome in its report")
        ok(not desk.carry_out_stops() and len(desk.stops) == 1, "a request is carried out once")
        old = json.loads((tc.STATE / "teamcamel.stop.laptop.to.desk").read_text())
        old["requests"] = [dict(old["requests"][0], id="stale", at=now - 11 * 60000)]
        (tc.STATE / "teamcamel.stop.laptop.to.desk").write_text(json.dumps(old))
        desk.carry_out_stops()
        ok("stale" not in desk.stops, "a request older than ten minutes is not carried out")
        snap = desk.build()
        ok(snap["stopFromOthers"] is True and "stops" in snap, "each report says whether that computer takes stop requests")
        # A new request is noticed within a second, carried out, and reported right away.
        desk.stop_requests_changed()
        again = detached(sys.executable, tmp / "spin.py")
        started.append(again)
        time.sleep(1)
        a_since = tc.describe([again])[again][1]
        rid2 = laptop.send_stop("desk", again, a_since, False)
        ok(desk.stop_requests_changed() and not desk.stop_requests_changed(), "a new request for this computer is noticed once")
        tc._stoppable[(again, a_since)] = {"name": "spin.py", "session": "cliS"}
        desk.written_at = time.time()                      # it has just written its report: normally it would wait
        real_collect = tc.collect
        tc.collect = lambda: []
        try:
            desk.tick()
        finally:
            tc.collect = real_collect
        written = json.loads(tc.state_file("desk").read_text())
        ok(not running(again) and written["stops"].get(rid2, {}).get("result") == "stopped",
           "carried out and written to the shared folder at once, not after the usual wait")

        # The page's way in.
        import http.client
        server = tc.PageServer(("127.0.0.1", 0), tc.make_handler(desk))
        tc.threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]

        def stop_request(body, header=True):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            headers = {"Host": "127.0.0.1:%d" % port, "Content-Type": "application/json",
                       "Origin": "http://127.0.0.1:%d" % port}
            if header:
                headers["X-TeamCamel"] = "1"
            conn.request("POST", "/api/stop", body=json.dumps(body), headers=headers)
            r = conn.getresponse()
            out = (r.status, r.read())
            conn.close()
            return out
        try:
            ok(stop_request({"node": "desk", "pid": 1, "since": 0}, header=False)[0] == 403,
               "only this computer's own page may ask")
            code, body = stop_request({"node": "desk", "pid": 1, "since": 0})
            ok(code == 200 and json.loads(body)["result"] == "refused", "a program the page does not show: refused")
            ok(stop_request({"node": "desk", "pid": "1", "since": 0})[0] == 400, "a malformed request is refused")
            # Stopped from this computer's page: TeamCamel looks again at once.
            local = detached(sys.executable, tmp / "spin.py")
            started.append(local)
            time.sleep(1)
            l_since = tc.describe([local])[local][1]
            tc._stoppable[(local, l_since)] = {"name": "spin.py", "session": "cliS"}
            looks = []
            real_tick = desk.tick
            desk.tick = lambda write_now=False: looks.append(write_now)
            try:
                code, body = stop_request({"node": "desk", "pid": local, "since": l_since})
            finally:
                desk.tick = real_tick
            ok(code == 200 and json.loads(body)["result"] == "stopped" and looks == [True] and not running(local),
               "stopped from the page: TeamCamel looks again before answering, so the page shows it at once")
            ok(desk.merged()["canStop"] is (not REAL["WINDOWS"]), "the page is told this TeamCamel can stop programs")
        finally:
            server.shutdown(); server.server_close()
    finally:
        tc.STUCK_AFTER, tc.STOP_WAIT = real_after, real_wait
        for pid in started:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        tc._stoppable.clear(); tc._activity.clear()

    print("starting at login (only if chosen), stopping, the not-running page")
    restore()
    home = tmp / "login-home"
    platform("mac", home)
    import plistlib
    calls = []
    real_run, real_spawn, real_running = tc._run_quiet, tc.spawn_background, tc.is_running
    tc._run_quiet = lambda cmd: calls.append(cmd) or 0
    real_ready, real_build = tc.login_item_ready, tc.build_helper_app
    tc.login_item_ready = lambda since, wait, hint=None, sleep=None: True
    tc.build_helper_app = lambda: "no compiler here"      # first without the Mac helper app
    try:
        mac = plistlib.loads(tc.login_item_text().encode())
        ok(mac["Label"] == tc.LOGIN_LABEL and mac["ProgramArguments"][1:] == [str(tc.SELF), "--login-item"]
           and mac["RunAtLoad"] and mac["KeepAlive"] and str(home) in mac["StandardOutPath"],
           "macOS: a LaunchAgent that starts this copy at login and again if it stops, logging to TeamCamel's log")
        ok("--port 2299" in " ".join(plistlib.loads(tc.login_item_text(2299).encode())["ProgramArguments"]),
           "another port carried over")
        ok(not tc.login_item_installed() and not tc.start_login_item(), "none yet: nothing to wake")
        ok(tc.install_login_item() and tc.login_item_path() == home / "Library/LaunchAgents" / (tc.LOGIN_LABEL + ".plist")
           and tc.login_item_installed() and calls[-1][:2] == ["launchctl", "bootstrap"],
           "added on request: the file in ~/Library/LaunchAgents, loaded at once")
        calls.clear()
        ok(tc.start_login_item() and calls == [["launchctl", "kickstart", "gui/%d/%s" % (os.getuid(), tc.LOGIN_LABEL)]],
           "woken through launchd, not as a second copy")
        spawned = []
        tc.spawn_background = lambda args: spawned.append(args)
        tc.is_running = lambda port: False
        tc.ensure_running(tc.PORT)
        ok(not spawned, "the Claude hook wakes the login item instead of starting another copy")
        tc._run_quiet = lambda cmd: calls.append(cmd) or 1
        tc.ensure_running(tc.PORT)
        ok(spawned == [["--port", str(tc.PORT)]], "if launchd will not, the hook starts TeamCamel the usual way")
        tc._run_quiet = lambda cmd: calls.append(cmd) or 0
        calls.clear()
        ok(tc.remove_login_item() and not tc.login_item_path().exists() and ["launchctl", "bootout",
           "gui/%d/%s" % (os.getuid(), tc.LOGIN_LABEL)] in calls, "taken away again on request")
        # One that cannot start (macOS keeps background jobs out of cloud-storage folders) is taken away again.
        tc.login_item_ready = lambda since, wait, hint=None, sleep=None: False
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            added = tc.install_login_item()
        ok(not added and not tc.login_item_path().exists() and "taken away again" in out.getvalue()
           and "log may say why" in out.getvalue(),
           "a login item that cannot start TeamCamel is taken away at once, with why, and nothing else changes")
        tc.login_item_ready = lambda since, wait, hint=None, sleep=None: True
        platform("linux", home)
        unit = tc.login_item_text()
        ok("ExecStart=" in unit and '"%s" --login-item' % tc.SELF in unit and "Restart=always" in unit
           and tc.login_item_path() == home / ".config/systemd/user/teamcamel.service", "Linux: a systemd user service")
        calls.clear()
        tc.install_login_item()
        ok(["systemctl", "--user", "enable", "--now", "teamcamel.service"] in calls, "enabled and started for this user")
        tc.remove_login_item()
        platform("win", home)
        ok(tc.login_item_text().startswith("@echo off") and "--ensure" in tc.login_item_text()
           and tc.login_item_path().parts[-2:] == ("Startup", "TeamCamel.cmd"), "Windows: a file in the Startup folder")
        platform("mac", home)
        waits = []
        answers = iter([True, True, False])
        tc.is_running = lambda port: next(answers)
        tc.wait_for_port(tc.PORT, sleep=waits.append)
        ok(waits == [tc.LOGIN_WAIT, tc.LOGIN_WAIT], "the login item's copy stands by while another one shows the page")
        # --install-hook does not add it: starting by itself after a restart is the user's choice.
        tc.is_running = lambda port: True
        real_ensure = tc.ensure_running
        tc.ensure_running = lambda port: None
        out = io.StringIO()
        real_argv = sys.argv
        sys.argv = ["teamcamel.py", "--install-hook"]
        try:
            with contextlib.redirect_stdout(out):
                tc.main()
        finally:
            sys.argv, tc.ensure_running = real_argv, real_ensure
        ok(not tc.login_item_installed() and "--start-at-login" in out.getvalue() and str(tc.login_item_path()) in out.getvalue(),
           "--install-hook adds no login item; it says what one would add, and where, for those who want it")
        # Asked for while a copy runs: nothing is stopped; the login item's copy stands by.
        tc.is_running = lambda port: True
        stopped = []
        real_stop = tc.stop
        tc.stop = lambda port, why="": stopped.append(why) or True
        sys.argv = ["teamcamel.py", "--start-at-login"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                tc.main()
        finally:
            sys.argv, tc.stop = real_argv, real_stop
        ok(tc.login_item_installed() and not stopped, "--start-at-login stops nothing: the running copy keeps the page")
        tc.remove_login_item(quiet=True)
    finally:
        tc._run_quiet, tc.spawn_background, tc.is_running = real_run, real_spawn, real_running
        tc.login_item_ready, tc.build_helper_app = real_ready, real_build

    print("the Mac helper app the login item starts (a script started at login may not open cloud folders)")
    restore()
    home = tmp / "helper-home"
    platform("mac", home)
    real_self = tc.SELF
    try:
        for where, expect in (("Library/CloudStorage/Dropbox-Personal/x/teamcamel.py", ("files managed by Dropbox", "FileProviderDomain")),
                              ("Library/CloudStorage/GoogleDrive-me@example.com/teamcamel.py", ("files managed by Google Drive", "FileProviderDomain")),
                              ("Library/Mobile Documents/com~apple~CloudDocs/teamcamel.py", ("files managed by iCloud Drive", "FileProviderDomain")),
                              ("Documents/tc/teamcamel.py", ("files in your Documents folder", "SystemPolicyDocumentsFolder")),
                              ("Desktop/teamcamel.py", ("files in your Desktop folder", "SystemPolicyDesktopFolder")),
                              ("code/teamcamel.py", None)):
            tc.SELF = home / where
            place = tc.protected_place()
            ok((place[:2] if place else None) == expect, "macOS guards %s: %s" % (where.split("/")[0] + "/...", expect[0] if expect else "no"))
        tc.SELF = home / "Library/CloudStorage/Dropbox-Personal/tc/teamcamel.py"
        info = tc.helper_info()
        ok(info["CFBundleIdentifier"] == tc.LOGIN_LABEL and info["LSUIElement"] and info["TeamCamelScript"] == str(tc.SELF)
           and "NSFileProviderDomainUsageDescription" in info and "Contents/Resources/helper.c" in info["TeamCamelNote"],
           "its Info.plist: a background app named TeamCamel, the one script it starts, and the reason macOS shows")
        tc.SELF = home / "code/teamcamel.py"
        ok(not any(k.endswith("UsageDescription") for k in tc.helper_info()), "no reason to give where macOS asks nothing")
        mac = plistlib.loads(tc.login_item_text(2299, helper=True).encode())
        ok(mac["ProgramArguments"] == [str(tc.helper_program()), "--port", "2299"] and mac["AssociatedBundleIdentifiers"] == [tc.LOGIN_LABEL]
           and str(tc.SELF) not in tc.login_item_text(helper=True),
           "the LaunchAgent starts the helper app (which names the script), shown as TeamCamel")
        # Whose login item: told by the script the helper app starts.
        contents = tc.helper_app() / "Contents"
        (contents / "Resources").mkdir(parents=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps(tc.helper_info()))
        tc.login_item_path().parent.mkdir(parents=True)
        tc.login_item_path().write_text(tc.login_item_text(helper=True))
        ok(tc.helper_script() == str(tc.SELF) and tc.login_item_uses_helper() and tc.login_item_installed(),
           "a login item through the helper app counts as this copy's")
        tc.SELF = home / "other/teamcamel.py"
        ok(not tc.login_item_installed() and not tc.remove_helper_app() and tc.helper_app().exists(),
           "another copy's helper app is neither counted nor taken away")
        tc.SELF = home / "code/teamcamel.py"
        real_run = tc._run_quiet
        tc._run_quiet = lambda cmd: 0
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                ok(tc.remove_login_item(), "taken away on request")
        finally:
            tc._run_quiet = real_run
        ok(not tc.login_item_path().exists() and not tc.helper_app().exists() and not tc.helper_app().parent.exists()
           and "Helper app removed" in out.getvalue(), "with its helper app, and the folder it was in")
        # Started? Its copy says so in a small file next to the log.
        since = tc.now_ms()
        ok(not tc.login_item_ready(since, 0), "not started yet")
        tc.note_login_item_started()
        ok(tc.login_item_ready(since, 0) and not tc.login_item_ready(tc.now_ms() + 1000, 0),
           "started: said by the login item's copy itself, counted only if after the request")
        tc.login_ready_path().unlink()
        out, waits = io.StringIO(), []
        with contextlib.redirect_stdout(out):
            gave = tc.login_item_ready(since, 6, hint="click Allow", sleep=waits.append)
        ok(not gave and len(waits) == 6 and out.getvalue().count("click Allow") == 1, "waits, and says once what to click")
        # Asked, and not allowed: taken away again, with how to be asked again.
        tc.SELF = home / "Library/CloudStorage/Dropbox-Personal/tc/teamcamel.py"
        real = (tc.build_helper_app, tc.login_item_ready, tc._run_quiet)
        built, hints = [], []
        def fake_build():
            contents.mkdir(parents=True, exist_ok=True)
            (contents / "Info.plist").write_bytes(plistlib.dumps(tc.helper_info()))
            built.append(1)
        tc.build_helper_app = fake_build
        tc.login_item_ready = lambda since, wait, hint=None, sleep=None: hints.append((wait, hint)) and False
        tc._run_quiet = lambda cmd: 0
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                added = tc.install_login_item()
        finally:
            tc.build_helper_app, tc.login_item_ready, tc._run_quiet = real
        said = out.getvalue()
        ok(built and str(tc.helper_app()) in said and str(tc.login_item_path()) in said and "starts this teamcamel.py and nothing else" in said,
           "it says first what it adds, and where")
        ok(hints and hints[0][0] == tc.LOGIN_ALLOW_WAIT and "files managed by Dropbox" in hints[0][1] and "Click Allow" in hints[0][1],
           "it waits for the answer to macOS's question, and says what to click")
        ok(not added and not tc.login_item_path().exists() and not tc.helper_app().exists()
           and "tccutil reset FileProviderDomain %s" % tc.LOGIN_LABEL in said,
           "not allowed: login item and helper app taken away again, with how to be asked again")
        # The real thing, built and run here (not through launchd).
        if REAL["MACOS"] and subprocess.run(["xcode-select", "-p"], capture_output=True).returncode == 0:
            fake = tmp / "fake-tc"; fake.mkdir()
            tc.SELF = fake / "teamcamel.py"
            tc.SELF.write_text("import json, os, signal, sys, time\n"
                               "def bye(s, f):\n    open(sys.argv[0] + '.seen', 'a').write(json.dumps(['stop', s]) + '\\n'); sys.exit(0)\n"
                               "signal.signal(signal.SIGTERM, bye)\n"
                               "open(sys.argv[0] + '.seen', 'a').write(json.dumps([sys.argv[1:], os.getppid()]) + '\\n')\n"
                               "time.sleep(30)\n")
            ok(tc.build_helper_app() is None and tc.helper_script() == str(tc.SELF), "built on this Mac")
            sig = lambda: [l for l in subprocess.run(["codesign", "-dvvv", str(tc.helper_app())], capture_output=True,
                                                     text=True).stderr.splitlines() if l.startswith(("CDHash=", "Signature="))]
            first = sig()
            ok("Signature=adhoc" in first and subprocess.run(["codesign", "--verify", "--strict", str(tc.helper_app())]).returncode == 0,
               "signed on this Mac, and intact")
            stamp = tc.helper_program().stat().st_mtime_ns
            ok(tc.build_helper_app() is None and tc.helper_program().stat().st_mtime_ns == stamp, "left as it is when nothing changed")
            shutil.rmtree(str(tc.helper_app()))
            ok(tc.build_helper_app() is None and sig() == first, "built again: the very same app, so macOS's permission still fits")
            proc = subprocess.Popen([str(tc.helper_program()), "--port", "47392"])
            deadline = time.time() + 10
            seen = Path(str(tc.SELF) + ".seen")
            while time.time() < deadline and not seen.exists():
                time.sleep(0.1)
            proc.terminate()
            code = proc.wait(timeout=10)
            lines = [json.loads(l) for l in seen.read_text().splitlines()]
            ok(lines[0] == [["--login-item", "--port", "47392"], proc.pid] and lines[1] == ["stop", 15] and code == 0,
               "it starts that script as its child, with --login-item and the port, and passes a stop on")
            tc.remove_helper_app()
    finally:
        tc.SELF = real_self
        restore()

    # The last word: why it stopped, in its report.
    tc._note_stop("stopped with --stop")
    ok(tc._stop_reason() == "stopped with --stop" and tc._stop_reason() is None, "a stop says why, once")
    tc._stop_note_path().write_text(json.dumps({"why": "old", "at": now - 300000}))
    ok(tc._stop_reason() is None, "an old note does not count")
    tc.STATE = tmp / "login-state"; tc.STATE.mkdir()
    node = tc.Node()
    node.goodbye("shutting down, restarting or logging out")
    last = json.loads(tc.state_file(node.name).read_text())
    ok(last["stopped"]["why"] == "shutting down, restarting or logging out" and "sessions" in last,
       "the last report says it stopped, and why, with the sessions as last seen")
    # For real: a running copy stopped by the system, and one stopped with --stop.
    if not REAL["WINDOWS"]:
        demo = tmp / "sig"; (demo / "web").mkdir(parents=True)
        shutil.copy2(str(REAL["SELF"]), str(demo / "teamcamel.py"))
        (demo / "web" / "index.html").write_text("<html></html>\n")
        dhome = tmp / "sig-home"; (dhome / ".claude").mkdir(parents=True)
        (dhome / ".claude" / "nodename").write_text("sigtest\n")
        env = dict({k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}, HOME=str(dhome))
        env.pop("XDG_STATE_HOME", None); env.pop("XDG_CONFIG_HOME", None)
        for note, expect in ((None, "shutting down, restarting or logging out"), ("stopped with --stop", "stopped with --stop")):
            proc = subprocess.Popen([sys.executable, str(demo / "teamcamel.py"), "--port", "47391"], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.time() + 20
            while time.time() < deadline and not tc.is_running(47391):
                time.sleep(0.3)
            if note:
                logs = dhome / ("Library/Logs" if REAL["MACOS"] else ".local/state/teamcamel")
                logs.mkdir(parents=True, exist_ok=True)
                (logs / "teamcamel.stopping").write_text(json.dumps({"why": note, "at": tc.now_ms()}))
            proc.terminate(); proc.wait(timeout=10)
            report = json.loads((demo / "state" / "sigtest.json").read_text())
            ok(report.get("stopped", {}).get("why") == expect, "stopped for real (%s): its last report says so" % expect)
    # The page shown when TeamCamel does not answer.
    sw = tc.service_worker()
    shown = json.loads(sw.split("const PAGE = ", 1)[1].split(";\nself.addEventListener", 1)[0])
    ok("respondWith" in sw and "navigate" in sw and json.dumps(tc.start_command()) in shown
       and "TeamCamel is not running on this computer" in shown and str(tc.SELF) in tc.start_command(),
       "a helper for the browser that shows 'not running', with the command to start it, when TeamCamel does not answer")
    if shutil.which("node"):
        (tmp / "sw.js").write_text(sw)
        ok(subprocess.run(["node", "--check", str(tmp / "sw.js")], capture_output=True).returncode == 0, "the helper is valid JavaScript")
    restore()

    print("two folders with the same name are two workspaces")
    cases = tmp / "same-name"
    for d in ("_Kian/workspace", "_Grinberg/workspace", "a/x/deep/workspace", "b/x/deep/workspace", "side/wp-abc", "side/abc", "solo/wp-one"):
        (cases / d).mkdir(parents=True)
    (cases / "link-to-kian").symlink_to(cases / "_Kian")
    def ws_of(dirs):
        ss = [{"id": str(i), "ws": tc.ws_name(d), "_dir": d} for i, d in enumerate(dirs)]
        tc.tell_workspaces_apart(ss)
        return [s["ws"] for s in ss]
    k, g = str(cases / "_Kian/workspace"), str(cases / "_Grinberg/workspace")
    ok(ws_of([k, g, k]) == ["_Kian/workspace", "_Grinberg/workspace", "_Kian/workspace"],
       "two cases each in a folder 'workspace': each label names the folder it sits in")
    ok(ws_of([k, str(cases / "link-to-kian/workspace"), k + "/.claude/worktrees/fix"]) == ["workspace"] * 3,
       "the same folder reached through a link, or from a worktree in it, stays one workspace, named as before")
    ok(ws_of([str(cases / "a/x/deep/workspace"), str(cases / "b/x/deep/workspace")]) == ["a/x/deep/workspace", "b/x/deep/workspace"],
       "going further up only as far as needed to tell them apart")
    ok(ws_of([str(cases / "side/wp-abc"), str(cases / "side/abc")]) == ["side/wp-abc", "side/abc"],
       "wp-abc and abc side by side: their own names tell them apart")
    ok(ws_of([str(cases / "solo/wp-one"), None]) == ["one", "(no folder)"], "a name used once is left as it was")
    ss = [{"id": "1", "ws": "workspace", "_dir": k}]
    tc.tell_workspaces_apart(ss)
    ok(ss == [{"id": "1", "ws": "workspace"}], "the folder itself is not passed on in the report")

    print("versions of Claude Code and Codex")
    restore()
    home = tmp / "ver-home"
    platform("mac" if REAL["MACOS"] else "linux", home)
    ok(tc.version_key("codex-cli 0.162.0-alpha.17.2") == (0, 162, 0) and tc.version_key("2.1") == (2, 1, 0)
       and tc.version_key("none") is None, "version numbers read from version texts")
    ok(tc.newest_version(["2.1.99", "2.1.299", None, "2.0.300", "junk"]) == "2.1.299", "newest of several, by number not by text")
    real_which, real_cmd, real_app = tc.shutil.which, tc.command_version, tc.CODEX_APP_CLI
    tc.shutil.which = lambda name: None      # a computer with no claude or codex command and no Codex app
    tc.CODEX_APP_CLI = ()
    try:
        tc._version_cache.clear()
        ok(tc.claude_code_version([{"version": "2.1.290"}, {"version": "2.1.301"}]) == "2.1.301",
           "Claude Code: the newest its running sessions use")
        for v in ("2.1.288", "2.1.302"):
            (tc.desktop_roots()[0] / "claude-code" / v).mkdir(parents=True)
        ok(tc.claude_code_version([]) == "2.1.302", "no running session: the newest the desktop app holds")
        rollout = home / "rollout.jsonl"
        write(rollout, '{"type":"session_meta","payload":{"id":"t1","cli_version":"0.163.0","originator":"Codex Desktop"}}\n')
        real_threads = tc.codex_threads
        tc.codex_threads = lambda: [{"id": "t1", "rollout_path": str(rollout), "updated_at_ms": now}]
        tc._version_cache.clear()
        ok(tc.codex_version() == "0.163.0", "Codex: the version its newest thread was run with")
        write(rollout, '{"type":"session_meta","payload":{"id":"t1","cli_version":"0.124.0-alpha.2"}}\n')
        tc.shutil.which = lambda name: "/usr/local/bin/codex" if name == "codex" else None
        tc.command_version = lambda exe: "0.163.0"
        tc._version_cache.clear()
        ok(tc.codex_version() == "0.163.0", "Codex updated since its last thread: the installed version counts")
        tc.shutil.which, tc.command_version = (lambda name: None), real_cmd
        tc.codex_threads = lambda: []
        tc._version_cache.clear()
        ok(tc.codex_version() is None, "no Codex: no Codex version")
        tc.codex_threads = real_threads
    finally:
        tc.shutil.which, tc.command_version, tc.CODEX_APP_CLI = real_which, real_cmd, real_app
        tc._version_cache.clear()

    print("finished sessions: archived, folder deleted; renamed and moved folders followed")
    restore()
    tc.STATE = tmp / "fate-state"
    tc._folders = None
    proj = tmp / "wp-alpha"
    proj.mkdir()
    old = str(proj)
    ok(tc.follow_folder(old, {}) == (None, old), "a folder in its place: nothing to say")
    proj.rename(tmp / "wp-alpha-2026")
    renamed = os.path.realpath(str(tmp / "wp-alpha-2026"))     # the system reports real paths (/private/var for /var)
    if REAL["MACOS"]:
        ok(tc.follow_folder(old, {}) == (None, renamed), "folder renamed: followed to its new name, nothing to say")
        e = tc.with_folder({"ws": "alpha", "attention": True}, old, {})
        ok(e == {"ws": "alpha-2026", "attention": True, "_dir": renamed},
           "the session carries on under the new name, state unchanged (its new folder noted for the labels)")
        (tmp / "elsewhere").mkdir()
        (tmp / "wp-alpha-2026").rename(tmp / "elsewhere" / "wp-alpha-2026")
        moved = os.path.realpath(str(tmp / "elsewhere" / "wp-alpha-2026"))
        ok(tc.follow_folder(old, {}) == (None, moved), "then moved: followed there too")
        tc.save_folder_ids(); tc._folders = None
        ok(tc.follow_folder(old, {}) == (None, moved), "followed folders kept between restarts, in the shared folder")
        shutil.rmtree(moved)
        ok(tc.follow_folder(old, {})[0] == "deleted", "then deleted: 'deleted'")
        proj.mkdir(); tc.follow_folder(str(proj), {})
        tc._folders["ids"][str(proj)][0] += 1                    # as if the disk's number changed after a restart
        proj.rmdir()
        ok(tc.follow_folder(str(proj), {})[0] == "missing", "a number from another disk is not trusted: only 'not found'")
    else:
        ok(tc.follow_folder(old, {})[0] == "missing", "folder renamed: 'not found' (no lookup by number here)")
    ok(tc.follow_folder(str(tmp / "never-seen"), {})[0] == "missing", "a folder never seen and not there: only 'not found'")
    ok(tc._in_trash("/Users/x/.Trash/proj") and tc._in_trash("/Volumes/Disk/.Trashes/501/proj")
       and not tc._in_trash("/Users/x/work/proj"), "the Trash told from other places")
    e = tc.with_folder({"attention": True}, str(tmp / "never-seen"), {})
    ok(e == {"attention": True, "folder": "missing", "_dir": str(tmp / "never-seen")}, "folder not found: noted, still needing you")
    seen = {str(tmp / "gone"): ("deleted", str(tmp / "gone"))}
    ok(tc.with_folder({"attention": True}, str(tmp / "gone"), seen) == {"attention": False, "folder": "deleted", "_dir": str(tmp / "gone")},
       "folder deleted: no longer needing you")
    tc._folders = None
    write(tc.folder_ids_path(), {"/old/style": [1, 2]})        # as 2.4 kept them
    ok(tc._folder_table() == {"ids": {"/old/style": [1, 2]}, "moved": {}}, "numbers noted by 2.4 still read")
    home = tmp / "arch-home"
    platform("mac" if REAL["MACOS"] else "linux", home)
    tc.STATE = tmp / "fate-state"
    store = home / ("Library/Application Support/Claude" if REAL["MACOS"] else ".config/Claude") / "claude-code-sessions/acct/org"
    write(store / "local_Z.json", {"sessionId": "local_Z", "cliSessionId": "cliZ", "title": "Closed and archived",
                                   "cwd": "/u/wp-old", "lastActivityAt": now - 90000, "isArchived": True})
    z_pid = sleepers[0].pid
    write(home / ".claude/sessions" / ("%d.json" % z_pid), {"pid": z_pid, "sessionId": "cliZ", "hostSessionId": "local_Z",
                                                        "procStart": lstart(z_pid), "status": "waiting", "name": "Closed and archived"})
    got = [s for s in tc.collect() if s["title"] == "Closed and archived"]
    ok(len(got) == 1 and got[0]["archived"] and not got[0]["attention"] and got[0]["state"] == "completed"
       and got[0]["detail"] == "archived", "archived in Claude: listed once, grey, never needing you, even while still running")

    print("a finished turn that asks something of you")
    restore()
    ok(tc.turn_needs({"turnWrapUp": {"description": "x", "needsUser": "Take batch 6?"}}) == "Take batch 6?",
       "the wrap-up's question")
    ok(tc.turn_needs({"postTurnSummary": {"status_category": "blocked", "needs_action": "your choices"}}) == "your choices",
       "no wrap-up: the turn summary's 'blocked' with what is needed")
    ok(tc.turn_needs({"postTurnSummary": {"status_category": "need_input", "needs_action": ""}}) == "your reply",
       "'need input' with nothing more said")
    ok(tc.turn_needs({"turnWrapUp": {"description": "done"}, "postTurnSummary": {"status_category": "blocked"}}) is None,
       "a wrap-up without a question means nothing is needed, whatever the summary says (as the Claude app reads it)")
    ok(tc.turn_needs({"postTurnSummary": {"status_category": "completed"}}) is None and tc.turn_needs({}) is None,
       "completed, or nothing kept: nothing needed")
    ok(tc.short("a " * 60).endswith("…") and len(tc.short("a " * 60)) <= 70, "a long question shortened")
    home = tmp / "ask-home"
    platform("mac" if REAL["MACOS"] else "linux", home)
    store = home / ("Library/Application Support/Claude" if REAL["MACOS"] else ".config/Claude") / "claude-code-sessions/acct/org"
    done = {"type": "assistant", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Take batch 6 as recommended?"}]}}
    for key, title, extra in (("Q", "Asks a question", {"turnWrapUp": {"description": "batch 5 done", "needsUser": "Take batch 6 as recommended?"},
                                                        "lastTurnEdgeAt": now - 60000}),
                              ("R", "Asked long ago", {"turnWrapUp": {"needsUser": "Which one?"}, "lastTurnEdgeAt": now - 9 * 86400 * 1000}),
                              ("S", "Just finished", {"turnWrapUp": {"description": "all done"}, "lastTurnEdgeAt": now - 60000})):
        write(store / ("local_%s.json" % key), dict({"sessionId": "local_" + key, "cliSessionId": "cli" + key, "title": title,
                                                     "cwd": "/u/wp-review", "lastActivityAt": now - 60000}, **extra))
        transcript(home / ".claude/projects/p1" / ("cli%s.jsonl" % key), done)
    got = {s["title"]: s for s in tc.collect()}
    ok(got["Asks a question"]["state"] == "input" and got["Asks a question"]["detail"] == "Take batch 6 as recommended?"
       and got["Asks a question"]["attention"] and got["Asks a question"]["since"] == now - 60000,
       "a turn that ended with a question: awaits input, with the question")
    ok(got["Asked long ago"]["state"] == "completed", "a question left for over 7 days, session not running: no longer counted")
    ok(got["Just finished"]["state"] == "completed", "a turn that ended without a question: completed")

    # The Claude app's end-of-turn card: a recap, and a question with answer buttons.
    def card_turn(key, card, reply="Shown in the next-step box. Your closing message is your answer to the user.",
                  after=(), wrap=None, blocked_rec=False):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        recs = [{"type": "user", "timestamp": stamp, "message": {"content": "set up the case"}},
                {"type": "assistant", "timestamp": stamp, "message": {"stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": "tu_" + key, "name": "mcp__ccd_turn__wrap_up", "input": card}]}},
                {"type": "user", "timestamp": stamp, "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "tu_" + key, "content": [{"type": "text", "text": reply}]}]}},
                {"type": "assistant", "timestamp": stamp, "message": {"stop_reason": "end_turn", "content": [
                    {"type": "text", "text": "The workspace is set up."}]}}] + list(after)
        write(home / ".claude/projects/p1" / ("cli%s.jsonl" % key),
              "\n".join(json.dumps(x, separators=(",", ":")) for x in recs) + "\n")
        write(store / ("local_%s.json" % key), dict({"sessionId": "local_" + key, "cliSessionId": "cli" + key,
                                                    "title": "Card " + key, "cwd": "/u/wp-cards",
                                                    "lastActivityAt": now - 60000, "lastTurnEdgeAt": now - 60000},
                                                   **({"turnWrapUp": wrap} if wrap else {})))

    buttons = [{"label": "Add both warnings", "message": "Add both."}, {"label": "Skip them"}]
    card_turn("C1", {"recap": "The workspace is set up.", "ask": "Add the firm's 2-day warnings?", "actions": buttons},
              wrap={"description": "The workspace is set up."})
    card_turn("C2", {"recap": "Done.", "ask": "Run the check now?", "blocked": True, "actions": buttons})
    card_turn("C3", {"recap": "All done; nothing waits on you."}, reply="Nothing is pending, so the app draws no box.")
    card_turn("C4", {"recap": "Done.", "ask": "Next?"}, reply="The app draws no box for this call; the recap is the session's status.")
    later = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    card_turn("C5", {"recap": "Done.", "ask": "Add the warnings?", "actions": buttons},
              after=[{"type": "user", "timestamp": later, "message": {"content": "Add both."}},
                     {"type": "assistant", "timestamp": later, "message": {"stop_reason": "end_turn",
                                                                            "content": [{"type": "text", "text": "Added."}]}}])
    got = {s["title"]: s for s in tc.collect()}
    ok(got["Card C1"]["state"] == "input" and got["Card C1"]["detail"] == "Add the firm's 2-day warnings?"
       and got["Card C1"]["attention"],
       "a turn that ended with the app's card asking a question (one that 'can wait'): awaits input, with the question")
    ok(got["Card C2"]["state"] == "input" and got["Card C2"]["detail"] == "Run the check now?",
       "the same card marked as blocking: awaits input")
    ok(got["Card C3"]["state"] == "completed", "a card with only a recap, nothing asked: completed")
    ok(got["Card C4"]["state"] == "completed", "a question the app did not draw a card for: completed")
    ok(got["Card C5"]["state"] == "completed", "the card's question answered (a button or a reply): completed")
    write(store / "local_U.json", {"sessionId": "local_U", "cliSessionId": "cliU-new", "priorCliSessionIds": ["cliU-old"],
                                   "title": "Long upload", "cwd": "/u/wp-photos", "lastActivityAt": now - 60000})
    transcript(home / ".claude/projects/p1" / "cliU-new.jsonl", done)
    real_bw = tc.background_work
    tc.background_work = lambda registry, desktop=None: {"cliU-old": {"detail": "in background (rclone)", "since": now - 3600000,
                                                                      "stuck": [], "programs": []}}
    try:
        u_got = {s["title"]: s for s in tc.collect()}["Long upload"]
    finally:
        tc.background_work = real_bw
    ok(u_got["state"] == "working" and u_got["detail"] == "in background (rclone)",
       "a program started under one of the session's earlier internal ids still counts as the session's work")

    print("shared settings")
    restore()
    tc.STATE = tmp / "settings-state"
    ok(tc.clean_settings({"order": ["a", 3], "apps": {"claude": False, "x": True}, "colors": {"bg": "#112233", "attn": "red"},
                          "notify": {"popup": True, "sound": "yes"}, "evil": "<script>", "online": "yes"})
       == {"order": ["a"], "apps": {"claude": False}, "colors": {"bg": "#112233"}, "notify": {"popup": True}},
       "settings cleaned: unknown keys and wrong kinds dropped")
    ok(tc.shared_settings() is None, "no shared settings yet")
    node = tc.Node()
    server = tc.PageServer(("127.0.0.1", 0), tc.make_handler(node))
    threading_mod = __import__("threading")
    threading_mod.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    import http.client

    def post(body, **headers):
        h = {"Host": "127.0.0.1:%d" % port, "Content-Type": "application/json", "X-TeamCamel": "1",
             "Origin": "http://127.0.0.1:%d" % port}
        h.update({k.replace("_", "-"): v for k, v in headers.items()})
        h = {k: v for k, v in h.items() if v is not None}
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/settings", body=body, headers=h)
        code = conn.getresponse().status
        conn.close()
        return code
    try:
        good = json.dumps({"order": ["primary"], "versions": False, "colors": {"bg": "#FFFFFF"}})
        ok(post(good) == 200 and tc.shared_settings() == {"order": ["primary"], "versions": False, "colors": {"bg": "#FFFFFF"}},
           "this computer's page saves settings for all computers")
        ok(json.load(urllib_request.urlopen("http://127.0.0.1:%d/api/state" % port, timeout=5))["settings"]["order"] == ["primary"],
           "shared settings sent with the page data")
        before = (tc.STATE / tc.SETTINGS_SHARED).read_text()
        ok(post(good, Origin="https://some-web-site.example") == 403, "another web site's page cannot change them")
        ok(post(good, X_TeamCamel=None) == 403, "a request without TeamCamel's own header is refused")
        ok(post(good, Host="attacker.example") == 403, "a request for another host name is refused")
        ok(post("x" * (tc.SETTINGS_MAX + 1)) == 413, "an oversized request is refused")
        ok(post("{ not json") == 400, "a malformed request is refused")
        ok((tc.STATE / tc.SETTINGS_SHARED).read_text() == before, "refused requests changed nothing")
        mine = json.dumps({"scope": "mine", "settings": {"notify": {"title": True}, "order": ["worker2"]}})
        ok(post(mine) == 200 and tc.own_settings(node.name) == {"notify": {"title": True}, "order": ["worker2"]}
           and tc.shared_settings()["order"] == ["primary"], "this computer's own settings kept apart from those for all")
        state = json.load(urllib_request.urlopen("http://127.0.0.1:%d/api/state" % port, timeout=5))
        ok(state["mySettings"]["notify"] == {"title": True} and state["settings"]["order"] == ["primary"],
           "the page receives both, for every browser on the computer")
        ok(state["settingsScopes"] is True, "the page is told this TeamCamel keeps own and shared settings apart")
        ok(post(json.dumps({"scope": "shared", "settings": {"versions": True}})) == 200 and tc.own_settings(node.name) is None
           and tc.shared_settings() == {"versions": True}, "choosing 'all my computers' drops this computer's own settings")
        ok(post(json.dumps({"scope": "elsewhere", "settings": {}})) == 400, "an unknown scope is refused")
        ok(tc.clean_settings({"refresh": 60}) == {"refresh": 60} and tc.clean_settings({"refresh": 7}) == {}
           and tc.clean_settings({"refresh": True}) == {} and tc.clean_settings({"refresh": "60"}) == {},
           "refresh: only the offered intervals are kept")
        ok(node.refresh_every() == tc.COLLECT_EVERY and node.merged()["pollSec"] == tc.COLLECT_EVERY,
           "no refresh chosen: every 15 seconds, and the page polls as often")
        post(json.dumps({"scope": "shared", "settings": {"refresh": 60}}))
        ok(node.refresh_every() == 60 and node.merged()["pollSec"] == 60, "refresh chosen for all computers: followed")
        post(json.dumps({"scope": "mine", "settings": {"refresh": 5}}))
        ok(node.refresh_every() == 5, "this computer's own refresh comes before the one for all")
        post(json.dumps({"scope": "mine", "settings": {"versions": True}}))
        ok(node.refresh_every() == tc.COLLECT_EVERY, "own settings without a refresh: the usual 15 seconds, as the page shows")
        post(json.dumps({"scope": "shared", "settings": {"versions": True}}))
        def mark(body):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/api/handled", body=json.dumps(body), headers={
                "Host": "127.0.0.1:%d" % port, "Content-Type": "application/json", "X-TeamCamel": "1",
                "Origin": "http://127.0.0.1:%d" % port})
            code = conn.getresponse().status
            conn.close()
            return code
        ok(mark({"node": "primary", "id": "local_A", "since": now}) == 200 and node.merged()["handled"] == {"primary/local_A": now},
           "a session marked handled, kept for every computer's page")
        ok(mark({"node": "primary", "id": "local_A", "since": None}) == 200 and node.merged()["handled"] == {},
           "and unmarked again")
        ok(mark({"node": "primary", "id": 5, "since": now}) == 400 and mark({"node": "primary", "id": "x", "since": "now"}) == 400,
           "a malformed mark is refused")
        ok(mark({"node": "primary", "id": "old", "since": now - 200 * 86400 * 1000}) == 200 and node.merged()["handled"] == {},
           "marks older than 90 days are let go")
        ok(post(json.dumps(["not", "an", "object"])) == 400, "a request that is not settings is refused")
        ok(all(not p.name.startswith(tc.SETTINGS_SHARED) for p in tc.STATE.glob("*.json")),
           "settings files are never taken for a computer's report")
    finally:
        server.shutdown(); server.server_close()

    print("the page")
    restore()
    html = (tc.WEB / "index.html").read_text(encoding="utf-8")
    ok('<svg class="logo"' in html and 'aria-label="TeamCamel"' in html and "<text" not in html and "@font-face" not in html,
       "footer logo is drawn as shapes: no text element and no font file needed")
    ok("bothApps" in html and "'Open in ' + app" in html, "page labels sessions when both kinds are present, and links by app")
    ok(html.count('<span id="credit">') == 1 and '<span id="ver"></span>' in html,
       "footer has the copyright line in its own element (a tester package leaves it out) and the version")
    ok("location.protocol === 'file:'" in html and "function guide()" in html,
       "a page opened as a file shows the setup guide instead of asking TeamCamel for sessions")
    ok(all("['%s'," % name in html for name in tc.SYNC_NAMES),
       "the setup guide knows the same synced-folder names as the precheck")
    ok("sh precheck.sh" in html and "precheck.bat" in html and "python3 teamcamel.py --install-hook" in html,
       "the setup guide gives the README's check and install commands")
    ok("class=\"panel\" id=\"settings\"" in html and "'X-TeamCamel': '1'" in html and "registry.npmjs.org" in html,
       "settings panel present; shared settings saved with TeamCamel's header; newest versions from the npm registry")
    ok(all(k in html for k in ("notify: { popup: false, sound: false, title: false }", "online: false")),
       "out of the box: no notices and no online look-up")
    ok('id="hint"' in html and "dataset.tip" in html and "node.title = text" not in html,
       "hints are the page's own, shown at once, not the browser's delayed ones")
    ok("'Mark if handled'" in html and "tips: true," in html and "function tip(" in html and "'Legend: the colors of the statuses'" in html,
       "an empty square to mark a session handled; help messages, which the settings can turn off")
    real_web = tc.WEB
    tc.WEB = tmp / "half-web"; tc.WEB.mkdir()
    try:
        (tc.WEB / "index.html").write_text("<html><body>first</body></html>\n")
        first, stamp = tc.page_body(), tc.page_stamp()
        time.sleep(0.05)
        (tc.WEB / "index.html").write_text("<html><body>second, still being writ")
        ok(tc.page_body() == first and tc.page_stamp() == stamp,
           "a page file caught half-written by the shared folder: the last whole copy is served, and no reload is asked")
        time.sleep(0.05)
        (tc.WEB / "index.html").write_text("<html><body>second</body></html>\n")
        ok(b"second" in tc.page_body() and tc.page_stamp() != stamp, "once it is whole: the new page, and open pages reload")
    finally:
        tc.WEB = real_web
    ok("register('/teamcamel-sw.js')" in html and "teamcamel.lastSeen" in html and "'[TeamCamel stopped '" in html,
       "the page keeps the not-running helper, and shows another computer's last word when it stopped")
    ok("Mark handled anyway" in html and "Stop it instead" in html and "s.state === 'stuck' && !s.handled" in html,
       "marking a session that may be stuck handled asks first: it does not stop the program")
    ok("await refresh(); else render();" in html and "quick = setTimeout" in html,
       "after a stop the page fetches the new status at once; while another computer is asked, every few seconds")
    ok("function askStop" in html and "'/api/stop'" in html and "may be stuck" in html and "stopFromOthers: false" in html,
       "the page names stuck programs and can ask to stop one")
    ok("function markHandled" in html and "archived: true," in html and "'/api/handled'" in html,
       "the page can mark a session handled and shows archived sessions in grey")
    ok("function updateArrows" in html and "othersColumn" not in html and ".nodes > .node { flex: 1 0 320px" in html,
       "one column per computer, with arrows to sessions out of view")
    ok("r.bottom > bottom ? r.bottom - bottom" in html and "(window.innerHeight - r.height) / 2" not in html,
       "an arrow scrolls only as far as needed, so the sessions in view stay in view")
    ok(".node:not(.stale) .s.attention:not(.quiet)" in html and ".node:not(.stale) .s.working:not(.quiet)" in html
       and "col.dataset.node = p.node.node" in html,
       "arrows only for sessions shown in red or blue, and they name the computer whose column it is in")
    ok("'@' + cols.indexOf(col)" in html and "Math.max(c.left, left) - 36" in html and ".arrow.up.work, .arrow.down.work" not in html,
       "each computer's column has its own up and down arrows, just left of it, for its own sessions only")
    ok("refresh: 15," in html and all("[%d, '" % sec in html for sec in tc.REFRESH_CHOICES),
       "the page offers the same refresh intervals as TeamCamel, 15 seconds by default")

    print("other nodes' files")
    restore()
    tc.STATE = tmp / "state"; tc.STATE.mkdir()
    node = tc.Node()
    good = {"schema": 1, "node": "office-pc", "os": "Windows", "generatedAt": now, "sessions": []}
    write(tc.STATE / "office-pc.json", good)
    write(tc.STATE / "some other name.json", dict(good, node=node.name))
    ok([n["node"] for n in node.merged()["nodes"]] == [node.name, "office-pc"], "own file skipped by its content, other node listed")
    write(tc.STATE / "office-pc.json", '{"schema": 1, "node": "office-pc", "sess')
    ok([n["node"] for n in node.merged()["nodes"]] == [node.name, "office-pc"], "file caught half-synced: last good copy kept")
    write(tc.STATE / "notes.json", ["not", "a", "node"])
    ok(len(node.merged()["nodes"]) == 2 and node.merged()["version"] == tc.__version__, "unrelated file ignored; version reported")
    snap = node.snapshot
    ok(isinstance(snap.get("programs"), list), "the report lists what programs sessions started are doing")
    ok(snap["version"] == tc.__version__ and snap["os"] == tc.os_label() and snap["schema"] == 1,
       "snapshot carries version and system: %s, %s" % (snap["version"], snap["os"]))
finally:
    restore()
    for p in sleepers:
        p.kill()
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d checks passed" % passed)
