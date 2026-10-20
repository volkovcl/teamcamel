#!/usr/bin/env python3
r"""TeamCamel: one page showing which Claude Code and Codex sessions need attention, on every computer.

Runs on macOS, Windows and Linux (standard library only, Python 3.9+). The page on
any of them shows the sessions of all of them.

    sh precheck.sh   (Windows: precheck.bat)
                                     run this first: says whether anything TeamCamel
                                     needs is missing on this computer, and what to do
                                     about it. Same as  python3 teamcamel.py --precheck
                                     once Python itself is there. Changes nothing.
    python3 teamcamel.py --install-hook
                                     one-time setup on a computer: adds a Claude
                                     SessionStart hook to settings.json (backed up
                                     first) so TeamCamel starts with Claude, and
                                     starts it now
    python3 teamcamel.py             collector + page at http://127.0.0.1:2263/
    python3 teamcamel.py --ensure    start that in the background unless it already runs
                                     (what the hook calls; silent)
    python3 teamcamel.py --check     report what TeamCamel finds on this computer
    python3 teamcamel.py --stop      stop it (it starts again with the next Claude session)
    python3 teamcamel.py --uninstall take this copy out of Claude's start-up and stop it;
                                     the folder itself stays for you to delete
    python3 teamcamel.py --no-serve  collector only (this node reports, nobody views here)
    python3 teamcamel.py --once      print this node's snapshot and exit
    python3 teamcamel.py --version

On Windows the command is usually `python` or `py` rather than `python3`.

Each node writes state/<node>.json next to this script. Keep this folder somewhere
that syncs between the computers (Dropbox, OneDrive, iCloud Drive, Syncthing ...)
and every node's page shows every node. No node talks to another directly, and the
page server binds to 127.0.0.1 only.

Where the status comes from (all local files, read-only):
  <claude>/sessions/<pid>.json     live sessions: busy / waiting / idle, and what a
                                   waiting session waits for (same data as
                                   `claude agents --json`)
  <desktop>/claude-code-sessions/*/*/local_*.json
                                   the desktop app's session list: title, folder,
                                   archived flag, last activity
  <claude>/projects/*/<id>.jsonl   transcript tail, to tell a finished turn from one
                                   that stopped part-way

  <codex>/state_*.sqlite, <codex>/sessions/**/rollout-*.jsonl
                                   Codex threads and their transcripts. Codex publishes
                                   no live status, so it is worked out from how the
                                   last turn of a transcript stands

  <claude>   ~/.claude, or $CLAUDE_CONFIG_DIR
  <codex>    ~/.codex, or $CODEX_HOME
  <desktop>  macOS    ~/Library/Application Support/Claude
             Windows  %APPDATA%\Claude, or for a Store (MSIX) install
                      %LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude
             Linux    ~/.config/Claude

Version history: CHANGELOG.txt
"""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import sqlite3
except ImportError:   # a Python built without it: Codex threads are then read from their files
    sqlite3 = None

__version__ = "2.1"

WINDOWS = sys.platform.startswith("win")
MACOS = sys.platform == "darwin"

HOME = Path.home()
CLAUDE = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
REGISTRY = CLAUDE / "sessions"
PROJECTS = CLAUDE / "projects"
CODEX = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")
SETTINGS = CLAUDE / "settings.json"
SELF = Path(__file__).resolve()
ROOT = SELF.parent
STATE = ROOT / "state"
WEB = ROOT / "web"

PORT = 2263                    # C-A-M-E on a phone keypad
COLLECT_EVERY = 15             # seconds between local reads
WRITE_MIN_GAP = 30             # a changed snapshot is written at most this often
HEARTBEAT = 300                # an unchanged snapshot is re-written this often
INTERRUPTED_ATTENTION_DAYS = 7  # an older interrupted session stops counting as "needs you"
CODEX_READ_DAYS = 8            # Codex threads idle longer than this are listed as completed, transcript unread
CODEX_READ_SECONDS = 2.0       # time spent per cycle on Codex transcripts not read yet; the rest waits its turn

CONNECTION_WORDS = ("connection", "socket", "network", "timed out", "timeout", "fetch failed",
                    "econnreset", "overloaded", "502", "503", "529")


def now_ms():
    return int(time.time() * 1000)


def stamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# --- where things are on this system -----------------------------------------

def desktop_roots():
    """Folders where the Claude desktop app may keep its data."""
    if MACOS:
        return [HOME / "Library" / "Application Support" / "Claude"]
    if WINDOWS:
        roots = []
        appdata, local = os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")
        if appdata:
            roots.append(Path(appdata) / "Claude")
        if local:
            # A Store (MSIX) install is redirected into its package folder.
            roots += sorted(Path(local, "Packages").glob("Claude_*/LocalCache/Roaming/Claude"))
        return roots
    return [Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "Claude"]


def log_path():
    if MACOS:
        return HOME / "Library" / "Logs" / "teamcamel.log"
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or HOME) / "TeamCamel" / "teamcamel.log"
    return Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local" / "state") / "teamcamel" / "teamcamel.log"


def node_name():
    try:
        name = (CLAUDE / "nodename").read_text(encoding="utf-8").strip()
        if name:
            return name
    except (OSError, ValueError):
        pass
    return host_name()


def host_name():
    if MACOS:
        try:
            out = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return platform.node() or socket.gethostname() or "this computer"


def os_label():
    if MACOS:
        return ("macOS " + platform.mac_ver()[0]).strip()
    if WINDOWS:
        return "Windows"
    name = "Linux"
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                name = line.split("=", 1)[1].strip().strip('"') or name
    except (OSError, ValueError):
        pass
    return name + (" (WSL)" if "microsoft" in platform.release().lower() else "")


def state_file(name):
    """This node's file in the shared folder; the name must be valid on every system."""
    safe = re.sub(r"[^\w .()'-]", "_", name).strip(" .") or "node"
    return STATE / (safe + ".json")


def ws_name(cwd):
    """Workspace label from a session folder: its last part, as in .../projects/abc -> 'abc'.

    A leading "wp-" or "ws-" is dropped (wp-abc -> 'abc'); a folder set aside with a
    "z_" in front keeps that marker (z_wp-abc -> 'z_abc'). Works on a path from any
    system, so C:\\work\\abc gives 'abc' too.
    """
    if not cwd:
        return "(no folder)"
    path = cwd.replace("\\", "/").split("/.claude/worktrees/")[0].rstrip("/")
    base = path.rsplit("/", 1)[-1] or path
    return re.sub(r"^(z_)?(wp|ws)[-_]", r"\1", base) or base


def read_json(path):
    # Bytes in, so the system's default text encoding (not UTF-8 on Windows) never matters.
    return json.loads(path.read_bytes())


# --- live sessions -----------------------------------------------------------

def registry_entries():
    entries = []
    for p in REGISTRY.glob("*.json"):
        try:
            d = read_json(p)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get("pid"), int) and d.get("sessionId"):
            entries.append(d)
    return entries


def live_registry():
    """Registry entries whose process is still the one that wrote them."""
    entries = registry_entries()
    if not entries:
        return []
    running = running_processes([e["pid"] for e in entries])
    return [e for e in entries if same_process(e, running)]


def running_processes(pids):
    """{pid: start token} for the pids running now.

    The token is what Claude Code records as the process start, so a recycled pid
    can be told from the original: the `ps lstart` text on macOS and Linux, the
    creation time (FILETIME) on Windows. None means running, start time unknown.
    """
    if WINDOWS:
        return _running_windows(pids)
    try:
        out = subprocess.run(["ps", "-o", "pid=,lstart=", "-p", ",".join(str(p) for p in pids)],
                             capture_output=True, text=True, timeout=5,
                             env=dict(os.environ, LC_ALL="C", TZ="UTC"))
        found = {}
        for line in out.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                found[int(parts[0])] = parts[1]
        if found or not out.stderr.strip():
            return found
    except (OSError, subprocess.SubprocessError):
        pass
    # No usable ps (BusyBox has no lstart): settle for "does the pid exist".
    return {pid: None for pid in pids if _pid_exists(pid)}


