"""Stand-in Claude Code files for a real Linux run of TeamCamel.

Claude Code is not installed in the test machine, so this writes the files it would
write, in the places it would write them, backed by real long-running processes:
the registry entries carry the pid and the `ps` start time of actual processes.
"""
import json
import os
import subprocess
import time
from pathlib import Path

home = Path.home()
now = int(time.time() * 1000)


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")


def lstart(pid):
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                         env=dict(os.environ, LC_ALL="C", TZ="UTC")).stdout
    return out.strip()


def transcript(name, last):
    lines = [{"type": "user", "timestamp": "2026-10-10T10:00:00.000Z", "message": {"content": "go"}}, last]
    write(home / ".claude" / "projects" / "-home-u-projects" / (name + ".jsonl"),
          "\n".join(json.dumps(x, separators=(",", ":")) for x in lines) + "\n")


procs = [subprocess.Popen(["sleep", "900"], start_new_session=True) for _ in range(2)]
time.sleep(0.3)
waiting, busy = (p.pid for p in procs)

store = home / ".config/Claude/claude-code-sessions/acct/org"
write(store / "local_L1.json", {"sessionId": "local_L1", "cliSessionId": "cli-L1", "title": "Linux desktop session awaiting input",
                                "originCwd": "/home/u/projects/wp-linux-alpha", "lastActivityAt": now - 120000})
write(store / "local_L2.json", {"sessionId": "local_L2", "cliSessionId": "cli-L2", "title": "Linux desktop session, finished",
                                "originCwd": "/home/u/projects/wp-linux-alpha", "lastActivityAt": now - 3600000})
write(store / "local_L3.json", {"sessionId": "local_L3", "cliSessionId": "cli-L3", "title": "Linux desktop session, connection dropped",
                                "originCwd": "/home/u/projects/linux-beta", "lastActivityAt": now - 600000})
transcript("cli-L2", {"type": "assistant", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 3600)),
                      "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}})
transcript("cli-L3", {"type": "assistant", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 600)),
                      "isApiErrorMessage": True,
                      "message": {"content": [{"type": "text", "text": "API Error: The socket connection was closed unexpectedly"}]}})
reg = home / ".claude/sessions"
write(reg / ("%d.json" % waiting), {"pid": waiting, "sessionId": "cli-L1", "hostSessionId": "local_L1", "procStart": lstart(waiting),
                                    "kind": "interactive", "entrypoint": "claude-desktop", "name": "Linux desktop session awaiting input",
                                    "cwd": "/home/u/projects/wp-linux-alpha", "status": "waiting", "waitingFor": "permission prompt",
                                    "statusUpdatedAt": now - 60000, "updatedAt": now - 60000})
write(reg / ("%d.json" % busy), {"pid": busy, "sessionId": "cli-T1", "procStart": lstart(busy), "kind": "interactive",
                                 "entrypoint": "cli", "name": "Linux terminal session working", "cwd": "/home/u/code/linux-gamma",
                                 "status": "busy", "statusUpdatedAt": now - 20000, "updatedAt": now - 20000})
print("made-up Claude files written; real processes: waiting=%d (%s)  busy=%d (%s)"
      % (waiting, lstart(waiting), busy, lstart(busy)))