def _pid_exists(pid):
    try:
        os.kill(pid, 0)   # POSIX only: on Windows os.kill would end the process
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def _running_windows(pids):
    try:
        return _running_windows_api(pids)
    except Exception:
        pass
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return {}
    have = set()
    for line in out.stdout.splitlines():
        cols = line.split('","')
        if len(cols) > 1 and cols[1].strip('"').isdigit():
            have.add(int(cols[1].strip('"')))
    return {pid: None for pid in pids if pid in have}


def _running_windows_api(pids):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    found = {}
    for pid in pids:
        handle = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:   # STILL_ACTIVE
                continue
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            ok = k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                     ctypes.byref(kernel), ctypes.byref(user))
            found[pid] = ((created.dwHighDateTime << 32) | created.dwLowDateTime) if ok else None
        finally:
            k32.CloseHandle(handle)
    return found


def same_process(entry, running):
    """Is the running pid the very process that wrote this registry entry?"""
    pid = entry["pid"]
    if pid not in running:
        return False
    seen = running[pid]
    if seen is None:
        return True
    for recorded in (entry.get("procStartFt"), entry.get("procStart")):
        verdict = _same_start(recorded, seen)
        if verdict is not None:
            return verdict
    return True   # nothing comparable was recorded


def _same_start(recorded, seen):
    """True / False when the two start tokens are of the same kind, else None."""
    if recorded is None or recorded == "":
        return None
    a, b = _as_int(recorded), _as_int(seen)
    if a is not None and b is not None:
        return abs(a - b) <= 10_000_000   # FILETIME units: within one second
    if a is None and b is None:
        # `ps` pads a one-digit day with a second space; compare without the padding.
        return " ".join(str(recorded).split()) == " ".join(str(seen).split())
    return None


def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


# --- desktop app session list ------------------------------------------------

DESKTOP_KEYS = ("sessionId", "cliSessionId", "cwd", "originCwd", "title", "isArchived", "lastActivityAt")
_desktop_cache = {}


def desktop_sessions():
    out, seen, ids = [], set(), set()
    for root in desktop_roots():
        for p in (root / "claude-code-sessions").glob("*/*/local_*.json"):
            try:
                st = p.stat()
            except OSError:
                continue
            seen.add(p)
            key = (st.st_mtime_ns, st.st_size)
            cached = _desktop_cache.get(p)
            if not cached or cached[0] != key:
                try:
                    d = read_json(p)
                except (OSError, ValueError):
                    d = None
                if isinstance(d, dict):
                    cached = (key, {k: d.get(k) for k in DESKTOP_KEYS})
                    _desktop_cache[p] = cached
                # else caught mid-rewrite by the app: keep the last good read, if any
            if cached and cached[1].get("sessionId") not in ids:
                ids.add(cached[1].get("sessionId"))
                out.append(cached[1])
    for p in list(_desktop_cache):
        if p not in seen:
            del _desktop_cache[p]
    return out


# --- transcript tail ---------------------------------------------------------

_transcript_paths = {}
_verdict_cache = {}


def transcript_path(cli_id):
    if not cli_id:
        return None
    p = _transcript_paths.get(cli_id)
    if p and p.exists():
        return p
    for p in PROJECTS.glob("*/%s.jsonl" % cli_id):
        _transcript_paths[cli_id] = p
        return p
    return None


def last_turn_record(path, size):
    for span in (256 * 1024, 4 * 1024 * 1024):
        start = max(0, size - span)
        with open(path, "rb") as fh:
            fh.seek(start)
            lines = fh.read().split(b"\n")
        if start > 0:
            lines = lines[1:]
        for raw in reversed(lines):
            if b'"type":"assistant"' not in raw and b'"type":"user"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if d.get("type") in ("user", "assistant") and not d.get("isSidechain") and not d.get("isMeta"):
                return d
        if start == 0:
            break
    return None


def message_text(rec):
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def record_ms(rec):
    ts = rec.get("timestamp")
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except (AttributeError, ValueError):
        return None


def transcript_verdict(cli_id):
    """(state, detail, since_ms) from how the transcript ends, or None if unreadable."""
    path = transcript_path(cli_id)
    if not path:
        return None
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        cached = _verdict_cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
        rec = last_turn_record(path, st.st_size)
    except OSError:
        return None
    verdict = None
    if rec:
        text = message_text(rec)
        stop = (rec.get("message") or {}).get("stop_reason")
        if rec.get("isApiErrorMessage"):
            lost = any(w in text.lower() for w in CONNECTION_WORDS)
            verdict = ("interrupted", "connection interrupted" if lost else "stopped on an error")
        elif rec["type"] == "assistant" and stop in ("end_turn", "stop_sequence"):
            verdict = ("completed", "")
        elif rec["type"] == "user" and text.startswith("[Request interrupted by user"):
            verdict = ("interrupted", "interrupted (stopped by you)")
        else:
            # Ends on a tool call, a tool result, or an unanswered prompt.
            verdict = ("interrupted", "interrupted mid-task")
        verdict = verdict + (record_ms(rec),)
    _verdict_cache[path] = (key, verdict)
    return verdict


# --- Codex sessions ----------------------------------------------------------
#
# Codex (OpenAI) keeps a list of its threads in a small database and a transcript
# per thread. Unlike Claude Code it publishes no live status, so the state of a
# thread is worked out from how its transcript stands:
#   turn finished                              -> completed
#   turn stopped by the user                   -> interrupted
#   a command asked for wider permissions and
#   has no result yet, or Codex asked a question -> awaits input
#   turn open, nothing of the above            -> working
#   turn open, but Codex is not running        -> interrupted

_codex_threads = (None, [])
_codex_turns = {}


def codex_threads():
    """Codex threads that are not archived: id, title, folder, transcript path."""
    global _codex_threads
    dbs = sorted(CODEX.glob("state_*.sqlite"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
    if dbs and sqlite3:
        db = dbs[-1]
        try:
            key = tuple(os.stat(str(db) + ext).st_mtime_ns for ext in ("", "-wal") if os.path.exists(str(db) + ext))
            if _codex_threads[0] == (db, key):
                return _codex_threads[1]
            con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=1)   # read-only: never written to
            try:
                have = {row[1] for row in con.execute("pragma table_info(threads)")}
                want = [c for c in ("id", "name", "title", "cwd", "rollout_path", "archived",
                                    "updated_at_ms", "thread_source", "originator") if c in have]
                rows = [dict(zip(want, row)) for row in con.execute("select %s from threads" % ", ".join(want))]
            finally:
                con.close()
            if "id" in want:
                threads = [r for r in rows if r.get("id") and not r.get("archived")
                           and r.get("thread_source") in (None, "user")]
                _codex_threads = ((db, key), threads)
                return threads
        except (sqlite3.Error, OSError, ValueError):
            pass
    return codex_threads_from_files()


def codex_threads_from_files():
    """The same list without the database: from the transcripts themselves (newest 300)."""
    names = {}
    try:
        for line in (CODEX / "session_index.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
                names[d.get("id")] = d.get("thread_name")
            except ValueError:
                pass
    except OSError:
        pass
    found = []
    for p in CODEX.glob("sessions/*/*/*/rollout-*.jsonl"):
        try:
            found.append((p.stat().st_mtime, p))
        except OSError:
            pass
    threads = []
    for mtime, p in sorted(found, reverse=True)[:300]:
        try:
            with open(p, "rb") as fh:
                head = fh.read(262144).decode("utf-8", "replace")
        except OSError:
            continue
        tid = re.search(r'"payload":\{.*?"id":"([0-9a-f-]{20,})"', head) or re.search(r"rollout-.*?-([0-9a-f]{8}-[0-9a-f-]{20,})", p.name)
        cwd = re.search(r'"cwd":("(?:[^"\\]|\\.)*")', head)
        if not tid:
            continue
        try:
            folder = json.loads(cwd.group(1)) if cwd else None
        except ValueError:
            folder = None
        made_by = re.search(r'"originator":"([^"]*)"', head)
        threads.append({"id": tid.group(1), "name": names.get(tid.group(1)), "cwd": folder, "rollout_path": str(p),
                        "updated_at_ms": int(mtime * 1000), "originator": made_by.group(1) if made_by else None})
    return threads


# A transcript line begins  {"timestamp":"...","ordinal":N,"type":"event_msg","payload":{"type":"task_started",
# Lines can be very long (a tool's whole output), so they are read by pattern, not parsed as a whole.
_CODEX_HEAD = re.compile(rb'"timestamp":"([^"]+)"[^{}]{0,60}?"type":"([a-z_]+)","payload":\{"type":"([a-z_]+)"')
_CODEX_CALL = re.compile(rb'"call_id":"([^"]+)"')


def codex_turn(path, read=True):
    """How the last turn of a Codex transcript stands; None if unreadable, or not yet read and read is False."""
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
        cached = _codex_turns.get(path)
        if cached and cached[0] == key:
            return cached[1]
        if not read:
            return None
        start = max(0, st.st_size - 768 * 1024)   # the end of the file is enough to see how the turn stands
        with open(path, "rb") as fh:
            fh.seek(start)
            lines = fh.read().split(b"\n")
    except OSError:
        return None
    if start > 0:
        lines = lines[1:]
    turn = {"started": None, "complete": None, "aborted": None, "error": None, "pending": None,
            "last": int(st.st_mtime * 1000)}
    calls, answered = [], set()
    for raw in lines:
        head = _CODEX_HEAD.search(raw[:400])
        if head:
            when, family, kind = _iso_ms(head.group(1).decode("ascii", "replace")), head.group(2), head.group(3)
        elif raw.strip() and len(raw) < 200000:
            try:   # a line in another layout: read it the slow way
                d = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            when = record_ms(d)
            family, kind = (str(x).encode() for x in _codex_kind(d))
        else:
            continue
        if turn["started"] is None and start > 0:
            turn["started"] = when          # the turn began before the part that was read
        if family == b"event_msg":
            if kind == b"task_started":
                turn.update(started=when or turn["last"], complete=None, aborted=None, error=None)
                calls, answered = [], set()
            elif kind in (b"task_complete", b"turn_complete"):
                turn["complete"] = when or turn["last"]
            elif kind == b"turn_aborted":
                turn["aborted"] = when or turn["last"]
            elif kind in (b"error", b"stream_error"):
                turn["error"] = raw[:600].decode("utf-8", "replace")
        elif family == b"response_item" and kind.endswith(b"_call_output"):
            found = _CODEX_CALL.search(raw)
            if found:
                answered.add(found.group(1))
        elif family == b"response_item" and kind.endswith(b"_call"):
            found = _CODEX_CALL.search(raw)
            calls.append((found.group(1) if found else None, when, b"require_escalated" in raw, b"request_user_input" in raw))
    for call_id, when, wider, asks in reversed(calls):
        if call_id not in answered:
            turn["pending"] = (when or turn["last"], wider, asks)
            break
    _codex_turns[path] = (key, turn)
    return turn


def _iso_ms(text):
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _codex_kind(record):
    payload = record.get("payload")
    return record.get("type") or "", (payload.get("type") or "") if isinstance(payload, dict) else ""


def codex_running():
    """Is any Codex program running here: the app's engine or the command-line one?"""
    try:
        if WINDOWS:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20).stdout
            return any(line.lower().startswith('"codex') for line in out.splitlines())
        out = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, timeout=5).stdout
        return any(os.path.basename(line.strip()).lower().split(" ")[0] == "codex" for line in out.splitlines())
    except (OSError, subprocess.SubprocessError):
        return True   # cannot tell: then nothing is called interrupted on that ground


def classify_codex(turn, updated, running):
    """state / detail / since / attention / live for one Codex thread. `running` is asked only if needed."""
    if not turn or turn["started"] is None:
        return "completed", "", updated, False, False
    recent = lambda since: bool(since and now_ms() - since < INTERRUPTED_ATTENTION_DAYS * 86400 * 1000)
    if turn["aborted"]:
        return "interrupted", "interrupted (stopped by you)", turn["aborted"], recent(turn["aborted"]), False
    if turn["complete"]:
        return "completed", "", turn["complete"], False, False
    idle = now_ms() - turn["last"]
    if idle > 5 * 60000 and not running():
        return "interrupted", "interrupted mid-task", turn["last"], recent(turn["last"]), False
    if turn["pending"]:
        since, wider, asks = turn["pending"]
        if wider:
            return "input", "approval", since, True, True
        if asks:
            return "input", "input needed", since, True, True
        return "working", "", turn["started"], False, True
    if turn["error"] and idle > 60000:
        lost = any(w in turn["error"].lower() for w in CONNECTION_WORDS)
        return "interrupted", "connection interrupted" if lost else "stopped on an error", turn["last"], True, False
    if idle > 10 * 60000:
        return "interrupted", "interrupted mid-task", turn["last"], recent(turn["last"]), False
    return "working", "", turn["started"], False, True


def codex_sessions():
    sessions, state = [], {}
    deadline = time.time() + CODEX_READ_SECONDS
    horizon = now_ms() - CODEX_READ_DAYS * 86400 * 1000

    def running():
        if "codex" not in state:
            state["codex"] = codex_running()
        return state["codex"]

    # Newest first, so that whatever the time allowance covers is what matters most.
    for t in sorted(codex_threads(), key=lambda t: t.get("updated_at_ms") or 0, reverse=True):
        path = t.get("rollout_path")
        if not path or not os.path.exists(path):
            found = list(CODEX.glob("sessions/*/*/*/rollout-*-%s.jsonl" % t["id"]))
            path = str(found[0]) if found else None
        turn = None
        if path and (t.get("updated_at_ms") or now_ms()) >= horizon:
            # A long Codex history is read a little each cycle; until its turn comes, a thread counts as completed.
            turn = codex_turn(path, read=time.time() < deadline)
        updated = t.get("updated_at_ms") or (turn or {}).get("last")
        status, detail, since, attention, live = classify_codex(turn, updated, running)
        title = " ".join(str(t.get("name") or t.get("title") or "(untitled)").split())
        sessions.append({
            "id": "codex-%s" % t["id"],
            "title": title if len(title) <= 80 else title[:77] + "...",
            "ws": ws_name(t.get("cwd")),
            "state": status,
            "detail": detail,
            "since": since,
            "attention": attention,
            "live": live,
            # The Codex desktop app opens a thread from this address; a terminal-only Codex has nothing to open it.
            "link": "codex://threads/%s" % t["id"] if "desktop" in str(t.get("originator") or "").lower() else None,
            "app": "codex",
        })
    return sessions


# --- work still running behind a finished turn ---------------------------------
#
# Claude reports a session idle as soon as its turn ends, even while work it started goes on:
# a command or a helper agent sent to the background, or a program that detached (a render).
# Two signals tell that work apart from a finished session:
#   - the transcript records each background command and helper agent when it starts, and the
#     notice Claude receives when it ends;
#   - every command a session runs carries CLAUDE_PID and CLAUDE_CODE_SESSION_ID in its
#     environment, and so does any program it starts, even one that detaches.
# Such a program counts only while it is busy, so an idle leftover (a helper service) does not.

BUSY_CPU = 10.0                # % of one core above which a program a session started counts as working
AGENT_HOURS = 12               # a background helper agent with no end notice stops counting after this

_ENV_PID = re.compile(r"(?:^|\s)CLAUDE_PID=(\d+)(?=\s|$)")
_ENV_SESSION = re.compile(r"(?:^|\s)CLAUDE_CODE_SESSION_ID=([0-9A-Za-z-]+)(?=\s|$)")
_TASK_ID = re.compile(r"<task-id>([^<]+)</task-id>")
_TASK_STATUS = re.compile(r"<status>([^<]+)</status>")
_task_cache = {}


def process_table():
    """[(pid, ppid, text)] for this user's processes, text being the command line followed by the
    environment where the system shows it (macOS hides it for its own programs, such as /bin/zsh).
    None where processes cannot be read."""
    if WINDOWS:
        return None
    rows = []
    if MACOS or not Path("/proc").is_dir():
        try:
            out = subprocess.run(["ps", "-Eww", "-U", str(os.getuid()), "-o", "pid=,ppid=,command="],
                                 capture_output=True, text=True, errors="replace", timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                rows.append((int(parts[0]), int(parts[1]), parts[2]))
        return rows
    uid = os.getuid()
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            if d.stat().st_uid != uid:
                continue
            ppid = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
            words = (d / "cmdline").read_bytes().split(b"\0") + (d / "environ").read_bytes().split(b"\0")
        except (OSError, ValueError, IndexError):
            continue
        rows.append((int(d.name), ppid, " ".join(w.decode("utf-8", "replace") for w in words if w)))
    return rows


def marked_processes(table):
    """Programs started by Claude sessions' commands, running now:
    [{"pid", "claude", "session", "cpu", "program", "since"}]."""
    me = os.getpid()
    marks = {}
    for pid, ppid, text in table or []:
        pid_m = _ENV_PID.search(text)
        # Not TeamCamel itself: when a Claude hook started it, it carries the marks too.
        if pid_m and pid != me and ppid != me:
            sess_m = _ENV_SESSION.search(text)
            marks[pid] = (int(pid_m.group(1)), sess_m.group(1) if sess_m else None)
    if not marks:
        return []
    try:
        out = subprocess.run(["ps", "-o", "pid=,pcpu=,lstart=,comm=", "-p", ",".join(str(p) for p in marks)],
                             capture_output=True, text=True, errors="replace", timeout=10,
                             env=dict(os.environ, LC_ALL="C", TZ="UTC")).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8 or not parts[0].isdigit() or int(parts[0]) not in marks:
            continue
        try:
            cpu = float(parts[1])
            since = int(datetime.strptime(" ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y")
                        .replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
        claude, session = marks[int(parts[0])]
        found.append({"pid": int(parts[0]), "claude": claude, "session": session, "cpu": cpu,
                      "program": os.path.basename(parts[7].strip()) or "program", "since": since})
    return found


def open_tasks(cli_id, since_ms):
    """[(kind, start_ms)] for the background commands and helper agents of a session that started
    after since_ms (the running process's start) and have no end notice yet, read from its
    transcript. Only new lines are read each time."""
    path = transcript_path(cli_id)
    if not path:
        return []
    try:
        size = path.stat().st_size
    except OSError:
        return []
    cache = _task_cache.get(path)
    if not cache or size < cache["offset"]:
        cache = _task_cache[path] = {"offset": 0, "open": {}}
    if size > cache["offset"]:
        try:
            with open(path, "rb") as fh:
                fh.seek(cache["offset"])
                chunk = fh.read(size - cache["offset"])
        except OSError:
            return []
        end = chunk.rfind(b"\n")
        if end >= 0:
            cache["offset"] += end + 1
            for raw in chunk[:end].split(b"\n"):
                _task_record(raw, cache["open"])
    now = now_ms()
    out = []
    for kind, start in cache["open"].values():
        if since_ms and start and start < since_ms - 1000:
            continue        # started by an earlier run of the session, and ended with it
        if kind == "agent" and start and now - start > AGENT_HOURS * 3600 * 1000:
            continue
        out.append((kind, start))
    return out


def _task_record(raw, tasks):
    """Note one transcript line's background start or end in tasks {id: (kind, start_ms)}."""
    if not (b"backgroundTaskId" in raw or b"async_launched" in raw or b'"task_id"' in raw
            or b"<task-notification>" in raw):
        return
    try:
        d = json.loads(raw)
    except ValueError:
        return
    result = d.get("toolUseResult")
    if isinstance(result, dict):
        if result.get("backgroundTaskId"):
            tasks[str(result["backgroundTaskId"])] = ("command", record_ms(d))
        elif result.get("status") == "async_launched" and result.get("agentId"):
            tasks[str(result["agentId"])] = ("agent", record_ms(d))
        elif result.get("task_id") and "stopped" in str(result.get("message", "")).lower():
            tasks.pop(str(result["task_id"]), None)          # stopped on purpose
    if d.get("type") == "queue-operation":
        text = d.get("content")
    elif d.get("type") == "attachment":
        text = (d.get("attachment") or {}).get("prompt")
    else:
        text = (d.get("message") or {}).get("content") if d.get("type") == "user" else None
    if isinstance(text, str) and text.lstrip().startswith("<task-notification>"):
        tid, status = _TASK_ID.search(text), _TASK_STATUS.search(text)
        if tid and status and status.group(1).strip() in ("completed", "failed", "killed"):
            tasks.pop(tid.group(1).strip(), None)


def background_work(registry):
    """{Claude session id: (detail, since_ms)} for sessions with work still running."""
    table = process_table()
    procs = marked_processes(table)
    by_pid = {e["pid"]: e for e in registry}
    labels, first = {}, {}

    def add(sid, label, since):
        labels.setdefault(sid, []).append(label)
        if since and (sid not in first or since < first[sid]):
            first[sid] = since

    for p in procs or []:
        if p["cpu"] >= BUSY_CPU:
            sid = (by_pid.get(p["claude"]) or {}).get("sessionId") or p["session"]
            if sid:
                add(sid, p["program"], p["since"])
    # A command Claude runs goes through a shell started from its shell snapshot.
    shells = {ppid for _, ppid, text in table or [] if "shell-snapshots" in text}
    for e in registry:
        if e.get("status") in ("busy", "waiting") or not e.get("sessionId"):
            continue
        for kind, start in open_tasks(e["sessionId"], _as_int(e.get("startedAt"))):
            if kind == "command" and table is not None and e["pid"] not in shells:
                continue    # no shell of this session is left: the command is over, notice or not
            add(e["sessionId"], kind, start)
    return {sid: (work_detail(found), first.get(sid)) for sid, found in labels.items()}


def work_detail(labels):
    """'in background (blender)', 'in background (2 agents, command)': what is still running."""
    programs = sorted({l for l in labels if l not in ("command", "agent")})
    words = list(programs)
    for kind in ("agent", "command"):
        n = labels.count(kind)
        if n and not (kind == "command" and programs):   # a busy program is the command's own work
            words.append(kind if n == 1 else "%d %ss" % (n, kind))
    return "in background (%s)" % ", ".join(words)


# --- versions of Claude Code and Codex ------------------------------------------
#
# Each computer reports the versions it runs; the page colors them against the newest.

VERSION_EVERY = 600            # seconds between re-reading installed versions
_version_cache = {}
_last_registry = []


def version_key(text):
    """(2, 1, 304) from '2.1.304' or 'codex-cli 0.162.0-alpha.17.2'; None without a version number."""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(text or ""))
    return tuple(int(x or 0) for x in m.groups()) if m else None


def newest_version(texts):
    found = [(version_key(t), str(t)) for t in texts if version_key(t)]
    return max(found)[1] if found else None


def command_version(exe):
    """'2.1.292' from running `exe --version`; None if it does not answer."""
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.]+)?", out.stdout)
    return m.group(0) if m else None


def _cached(name, read):
    hit = _version_cache.get(name)
    if hit and time.time() - hit[0] < VERSION_EVERY:
        return hit[1]
    value = read()
    _version_cache[name] = (time.time(), value)
    return value


def claude_code_version(registry):
    """The Claude Code version this computer runs: the newest one its running sessions use,
    else the newest one installed (the desktop app's copies, the claude command)."""
    running = newest_version(e.get("version") for e in registry)
    if running:
        return running

    def installed():
        found = [p.name for root in desktop_roots() if (root / "claude-code").is_dir()
                 for p in (root / "claude-code").iterdir()]
        exe = shutil.which("claude")
        if exe:
            found.append(command_version(exe))
        return newest_version(found)
    return _cached("claude", installed)


CODEX_APP_CLI = ("Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",)


def codex_version():
    """The newest Codex on this computer: the codex command, the copy inside the Codex desktop app,
    or the version its newest threads were run with."""
    def read():
        found = []
        exe = shutil.which("codex")
        if exe:
            found.append(command_version(exe))
        if MACOS:
            for rel in CODEX_APP_CLI:
                for base in (Path("/"), HOME):
                    if (base / rel).exists():
                        found.append(command_version(str(base / rel)))
        try:
            threads = codex_threads()
        except Exception:
            threads = []
        for t in sorted(threads, key=lambda t: t.get("updated_at_ms") or 0, reverse=True)[:5]:
            try:
                with open(str(t.get("rollout_path") or ""), "rb") as fh:
                    m = re.search(rb'"cli_version":"([^"]+)"', fh.read(65536))
            except OSError:
                continue
            if m:
                found.append(m.group(1).decode("ascii", "replace"))
        return newest_version(found)
    return _cached("codex", read)


def app_versions():
    apps = {"claude": claude_code_version(_last_registry), "codex": codex_version()}
    return {k: v for k, v in apps.items() if v}


# --- settings ----------------------------------------------------------------------
#
# Kept in the shared folder: one file with the settings for all computers, and one per
# computer that chose its own. Each computer's page receives both and uses its own computer's
# when there is one; every browser on the computer therefore sees the same settings.

SETTINGS_SHARED = "teamcamel.settings"      # in STATE; not *.json, so never taken for a computer's report
SETTINGS_MAX = 32 * 1024
_HEX = re.compile(r"#[0-9A-Fa-f]{6}")
_settings_cache = {}


def clean_settings(d):
    """Only the settings the page knows, each of the right kind; anything else is dropped."""
    if not isinstance(d, dict):
        return {}
    out = {}
    for key in ("order", "hidden"):
        v = d.get(key)
        if isinstance(v, list):
            out[key] = [x[:200] for x in v if isinstance(x, str)][:100]
    for key, names in (("apps", ("claude", "codex")), ("systems", ("macOS", "Windows", "Linux")),
                       ("notify", ("popup", "sound", "title"))):
        v = d.get(key)
        if isinstance(v, dict):
            out[key] = {n: v[n] for n in names if isinstance(v.get(n), bool)}
    for key in ("versions", "online"):
        if isinstance(d.get(key), bool):
            out[key] = d[key]
    v = d.get("colors")
    if isinstance(v, dict):
        out["colors"] = {n: v[n] for n in ("bg", "attn", "work", "muted")
                         if isinstance(v.get(n), str) and _HEX.fullmatch(v[n])}
    return out


def settings_path(name=None):
    """The file of the settings for all computers, or (with a name) that computer's own."""
    if name is None:
        return STATE / SETTINGS_SHARED
    return STATE / (SETTINGS_SHARED + "." + (re.sub(r"[^\w.-]", "_", name).strip(".") or "node"))


def read_settings(path):
    """The settings in a file, or None."""
    try:
        key = path.stat().st_mtime_ns
    except OSError:
        return None
    hit = _settings_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        value = clean_settings(read_json(path))
    except (OSError, ValueError):
        return hit[1] if hit else None      # caught half-synced: keep the last good copy
    _settings_cache[path] = (key, value)
    return value


def shared_settings():
    """The settings chosen for all computers, or None."""
    return read_settings(settings_path())


def own_settings(name):
    """The settings a computer chose for itself, or None."""
    return read_settings(settings_path(name))


def save_settings(d, name=None):
    """Write the settings for all computers, or (with a name) that computer's own."""
    STATE.mkdir(exist_ok=True)
    path = settings_path(name)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(clean_settings(d), indent=1), encoding="utf-8")
    os.replace(str(tmp), str(path))


def drop_own_settings(name):
    try:
        settings_path(name).unlink()
    except OSError:
        pass


# --- snapshot ----------------------------------------------------------------

def classify(live, cli_id, last_activity, background=None):
    """state / detail / since / attention for one session."""
    status = (live or {}).get("status")
    if status == "waiting":
        return "input", live.get("waitingFor") or "input needed", live.get("statusUpdatedAt"), True
    if status == "busy":
        return "working", "", live.get("statusUpdatedAt"), False
    verdict = transcript_verdict(cli_id)
    if not verdict:
        state, detail, since = "completed", "", (live or {}).get("statusUpdatedAt") or last_activity
    else:
        state, detail, since = verdict
        since = since or last_activity
    if state == "completed" and background and cli_id in background:
        detail, since_work = background[cli_id]
        return "working", detail, since_work or since, False
    attention = False
    if state == "interrupted":
        recent = since and now_ms() - since < INTERRUPTED_ATTENTION_DAYS * 86400 * 1000
        attention = bool(live) or bool(recent)
    return state, detail, since, attention


def collect():
    global _last_registry
    registry = _last_registry = live_registry()
    by_host = {e["hostSessionId"]: e for e in registry if e.get("hostSessionId")}
    by_cli = {e["sessionId"]: e for e in registry if e.get("sessionId")}
    sessions, matched = [], set()
    try:
        background = background_work(registry)
    except Exception as err:   # never lose the session list over it
        print("teamcamel: background work not read: %r" % err, file=sys.stderr)
        background = {}

    for d in desktop_sessions():
        if d.get("isArchived") or not d.get("sessionId"):
            continue
        live = by_host.get(d["sessionId"]) or by_cli.get(d.get("cliSessionId"))
        if live:
            matched.add(live["pid"])
        cwd = d.get("originCwd") or d.get("cwd") or (live or {}).get("cwd")
        state, detail, since, attention = classify(live, d.get("cliSessionId"), d.get("lastActivityAt"), background)
        sessions.append({
            "id": d["sessionId"],
            "title": d.get("title") or (live or {}).get("name") or "(untitled)",
            "ws": ws_name(cwd),
            "state": state,
            "detail": detail,
            "since": since,
            "attention": attention,
            "live": bool(live),
            "link": "claude://claude.ai/epitaxy/%s" % d["sessionId"],
        })

    # Sessions started outside the desktop app (a terminal `claude`).
    for e in registry:
        if e["pid"] in matched:
            continue
        state, detail, since, attention = classify(e, e.get("sessionId"), e.get("updatedAt"), background)
        sessions.append({
            "id": e.get("sessionId") or "pid-%d" % e["pid"],
            "title": e.get("name") or "(terminal session)",
            "ws": ws_name(e.get("cwd")),
            "state": state,
            "detail": detail,
            "since": since,
            "attention": attention,
            "live": True,
            "link": None,
        })

    try:
        sessions += codex_sessions()
    except Exception as err:   # Codex's files are not ours to rely on: never lose the Claude sessions over them
        print("teamcamel: Codex sessions skipped: %r" % err, file=sys.stderr)

    sessions.sort(key=lambda s: s["id"])
    return sessions


def code_stamp():
    try:
        st = SELF.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def page_stamp():
    """Changes whenever web/index.html does; an open page reloads itself on a new value."""
    try:
        return str((WEB / "index.html").stat().st_mtime_ns)
    except OSError:
        return ""


class Node:
    def __init__(self):
        self.name = node_name()
        self.host = host_name()
        self.os = os_label()
        self.lock = threading.Lock()
        self.snapshot = self.build()
        self.written_hash = None
        self.written_at = 0.0
        self.code = code_stamp()
        self.code_pending = None
        self.others = {}

    def reload_if_updated(self):
        """Restart on a new teamcamel.py (an edit made on another node and synced in).

        The new file must sit unchanged for one full tick and compile, so a
        half-synced copy is never run; until then the old code keeps serving.
        """
        seen = code_stamp()
        if seen is None or seen == self.code:
            self.code_pending = None
            return
        if seen != self.code_pending:
            self.code_pending = seen
            return
        try:
            compile(SELF.read_bytes(), str(SELF), "exec")
        except (OSError, SyntaxError, ValueError):
            return
        print("%s teamcamel.py changed; restarting" % stamp(), flush=True)
        restart()

    def build(self):
        sessions = collect()
        try:
            apps = app_versions()
        except Exception as err:   # never lose the session list over it
            print("teamcamel: versions not read: %r" % err, file=sys.stderr)
            apps = {}
        return {"schema": 1, "node": self.name, "host": self.host, "os": self.os,
                "version": __version__, "generatedAt": now_ms(), "sessions": sessions, "apps": apps}

    def tick(self):
        snap = self.build()
        with self.lock:
            self.snapshot = snap
        digest = hashlib.sha1(json.dumps([snap["sessions"], snap.get("apps")], sort_keys=True).encode()).hexdigest()
        age = time.time() - self.written_at
        if (digest != self.written_hash and age >= WRITE_MIN_GAP) or age >= HEARTBEAT:
            try:
                STATE.mkdir(exist_ok=True)
                state_file(self.name).write_text(json.dumps(snap, indent=1), encoding="utf-8")
                self.written_hash, self.written_at = digest, time.time()
            except OSError as err:
                print("teamcamel: cannot write state file: %s" % err, file=sys.stderr)

    def run(self, interval):
        while True:
            try:
                self.tick()
            except Exception as err:  # keep the collector alive across a bad read
                print("teamcamel: collect failed: %r" % err, file=sys.stderr)
            self.reload_if_updated()
            time.sleep(interval)

    def other_node(self, path):
        """Another node's snapshot; a file caught mid-sync keeps its last good copy."""
        cached = self.others.get(path)
        try:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_size)
            if cached and cached[0] == key:
                return cached[1]
            d = read_json(path)
        except (OSError, ValueError):
            return cached[1] if cached else None
        if not (isinstance(d, dict) and isinstance(d.get("sessions"), list) and d.get("node")):
            d = None
        self.others[path] = (key, d)
        return d

    def merged(self):
        with self.lock:
            mine = self.snapshot
        others = [d for d in (self.other_node(p) for p in sorted(STATE.glob("*.json")))
                  if d and d["node"] != self.name]
        others.sort(key=lambda d: str(d["node"]).lower())
        return {"now": now_ms(), "self": self.name, "version": __version__, "pollSec": COLLECT_EVERY,
                "heartbeatSec": HEARTBEAT, "page": page_stamp(), "pid": os.getpid(),
                # settingsScopes: this TeamCamel keeps "mine" and "shared" settings apart (2.1); a page
                # newer than a TeamCamel that has not restarted yet must not save to an older one
                "settingsScopes": True, "settings": shared_settings(), "mySettings": own_settings(self.name),
                "nodes": [mine] + others}


# --- page server -------------------------------------------------------------

def make_handler(node):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                return self.reply(403, "text/plain", b"local only")
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                try:
                    return self.reply(200, "text/html; charset=utf-8", (WEB / "index.html").read_bytes())
                except OSError:
                    return self.reply(500, "text/plain", b"web/index.html missing")
            if path == "/api/state":
                return self.reply(200, "application/json", json.dumps(node.merged()).encode())
            self.reply(404, "text/plain", b"not found")

        def do_POST(self):
            # Only this computer's own page may change the shared settings: a page from any web site
            # could send a request here, so it must come with the right Host and Origin, and with a
            # header a foreign page cannot add without asking first (which this server never allows).
            host = (self.headers.get("Host") or "").split(":")[0]
            origin = self.headers.get("Origin")
            local = ("http://127.0.0.1:%d" % self.server.server_port, "http://localhost:%d" % self.server.server_port)
            if (host not in ("127.0.0.1", "localhost") or (origin is not None and origin not in local)
                    or self.headers.get("X-TeamCamel") != "1"):
                return self.reply(403, "text/plain", b"local page only")
            if self.path.split("?")[0] != "/api/settings":
                return self.reply(404, "text/plain", b"not found")
            try:
                size = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                size = -1
            if not 0 < size <= SETTINGS_MAX:
                return self.reply(413, "text/plain", b"too large")
            try:
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("not an object")
                # {"scope": "mine" | "shared", "settings": {...}}; a bare object is for all computers (2.0)
                where, values = (body.get("scope"), body.get("settings")) if "scope" in body else ("shared", body)
                if where == "mine":
                    save_settings(values, node.name)
                elif where == "shared":
                    save_settings(values)
                    drop_own_settings(node.name)      # this computer now follows the settings for all
                else:
                    raise ValueError("unknown scope")
            except ValueError:
                return self.reply(400, "text/plain", b"not settings")
            except OSError as err:
                return self.reply(500, "text/plain", str(err).encode())
            self.reply(200, "application/json", json.dumps({"settings": shared_settings(),
                                                             "mySettings": own_settings(node.name)}).encode())

        def reply(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


class PageServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let a second copy share the port, so the port is
    # taken exclusively there instead.
    allow_reuse_address = not WINDOWS

    def server_bind(self):
        if WINDOWS:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)   # not HTTPServer's: it does a reverse-DNS lookup
        self.server_name, self.server_port = self.server_address[:2]


def open_server(port, handler):
    # After a restart on Windows the previous copy may hold the port for a moment.
    tries = 20 if WINDOWS else 1
    for attempt in range(tries):
        try:
            return PageServer(("127.0.0.1", port), handler)
        except OSError:
            if attempt + 1 == tries:
                raise
            time.sleep(0.5)


def monitor_info(port):
    """What the TeamCamel page server on this port says about itself, or None if there is none."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:%d/api/state" % port, timeout=2) as resp:
            info = json.load(resp)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and "nodes" in info else None


def is_running(port):
    """True if a TeamCamel page server already answers on this port."""
    return monitor_info(port) is not None


def stop(port):
    """Stop the monitor running on this computer. True when none is left running."""
    info = monitor_info(port)
    if info is None:
        print("TeamCamel is not running.")
        return True
    pid = info.get("pid")
    if not isinstance(pid, int):
        print("TeamCamel is running, but as an older version that cannot be stopped this way.")
        return False
    try:
        os.kill(pid, signal.SIGTERM)   # on Windows this ends the process outright
    except OSError as err:
        print("Could not stop TeamCamel (process %d): %s" % (pid, err))
        return False
    for _ in range(25):
        if not is_running(port):
            print("TeamCamel stopped.")
            return True
        time.sleep(0.2)
    print("TeamCamel did not stop (process %d)." % pid)
    return False


def spawn_background(args):
    """Start a detached copy of this program that outlives whatever started it."""
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(SELF)] + args
    with open(log, "ab") as out:
        common = dict(stdin=subprocess.DEVNULL, stdout=out, stderr=out, cwd=str(HOME))
        if not WINDOWS:
            subprocess.Popen(cmd, start_new_session=True, **common)
            return
        detached = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            # Also leave the starter's job, so ending that job does not end the monitor.
            subprocess.Popen(cmd, creationflags=detached | 0x01000000, **common)
        except OSError:
            subprocess.Popen(cmd, creationflags=detached, **common)


def restart():
    if WINDOWS:
        # Windows cannot replace a running program in place: hand over to a fresh copy.
        spawn_background(sys.argv[1:])
        os._exit(0)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def ensure_running(port):
    """Start the collector and page in the background unless they already run.

    Called by the Claude SessionStart hook, so it prints nothing (a hook's stdout
    is fed to the model) and returns at once.
    """
    if not is_running(port):
        spawn_background(["--port", str(port)])


# --- one-time setup ----------------------------------------------------------

def hook_entry(port=PORT):
    """The settings.json hook that starts TeamCamel with each Claude session."""
    ensure = "--ensure" if port == PORT else "--ensure --port %d" % port
    if not WINDOWS:
        py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
        return {"type": "command", "command": '%s "%s" %s 2>/dev/null || true' % (py, SELF, ensure), "timeout": 15}
    # Windows runs a hook through Git Bash when it is installed and PowerShell otherwise.
    # A program name or space-free path followed by a quoted argument reads the same in both.
    exe, script = sys.executable.replace("\\", "/"), str(SELF).replace("\\", "/")
    launcher = exe if re.fullmatch(r"[\w./:-]+", exe) else next(
        (name for name in ("py", "python", "python3") if shutil.which(name)), None)
    if launcher:
        return {"type": "command", "command": '%s "%s" %s' % (launcher, script, ensure), "timeout": 15}
    return {"type": "command", "shell": "powershell",
            "command": '& "%s" "%s" %s' % (exe, script, ensure), "timeout": 15}


def our_hooks(data):
    """(SessionStart groups, the TeamCamel hooks among them) from parsed settings."""
    hooks = data.setdefault("hooks", {}) if isinstance(data, dict) else None
    groups = hooks.setdefault("SessionStart", []) if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        return None, []
    ours = [h for g in groups if isinstance(g, dict) for h in g.get("hooks", [])
            if isinstance(h, dict) and "teamcamel.py" in str(h.get("command", ""))]
    return groups, ours


def install_hook(settings=None, port=PORT):
    """Add the Claude SessionStart hook that starts TeamCamel on this computer.

    Idempotent; leaves every other setting and hook alone; backs the file up first.
    """
    settings = settings or SETTINGS
    entry = hook_entry(port)
    data = {}
    if settings.exists():
        try:
            data = read_json(settings) if settings.stat().st_size else {}
        except ValueError as err:
            sys.exit("teamcamel: %s is not valid JSON (%s). Nothing was changed." % (settings, err))
    groups, ours = our_hooks(data)
    if groups is None:
        sys.exit("teamcamel: unexpected layout in %s. Nothing was changed." % settings)
    if any(h.get("command") == entry["command"] for h in ours):
        print("Hook already present in %s. Nothing was changed." % settings)
        return
    if settings.exists():
        backup = CLAUDE / "backups" / ("settings.json.%s.bak" % time.strftime("%Y-%m-%d-%H%M%S"))
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(settings), str(backup))
        print("Backup saved: %s" % backup)
    if ours:
        # The project folder moved: point the existing hook at its new place.
        for h in ours:
            h.pop("shell", None)
            h.update(entry)
    else:
        groups.append({"hooks": [entry]})
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print("Hook added to %s. TeamCamel now starts with every Claude session on this computer." % settings)


def uninstall(port, settings=None):
    """Take this copy of TeamCamel out of Claude's start-up and stop it. The folder stays.

    Only a hook that points at this copy is removed; any other hook, including another
    copy of TeamCamel installed elsewhere, is left alone. The settings file is backed up first.
    """
    settings = settings or SETTINGS
    here = (str(SELF), str(SELF).replace("\\", "/"))
    removed = 0
    if settings.exists():
        try:
            data = read_json(settings) if settings.stat().st_size else {}
        except (OSError, ValueError) as err:
            sys.exit("teamcamel: %s cannot be read (%s). Nothing was changed." % (settings, err))
        hooks = data.get("hooks") if isinstance(data, dict) else None
        groups = hooks.get("SessionStart") if isinstance(hooks, dict) else None
        if isinstance(groups, list):
            kept = []
            for group in groups:
                inner = group.get("hooks") if isinstance(group, dict) else None
                if not isinstance(inner, list):
                    kept.append(group)
                    continue
                rest = [h for h in inner
                        if not (isinstance(h, dict) and any(p in str(h.get("command", "")) for p in here))]
                removed += len(inner) - len(rest)
                if len(rest) == len(inner):
                    kept.append(group)
                elif rest:
                    kept.append(dict(group, hooks=rest))
            if removed:
                backup = CLAUDE / "backups" / ("settings.json.%s.bak" % time.strftime("%Y-%m-%d-%H%M%S"))
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(settings), str(backup))
                if kept:
                    hooks["SessionStart"] = kept
                else:
                    del hooks["SessionStart"]
                if not hooks:
                    del data["hooks"]
                settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                print("Start hook removed from %s (backup: %s)." % (settings, backup))
    if not removed:
        print("No start hook for this copy of TeamCamel in %s." % settings)
    if stop(port):
        print("TeamCamel is off this computer's Claude start-up. You can delete the folder %s." % ROOT)


def check(port):
    """Say what TeamCamel finds on this computer: counts and places, never session names."""
    def line(label, text):
        print("  %-21s %s" % (label, text))

    print("TeamCamel %s on '%s'  (%s, Python %s)" % (__version__, node_name(), os_label(), platform.python_version()))
    line("Claude folder", "%s  %s" % (CLAUDE, "found" if CLAUDE.is_dir()
                                      else "NOT FOUND: has Claude Code been used by this user?"))
    line("Live-status files", "%d, of which %d belong to a session running now"
         % (len(registry_entries()), len(live_registry())))
    stores = [r / "claude-code-sessions" for r in desktop_roots() if (r / "claude-code-sessions").is_dir()]
    if stores:
        line("Desktop app sessions", "%d in %s" % (len(desktop_sessions()), "; ".join(str(s) for s in stores)))
    else:
        line("Desktop app sessions", "none found, so only terminal sessions will show; looked in %s"
             % ("; ".join(str(r) for r in desktop_roots()) or "(no location known)"))
    line("Transcripts", "%d" % sum(1 for _ in PROJECTS.glob("*/*.jsonl")))
    if CODEX.is_dir():
        try:
            line("Codex sessions", "%d in %s; Codex %s" % (len(codex_threads()), CODEX,
                                                          "is running" if codex_running() else "is not running"))
        except Exception as err:
            line("Codex sessions", "could not be read (%r)" % err)
    else:
        line("Codex sessions", "none (no %s folder)" % CODEX)
    try:
        STATE.mkdir(exist_ok=True)
        shared = "writable" if os.access(str(STATE), os.W_OK) else "NOT WRITABLE"
    except OSError as err:
        shared = "CANNOT CREATE (%s)" % err
    line("Shared folder", "%s  %s" % (STATE, shared))
    seen = []
    for p in sorted(STATE.glob("*.json")):
        try:
            d = read_json(p)
            seen.append("%s (%s, %d sessions, reported %d min ago)" % (
                d["node"], d.get("os") or "system not stated", len(d["sessions"]),
                (now_ms() - d["generatedAt"]) // 60000))
        except (OSError, ValueError, KeyError, TypeError):
            seen.append("%s (unreadable)" % p.name)
    line("Computers reporting", "; ".join(seen) or "none yet")
    collect()
    apps = app_versions()
    line("Versions", "Claude Code %s; Codex %s" % (apps.get("claude") or "not found", apps.get("codex") or "not found"))
    line("Monitor", "running at http://127.0.0.1:%d/" % port if is_running(port) else "not running")
    try:
        ours = our_hooks(read_json(SETTINGS))[1] if SETTINGS.exists() else []
    except (OSError, ValueError):
        ours = []
    want = hook_entry(port)["command"]
    line("Start hook", "installed" if any(h.get("command") == want for h in ours)
         else "installed, but for another location: run --install-hook" if ours
         else "not installed: run --install-hook")
    line("Log", str(log_path()))


def port_state(port):
    """'teamcamel', 'free' or 'busy' for the page's port on this computer."""
    if is_running(port):
        return "teamcamel"
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if not WINDOWS:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
        return "free"
    except OSError:
        return "busy"
    finally:
        probe.close()


def claude_version():
    """Version line of the `claude` command, or None when there is no such command."""
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        return (out.stdout.strip().splitlines() or ["found (version not reported)"])[0]
    except (OSError, subprocess.SubprocessError):
        return "found, but it did not answer"


SYNC_NAMES = ("dropbox", "onedrive", "icloud", "mobile documents", "cloudstorage", "google drive",
              "googledrive", "my drive", "syncthing", "nextcloud", "pcloud")


def precheck(port):
    """Before installing: is everything TeamCamel needs on this computer?

    Changes nothing. Prints one line per component and returns how many are missing.
    """
    problems = []

    def ok(text):
        print("  [  OK   ] " + text)

    def note(text):
        print("  [ note  ] " + text)

    def missing(text, fix):
        problems.append(text)
        print("  [MISSING] " + text)
        print("            -> " + fix)

    py = "python" if WINDOWS else "python3"
    print("TeamCamel %s precheck on '%s' (%s)" % (__version__, host_name(), os_label()))

    if sys.version_info >= (3, 9):
        ok("Python %s" % platform.python_version())
    else:
        missing("Python 3.9 or newer (this one is %s)" % platform.python_version(),
                "install a current Python from https://www.python.org/downloads/ and run this again")

    if (WEB / "index.html").is_file():
        ok("TeamCamel files: teamcamel.py and web/index.html, in %s" % ROOT)
    else:
        missing("the page file web/index.html next to teamcamel.py",
                "download the complete TeamCamel folder again: teamcamel.py and the web folder belong together")
    folder = STATE if STATE.is_dir() else ROOT
    if os.access(str(folder), os.W_OK):
        ok("TeamCamel folder is writable (each computer keeps its report in its state folder)")
    else:
        missing("permission to write in %s" % folder,
                "move the TeamCamel folder to a place you can write to, such as your home folder")

    if not CLAUDE.is_dir():
        missing("Claude Code (there is no %s folder, so it has never run for this user)" % CLAUDE,
                "install the Claude desktop app or Claude Code from https://claude.com/download, sign in, "
                "start one session, then run this again")
    else:
        ok("Claude Code folder: %s" % CLAUDE)
        entries = registry_entries()
        if not REGISTRY.is_dir():
            missing("live session status (there is no %s folder)" % REGISTRY,
                    "update Claude Code to a current version, start a session, then run this again")
        elif any("status" in e for e in entries):
            ok("live session status: %d session(s) reporting now" % len(live_registry()))
        elif entries:
            missing("live session status (this Claude Code version does not report it)",
                    "update Claude Code to a current version")
        else:
            note("no Claude session is running right now, so live status could not be confirmed; "
                 "start a session and run this again to confirm")
    cli = claude_version()
    if cli:
        ok("claude command: %s" % cli)
    else:
        note("no `claude` command on this computer's PATH; that is fine if you use the desktop app")
    if CODEX.is_dir():
        try:
            ok("Codex: %d session(s) found; they will show next to Claude's" % len(codex_threads()))
        except Exception:
            note("Codex folder found, but its sessions could not be read; Claude sessions are not affected")
    else:
        note("Codex not found; that is fine, it is optional")
    if any((r / "claude-code-sessions").is_dir() for r in desktop_roots()):
        ok("Claude desktop app: its session list is here (%d sessions)" % len(desktop_sessions()))
    else:
        note("Claude desktop app session list not found; sessions started from a terminal still show")

    hooked = False
    if not SETTINGS.exists():
        ok("Claude settings file: none yet; --install-hook will create it")
    else:
        try:
            data = read_json(SETTINGS) if SETTINGS.stat().st_size else {}
            hooked = any(h.get("command") == hook_entry(port)["command"] for h in our_hooks(data)[1])
            ok("Claude settings file is readable" + ("; the TeamCamel start hook is installed" if hooked else ""))
        except (OSError, ValueError):
            missing("a readable Claude settings file (%s is damaged or locked)" % SETTINGS,
                    "repair or restore that file; TeamCamel will not touch it while it cannot be read")

    use = port_state(port)
    if use == "teamcamel":
        ok("TeamCamel is already running at http://127.0.0.1:%d/" % port)
    elif use == "free":
        ok("port %d is free for the page" % port)
    else:
        missing("port %d for the page (another program is using it)" % port,
                "close that program, or pick another port:  %s teamcamel.py --install-hook --port %d" % (py, port + 1))

    if WINDOWS:
        try:
            exact = os.getpid() in _running_windows_api([os.getpid()])
        except Exception:
            exact = False
    else:
        exact = bool(running_processes([os.getpid()]).get(os.getpid()))
    if exact:
        ok("process check: start times available, so a reused process number is never mistaken for a session")
    else:
        note("process check: this system gives no process start times; a simpler check is used")

    if any(name in str(ROOT).lower() for name in SYNC_NAMES):
        note("the TeamCamel folder is in a synced location: other computers sharing it appear on the page")
    else:
        note("the TeamCamel folder is not in a recognized synced location. Fine for one computer; to see several "
             "on one page, keep this folder in Dropbox, OneDrive, iCloud Drive or similar on each of them")

    if problems:
        print("Result: %d item%s missing. Follow the lines marked -> above, then run this again."
              % (len(problems), "" if len(problems) == 1 else "s"))
    elif hooked and use == "teamcamel":
        print("Result: everything needed is here, and TeamCamel is installed and running.")
    else:
        print("Result: everything needed is here. To install:  %s teamcamel.py --install-hook" % py)
    return len(problems)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # names may not fit the console's encoding
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="TeamCamel session monitor")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--interval", type=int, default=COLLECT_EVERY, help="seconds between local reads")
    ap.add_argument("--ensure", action="store_true", help="start in the background unless already running")
    ap.add_argument("--install-hook", action="store_true",
                    help="one-time setup: start TeamCamel with every Claude session, and start it now")
    ap.add_argument("--precheck", action="store_true",
                    help="before installing: say whether anything TeamCamel needs is missing")
    ap.add_argument("--check", action="store_true", help="report what TeamCamel finds on this computer")
    ap.add_argument("--stop", action="store_true", help="stop the running monitor (it starts again with Claude)")
    ap.add_argument("--uninstall", action="store_true",
                    help="take this copy out of Claude's start-up and stop it; the folder stays")
    ap.add_argument("--no-serve", action="store_true", help="collect and report only; no page")
    ap.add_argument("--once", action="store_true", help="print this node's snapshot and exit")
    ap.add_argument("--version", action="version", version="TeamCamel " + __version__)
    args = ap.parse_args()

    if args.ensure:
        try:
            ensure_running(args.port)
        except Exception:
            pass   # a monitor that cannot start must never get in the way of a Claude session
        return
    if args.precheck:
        sys.exit(1 if precheck(args.port) else 0)
    if args.install_hook:
        install_hook(port=args.port)
        ensure_running(args.port)
        print("TeamCamel page: http://127.0.0.1:%d/" % args.port)
        return
    if args.check:
        check(args.port)
        return
    if args.stop:
        sys.exit(0 if stop(args.port) else 1)
    if args.uninstall:
        uninstall(args.port)
        return

    node = Node()
    if args.once:
        print(json.dumps(node.snapshot, indent=1))
        return
    if args.no_serve:
        node.run(args.interval)
        return

    try:
        server = open_server(args.port, make_handler(node))
    except OSError as err:
        sys.exit("teamcamel: cannot listen on 127.0.0.1:%d (%s). Is it already running?" % (args.port, err))
    threading.Thread(target=node.run, args=(args.interval,), daemon=True).start()
    print("%s TeamCamel %s on node '%s' (%s): http://127.0.0.1:%d/ (pid %d)"
          % (stamp(), __version__, node.name, node.os, args.port, os.getpid()), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
