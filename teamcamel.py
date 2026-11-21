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
    python3 teamcamel.py --start-at-login
                                     optional, only if you want it: also start TeamCamel
                                     when you log in, and again if it stops, by adding a
                                     login item (macOS: a LaunchAgent in
                                     ~/Library/LaunchAgents, which starts a small helper
                                     app built on the Mac, in ~/Library/Application
                                     Support/TeamCamel; Linux: a systemd user service;
                                     Windows: a file in the Startup folder)
    python3 teamcamel.py --no-start-at-login
                                     take that login item (and helper app) away again
    python3 teamcamel.py --stop      stop it (it starts again with the next Claude session,
                                     or the next login if the login item is there)
    python3 teamcamel.py --uninstall take this copy out of Claude's start-up (and the login
                                     item, if any) and stop it; the folder stays for you
                                     to delete
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

__version__ = "2.9.11"

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
COLLECT_EVERY = 15             # seconds between local reads, unless the settings say otherwise
REFRESH_CHOICES = (5, 10, 15, 30, 60, 120, 300)   # what the settings offer for that
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


def _folder_key(path, cache):
    """A session folder as one string: the same folder however it was reached (through a link,
    or from a worktree inside it)."""
    if not path:
        return None
    if path not in cache:
        plain = path.replace("\\", "/").split("/.claude/worktrees/")[0].rstrip("/")
        try:
            real = os.path.realpath(plain)
        except (OSError, ValueError):
            real = plain
        cache[path] = real.replace("\\", "/")
    return cache[path]


def tell_workspaces_apart(sessions):
    """Two different folders with the same name (two cases each kept in a folder "workspace")
    are two workspaces, not one. Their labels then also name the folder each sits in, as in
    "_Kian/workspace" and "_Grinberg/workspace", going further up only if that is not enough.
    Takes each session's private "_dir" (its folder) away."""
    folders, cache = {}, {}
    for s in sessions:
        key = _folder_key(s.pop("_dir", None), cache)
        if key:
            folders.setdefault(s["ws"], {}).setdefault(key, []).append(s)
    for label, by_folder in folders.items():
        if len(by_folder) < 2:
            continue
        above = {key: key.split("/")[:-1] for key in by_folder}
        for depth in range(1, max(len(a) for a in above.values()) + 1):
            tails = {key: "/".join(a[-depth:]).lstrip("/") for key, a in above.items()}
            if len(set(tails.values())) == len(tails):
                break
        else:
            # The same folder above them all (wp-abc and abc side by side): their own names differ.
            tails = {key: "/".join(a[-1:]).lstrip("/") + "/" + key.rsplit("/", 1)[-1] for key, a in above.items()}
            label = ""
        for key, ss in by_folder.items():
            name = (tails[key] + "/" + label).strip("/") if label else tails[key].strip("/")
            for s in ss:
                s["ws"] = name or s["ws"]


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

DESKTOP_KEYS = ("sessionId", "cliSessionId", "cwd", "originCwd", "title", "isArchived", "lastActivityAt",
                "turnWrapUp", "postTurnSummary", "lastTurnEdgeAt", "priorCliSessionIds")
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


# The Claude app's end-of-turn card: Claude calls this tool with a short recap and, when the next
# move is yours, a question with answer buttons. The app counts the question as waiting on you only
# when Claude marks it "blocked"; one that "can wait" stays on screen all the same, until you answer.
CARD_TOOL = "mcp__ccd_turn__wrap_up"
_card_cache = {}


def card_question(cli_id):
    """The question the last turn's end-of-turn card asks you, not yet answered, or None."""
    path = transcript_path(cli_id)
    if not path:
        return None
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        cached = _card_cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
        ask = _open_card(path, st.st_size)
    except OSError:
        return None
    _card_cache[path] = (key, ask)
    return ask


def _result_text(block):
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _open_card(path, size):
    start = max(0, size - 1024 * 1024)
    with open(path, "rb") as fh:
        fh.seek(start)
        lines = fh.read().split(b"\n")
    if start > 0:
        lines = lines[1:]
    replies = {}                 # the app's answer to each tool call: whether it drew the card
    for raw in reversed(lines):
        if b'"type":"user"' not in raw and b'"type":"assistant"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if d.get("isSidechain") or d.get("isMeta") or d.get("type") not in ("user", "assistant"):
            continue
        content = (d.get("message") or {}).get("content")
        blocks = [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []
        if d["type"] == "user":
            results = [c for c in blocks if c.get("type") == "tool_result"]
            if not results:
                return None      # you wrote (or clicked an answer) after it: nothing open
            for c in results:
                replies[c.get("tool_use_id")] = _result_text(c)
            continue
        for c in reversed(blocks):
            if c.get("type") == "tool_use" and c.get("name") == CARD_TOOL:
                given = c.get("input") if isinstance(c.get("input"), dict) else {}
                ask = next((given[k] for k in ("ask", "next_ask", "needs_user")
                            if isinstance(given.get(k), str) and given[k].strip()), None)
                if not ask or "draws no box" in replies.get(c.get("id"), ""):
                    return None
                return " ".join(ask.split())
    return None


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
                threads = [r for r in rows if r.get("id") and r.get("thread_source") in (None, "user")]
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
        title = " ".join(str(t.get("name") or t.get("title") or "(untitled)").split())
        if t.get("archived"):
            # Archived in Codex: shown grey, never needing you; its transcript is not read.
            sessions.append({"id": "codex-%s" % t["id"], "title": title if len(title) <= 80 else title[:77] + "...",
                             "ws": ws_name(t.get("cwd")), "state": "completed", "detail": "archived",
                             "since": t.get("updated_at_ms"), "attention": False, "live": False,
                             "link": "codex://threads/%s" % t["id"] if "desktop" in str(t.get("originator") or "").lower() else None,
                             "app": "codex", "archived": True, "cwd": t.get("cwd")})
            continue
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
            "cwd": t.get("cwd"),          # taken out again by collect(), after looking at the folder
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
# Such a program counts only while it is busy, so an idle leftover (a helper service) does not:
# while it uses the processor, or moves data, reading or writing the disk or (on a Mac) sending
# or receiving over the network, as a copy or an upload does with hardly any processor use.

BUSY_CPU = 10.0                # % of one core above which a program a session started counts as working
BUSY_BYTES_PER_MIN = 512 * 1024   # data moved per minute (disk, network) above which it counts as working
BUSY_GRACE = 120               # seconds it still counts after its last activity: a pause is not the end
AGENT_HOURS = 12               # a background helper agent with no end notice stops counting after this
STUCK_CPU = 50.0               # % of one core: a program this busy that reads, writes and sends nothing
STUCK_AFTER = 3600             # for this many seconds "may be stuck"
STOP_WAIT = 5                  # seconds to wait for a program asked to stop before saying it still runs
STOP_REQUEST_MINUTES = 10      # a stop asked for from another computer is carried out only while this fresh

_ENV_PID = re.compile(r"(?:^|\s)CLAUDE_PID=(\d+)(?=\s|$)")
_TEAMCAMEL = re.compile(r"(?:^|[/ ])teamcamel\.py(?=\s|$)")
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


# The Claude desktop app's own programs (the app, its helpers, its launcher), by where they run from.
_CLAUDE_APP = re.compile(r"^[^ ]*/Claude\.app/Contents/")


def _started_by_claude_app(pid, claude, parent, text):
    """True for the Claude desktop app's own programs and what they start, unless the session's own
    Claude Code (whose mark the program carries) stands between them and the app."""
    seen = 0
    while pid and seen < 64:
        if pid == claude:
            return False
        if _CLAUDE_APP.match(text.get(pid, "")):
            return True
        pid, seen = parent.get(pid), seen + 1
    return False


def marked_processes(table):
    """Programs started by Claude sessions' commands, running now:
    [{"pid", "claude", "session", "cpu", "program", "since"}]."""
    me = os.getpid()
    marks = {}
    for pid, ppid, text in table or []:
        pid_m = _ENV_PID.search(text)
        # Not TeamCamel itself (nor another copy of it running): when a Claude hook started it, it
        # carries the marks too.
        if pid_m and pid != me and ppid != me and not _TEAMCAMEL.search(text):
            sess_m = _ENV_SESSION.search(text)
            marks[pid] = (int(pid_m.group(1)), sess_m.group(1) if sess_m else None)
    # Not the Claude app: when a session's command quits and reopens it, the app and everything it
    # starts carry that session's mark from then on, though none of it is the session's work.
    parent = {pid: ppid for pid, ppid, _ in table or []}
    text = {pid: t for pid, _, t in table or []}
    for pid in [p for p in marks if _started_by_claude_app(p, marks[p][0], parent, text)]:
        del marks[pid]
    found = []
    for pid, (cpu, since, program) in describe(list(marks)).items():
        claude, session = marks[pid]
        found.append({"pid": pid, "claude": claude, "session": session, "cpu": cpu, "program": program, "since": since})
    return found


def describe(pids):
    """{pid: (processor %, start ms, program name)} for the pids running now."""
    if not pids:
        return {}
    try:
        out = subprocess.run(["ps", "-o", "pid=,pcpu=,lstart=,comm=", "-p", ",".join(str(p) for p in pids)],
                             capture_output=True, text=True, errors="replace", timeout=10,
                             env=dict(os.environ, LC_ALL="C", TZ="UTC")).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8 or not parts[0].isdigit():
            continue
        try:
            since = int(datetime.strptime(" ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y")
                        .replace(tzinfo=timezone.utc).timestamp() * 1000)
            found[int(parts[0])] = (float(parts[1]), since, os.path.basename(parts[7].strip()) or "program")
        except ValueError:
            continue
    return found


# --- programs set running by a session's commands, without its mark ------------------
#
# A program a session set running can lose the session's mark: one started again by a scheduled
# job, by a loop, or by you, from a script the session wrote. Such a program is tied to the
# session whose own commands named the program's file, a script or program of yours outside the
# system folders, within the last 14 days; when several did, to the one that named it last. The
# programs it starts belong to the same session.

NAMED_DAYS = 14
NAMED_READ_SECONDS = 2.0       # time per cycle for reading commands from transcripts not read yet
# The system's own programs and the apps: never a session's file. (Temporary folders are not among
# them: sessions often write a script there and run it.)
_SYSTEM_PLACES = ("/bin/", "/sbin/", "/usr/", "/System/", "/Library/", "/dev/", "/etc/", "/private/etc/",
                  "/opt/homebrew/bin/", "/opt/homebrew/Cellar/", "/opt/homebrew/opt/", "/Applications/")
_RUNNERS = {"bash", "sh", "zsh", "dash", "fish", "python", "python3", "Python", "node", "perl", "ruby", "env",
            "caffeinate", "nohup", "nice", "timeout", "osascript", "uv"}
_PATH_QUOTED = re.compile(r"""(['"])((?:/|~/)[^'"\n]+?)\1""")
_PATH_BARE = re.compile(r"""(?<![\w/.~-])((?:/|~/)[^\s'"`;|&<>(){}]+)""")
# Claude Code as the desktop app runs it, and the app's launcher for Claude Code and its connectors.
_CLAUDE_LAUNCHERS = ("/claude.app/Contents/MacOS/claude", "/Claude.app/Contents/Helpers/disclaimer")
_named_cache = {}            # {transcript: {"offset": read up to, "paths": {path: last named ms}}}
_file_cache = {}             # {path: (checked at, is a file)}


def paths_in(text):
    """The paths of the user's own files and folders that a command names."""
    found = {m.group(2) for m in _PATH_QUOTED.finditer(text)} | {m.group(1) for m in _PATH_BARE.finditer(text)}
    out = set()
    for p in found:
        p = p.rstrip(".,:;")
        if p.startswith("~/"):
            p = str(HOME) + p[1:]
        if len(p) >= 6 and not p.startswith(_SYSTEM_PLACES):
            out.add(p)
    return out


def _named_record(raw, paths):
    if b'"tool_use"' not in raw:
        return
    try:
        d = json.loads(raw)
    except ValueError:
        return
    content = (d.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    when = record_ms(d) or 0
    for c in content:
        if isinstance(c, dict) and c.get("type") == "tool_use" and isinstance(c.get("input"), dict):
            for key in ("command", "file_path", "path"):
                value = c["input"].get(key)
                if isinstance(value, str):
                    for p in paths_in(value):
                        if when >= paths.get(p, 0):
                            paths[p] = when


def named_paths(cli_ids, deadline):
    """{path: when last named} from the commands in these sessions' transcripts. Only new lines are
    read each time, and only while the deadline allows; the rest waits for the next cycle."""
    out = {}
    for cli in cli_ids:
        path = transcript_path(cli)
        if not path:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        cache = _named_cache.get(path)
        if not cache or size < cache["offset"]:
            cache = _named_cache[path] = {"offset": 0, "paths": {}}
        while size > cache["offset"] and time.time() < deadline:
            try:
                with open(path, "rb") as fh:
                    fh.seek(cache["offset"])
                    chunk = fh.read(min(size - cache["offset"], 8 * 1024 * 1024))
            except OSError:
                break
            end = chunk.rfind(b"\n")
            if end < 0:
                if len(chunk) < 8 * 1024 * 1024:
                    break                          # a line still being written
                cache["offset"] += len(chunk)       # one enormous line: skipped
                continue
            cache["offset"] += end + 1
            for raw in chunk[:end].split(b"\n"):
                _named_record(raw, cache["paths"])
        for p, ms in cache["paths"].items():
            if ms > out.get(p, 0):
                out[p] = ms
    return out


_COMPILED = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
             b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\x7fELF", b"MZ")


def _script_file(path):
    """A file of the session's own: a script or data, not a program such as uv or rclone (a session that
    once ran a program by its path does not own every later run of it)."""
    hit = _file_cache.get(path)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        with open(path, "rb") as fh:
            value = not fh.read(4).startswith(_COMPILED)
    except OSError:
        value = False
    _file_cache[path] = (time.time(), value)
    return value


def command_lines():
    """[(pid, ppid, command line)] for this user's processes, without their environment."""
    if WINDOWS:
        return []
    try:
        out = subprocess.run(["ps", "-U", str(os.getuid()), "-o", "pid=,ppid=,command="],
                             capture_output=True, text=True, errors="replace", timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def named_programs(registry, desktop, skip=()):
    """Programs tied to a session by the files its commands named:
    [{"pid", "claude": None, "session", "cpu", "program", "since", "via": "named"}]."""
    horizon = now_ms() - NAMED_DAYS * 86400 * 1000
    sessions = {}
    for d in desktop or []:
        cli = d.get("cliSessionId")
        if cli and not d.get("isArchived") and (_as_int(d.get("lastActivityAt")) or 0) >= horizon:
            sessions[cli] = [cli] + [x for x in (d.get("priorCliSessionIds") or []) if isinstance(x, str)]
    for e in registry:
        if e.get("sessionId"):
            sessions.setdefault(e["sessionId"], [e["sessionId"]])
    if not sessions:
        return []
    deadline = time.time() + NAMED_READ_SECONDS
    by_name = {}                 # file name -> [(path, session, when named)]
    for sid, ids in sessions.items():
        for path, when in named_paths(ids, deadline).items():
            if when >= horizon:
                by_name.setdefault(os.path.basename(path.rstrip("/")), []).append((path, sid, when))
    rows = command_lines()
    parent = {pid: ppid for pid, ppid, _ in rows}
    command = {pid: cmd for pid, _, cmd in rows}
    # Nothing under Claude Code or the Claude app's program launcher: a session's commands carry its
    # mark, and the connector programs they start are not work. (The Claude app's Terminal panel is
    # not among them: a script you start again there still counts.) Not the Claude app's own programs,
    # and not TeamCamel itself (its own helpers, ps and the like, name no session's file).
    claude_pids = {e["pid"] for e in registry if e.get("pid")}
    me = os.getpid()

    def under_claude(pid):
        if "/Claude.app/" in command.get(pid, ""):
            return True
        seen = 0
        while pid and seen < 64:
            if pid in claude_pids or any(mark in command.get(pid, "") for mark in _CLAUDE_LAUNCHERS):
                return True
            pid, seen = parent.get(pid), seen + 1
        return False

    owner = {}                   # pid -> (session, when named, the file named)
    for pid, ppid, cmd in rows:
        if pid == me or pid in skip or under_claude(pid):
            continue
        best = None
        for word in cmd.split():
            if "/" not in word:
                continue
            for path, sid, when in by_name.get(word.rstrip("/").rsplit("/", 1)[-1], ()):
                if path in cmd and _script_file(path) and (best is None or when > best[1]):
                    best = (sid, when, path)
        if best:
            owner[pid] = best
    if not owner:
        return []
    # What they start belongs to the same session.
    children = {}
    for pid, ppid, _ in rows:
        children.setdefault(ppid, []).append(pid)
    queue = list(owner)
    while queue:
        pid = queue.pop()
        for child in children.get(pid, ()):
            if child not in owner and child not in skip:
                owner[child] = owner[pid]
                queue.append(child)
    found = []
    for pid, (cpu, since, program) in describe(list(owner)).items():
        sid, _, named = owner[pid]
        if program in _RUNNERS or program.startswith("python"):
            program = os.path.basename(named)        # "bash run_uploads.sh": the script, not bash
        found.append({"pid": pid, "claude": None, "session": sid, "cpu": cpu, "program": program,
                      "since": since, "via": "named"})
    return found


_activity = {}               # {(pid, start): {"bytes": moved so far, "at": when measured, "last": last busy, "rate",
                             #                 "alive": last sign of work (data moved, or the processor let go)}}
_activity_seed = None        # "alive" times kept over a restart of TeamCamel, read once
_activity_saved = 0.0
_programs_seen = []          # programs sessions started that run now, with what they do: for the report
_stoppable = {}              # {(pid, start): {"name", "session"}}: the programs the page shows, the only ones it may stop
_names = {}                  # {(pid, start): the name shown}, from the last look


def _activity_file():
    return log_path().with_name("teamcamel.activity.json")       # this computer's own, not shared


def _load_activity_seed():
    global _activity_seed
    if _activity_seed is None:
        try:
            d = read_json(_activity_file())
            _activity_seed = {k: v for k, v in d.items() if type(v) in (int, float)} if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _activity_seed = {}
    return _activity_seed


def _save_activity(now):
    """Keeps the "alive" times in a small local file, at most once a minute, so that a restart of
    TeamCamel (a new version, say) does not start the "may be stuck" clocks over."""
    global _activity_saved
    if now - _activity_saved < 60:
        return
    _activity_saved = now
    data = {"%d:%d" % key: r["alive"] for key, r in _activity.items() if "alive" in r}
    try:
        path = _activity_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        pass


def _rusage_lib():
    import ctypes

    class Usage(ctypes.Structure):                 # rusage_info_v2, <libproc.h>
        _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
            "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins", "wired_size",
            "resident_size", "phys_footprint", "proc_start_abstime", "proc_exit_abstime", "child_user_time",
            "child_system_time", "child_pkg_idle_wkups", "child_interrupt_wkups", "child_pageins",
            "child_elapsed_abstime", "diskio_bytesread", "diskio_byteswritten")]
    return ctypes.CDLL("/usr/lib/libproc.dylib"), Usage, ctypes


def bytes_moved(pids):
    """{pid: bytes moved so far}: on a Mac read and written on disk plus sent and received over the
    network; on Linux everything read and written. Programs that cannot be read are left out."""
    out = {}
    if not pids:
        return out
    if MACOS:
        try:
            lib, Usage, ctypes = _rusage_lib()
            for pid in pids:
                u = Usage()
                if lib.proc_pid_rusage(pid, 2, ctypes.byref(u)) == 0:
                    out[pid] = u.diskio_bytesread + u.diskio_byteswritten
        except (OSError, AttributeError):
            pass
        try:
            net = subprocess.run(["nettop", "-P", "-L", "1", "-x", "-J", "bytes_in,bytes_out"],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            net = ""
        for line in net.splitlines():
            cols = line.split(",")
            name = cols[0].rsplit(".", 1)
            if len(cols) >= 3 and len(name) == 2 and name[1].isdigit() and int(name[1]) in out:
                try:
                    out[int(name[1])] += int(cols[1] or 0) + int(cols[2] or 0)
                except ValueError:
                    pass
    elif Path("/proc").is_dir():
        for pid in pids:
            try:
                fields = dict(line.split(":", 1) for line in Path("/proc/%d/io" % pid).read_text().splitlines())
                out[pid] = int(fields["rchar"]) + int(fields["wchar"])
            except (OSError, ValueError, KeyError):
                continue
    return out


def busy_processes(procs):
    """The programs among procs that are working: busy on the processor, or moving data at more than
    BUSY_BYTES_PER_MIN since the last look, or either within the last BUSY_GRACE seconds."""
    now = time.time()
    moved = bytes_moved([p["pid"] for p in procs])
    seed = _load_activity_seed()
    busy, seen = [], set()
    for p in procs:
        key = (p["pid"], p["since"])            # a reused process number is another program
        seen.add(key)
        before = _activity.get(key) or {}
        active = p["cpu"] >= BUSY_CPU
        record = {"at": now, "last": before.get("last", 0),
                  "alive": before.get("alive") or seed.get("%d:%d" % key) or now}
        total = moved.get(p["pid"])
        if total is None:
            record["alive"] = now                # what it moves cannot be read: never judged stuck
        else:
            record["bytes"] = total
            if before.get("bytes") is not None and now > before["at"]:
                rate = max(0, total - before["bytes"]) * 60.0 / (now - before["at"])
                record["rate"] = rate
                active = active or rate >= BUSY_BYTES_PER_MIN
                if total != before["bytes"]:
                    record["alive"] = now
        if p["cpu"] < STUCK_CPU:
            record["alive"] = now                # not spinning: waiting, or done
        if active:
            record["last"] = now
        _activity[key] = record
        if now - record["last"] < BUSY_GRACE:
            busy.append(p)
    for key in [k for k in _activity if k not in seen]:
        del _activity[key]
    _save_activity(now)
    return busy


def quiet_seconds(p):
    """How long a program has used the processor nonstop while reading, writing and sending nothing;
    0 when it showed a sign of work at the last look, or when that cannot be told."""
    r = _activity.get((p["pid"], p["since"])) or {}
    if "bytes" not in r or p["cpu"] < STUCK_CPU:
        return 0
    return max(0, int(time.time() - r.get("alive", time.time())))


def may_be_stuck(p):
    return quiet_seconds(p) >= STUCK_AFTER


_SCRIPT = re.compile(r"\.(py|pyw|js|mjs|cjs|ts|sh|bash|zsh|rb|pl|php|lua|R|r|jl)$")


def program_name(program, command):
    """The name to show for a program: for one that runs a script (Python, node, a shell...), the
    script: 'qa3_billscan.py' rather than 'Python'."""
    low = program.lower()
    if not (program in _RUNNERS or low.startswith(("python", "node", "ruby", "perl", "php", "bun", "deno"))):
        return program
    words = command.split()
    for i, word in enumerate(words[1:], 1):
        if word == "-m" and i + 1 < len(words):
            return words[i + 1]                  # python -m a.module
        if word == "-c":
            return program
        if _SCRIPT.search(word):
            return os.path.basename(word)
    return program


def short_command(command, limit=240):
    """A command line to show: the home folder as ~, and cut to a readable length."""
    text = " ".join(str(command or "").split())
    home = str(HOME)
    if home and len(home) > 1:
        text = text.replace(home + "/", "~/")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _alive(pid, since):
    """True while that very program (the number and start time) still runs, not as a leftover entry."""
    got = describe([pid]).get(pid)
    if not got or got[1] != since:
        return False
    try:
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    return bool(stat) and not stat.startswith("Z")


def stop_program(pid, since, force=False, asked_by="this computer's page"):
    """Stops one of the programs the page shows as a session's work, and what it started.
    Returns (result, words): result is 'stopped', 'running' (asked to stop, still running), 'gone'
    (it had ended) or 'refused'. Anything else on the computer is never touched."""
    if WINDOWS:
        return "refused", "TeamCamel cannot stop programs on Windows"
    info = _stoppable.get((pid, since))
    if not info:
        return "refused", "not one of the programs TeamCamel shows as a session's work"
    if not _alive(pid, since):
        _stoppable.pop((pid, since), None)
        return "gone", "%s had already ended" % info["name"]
    rows = command_lines()
    children, command = {}, {}
    for p, pp, cmd in rows:
        children.setdefault(pp, []).append(p)
        command[p] = cmd
    targets, todo = [pid], [pid]
    while todo:
        for c in children.get(todo.pop(), ()):
            if c not in targets:
                targets.append(c)
                todo.append(c)
    keep = {os.getpid()} | {e["pid"] for e in _last_registry if e.get("pid")}
    targets = [t for t in targets if t == pid or (t not in keep and not _CLAUDE_APP.match(command.get(t, "")))]
    sig = signal.SIGKILL if force else signal.SIGTERM
    for t in reversed(targets):                  # what it started first, the program last
        try:
            os.kill(t, sig)
        except OSError:
            pass
    print("%s stopped%s %s (process %d, %d with what it started) of session %s, asked by %s"
          % (stamp(), " by force" if force else "", info["name"], pid, len(targets), info["session"], asked_by),
          flush=True)
    deadline = time.time() + STOP_WAIT
    while time.time() < deadline:
        if not _alive(pid, since):
            _stoppable.pop((pid, since), None)
            return "stopped", "%s stopped" % info["name"]
        time.sleep(0.25)
    return "running", "%s is still running" % info["name"]


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


def background_work(registry, desktop=None):
    """{Claude session id: (detail, since_ms)} for sessions with work still running."""
    table = process_table()
    procs = marked_processes(table)
    if table is not None:
        try:
            procs = (procs or []) + named_programs(registry, desktop, skip={p["pid"] for p in procs or []})
        except Exception as err:   # a guess at most: never lose the rest over it
            print("teamcamel: programs named by sessions not read: %r" % err, file=sys.stderr)
    by_pid = {e["pid"]: e for e in registry}
    labels, first, stuck, programs = {}, {}, {}, {}

    def add(sid, label, since):
        labels.setdefault(sid, []).append(label)
        if since and (sid not in first or since < first[sid]):
            first[sid] = since

    global _programs_seen, _stoppable
    busy = busy_processes(procs or [])
    commands = {pid: cmd for pid, _, cmd in command_lines()} if procs else {}
    global _names
    names = {}
    for p in procs or []:
        key = (p["pid"], p["since"])
        if p.get("via") != "named":              # a named program already carries its script's name
            # A program ending as it is looked at may have no command line left: keep its name.
            p["program"] = (program_name(p["program"], commands[p["pid"]]) if p["pid"] in commands
                            else _names.get(key, p["program"]))
        names[key] = p["program"]
    _names = names
    stoppable = {}
    for p in busy:
        sid = (by_pid.get(p["claude"]) or {}).get("sessionId") or p["session"]
        if sid:
            add(sid, p["program"], p["since"])
            quiet = quiet_seconds(p)
            if quiet >= STUCK_AFTER:
                stuck.setdefault(sid, []).append(p["program"])
            programs.setdefault(sid, []).append({
                "pid": p["pid"], "since": p["since"], "name": p["program"], "cpu": round(p["cpu"]),
                "cmd": short_command(commands.get(p["pid"], "")), "stuck": quiet >= STUCK_AFTER, "quietSec": quiet})
            stoppable[(p["pid"], p["since"])] = {"name": p["program"], "session": sid}
    _stoppable = stoppable
    # What each program a session started is doing: in the report, to see why one counts or not.
    _programs_seen = [{"session": (by_pid.get(p["claude"]) or {}).get("sessionId") or p["session"],
                       "program": p["program"], "cpu": round(p["cpu"], 1), "busy": p in busy,
                       "via": p.get("via", "mark"), "quietMin": quiet_seconds(p) // 60,
                       "kbPerMin": round(_activity.get((p["pid"], p["since"]), {}).get("rate", 0) / 1024)
                       if "rate" in _activity.get((p["pid"], p["since"]), {}) else None}
                      for p in procs or []]
    # A command Claude runs goes through a shell started from its shell snapshot.
    shells = {ppid for _, ppid, text in table or [] if "shell-snapshots" in text}
    for e in registry:
        if e.get("status") in ("busy", "waiting") or not e.get("sessionId"):
            continue
        for kind, start in open_tasks(e["sessionId"], _as_int(e.get("startedAt"))):
            if kind == "command" and table is not None and e["pid"] not in shells:
                continue    # no shell of this session is left: the command is over, notice or not
            add(e["sessionId"], kind, start)
    return {sid: {"detail": work_detail(found), "since": first.get(sid), "stuck": sorted(set(stuck.get(sid, []))),
                  "programs": programs.get(sid, [])} for sid, found in labels.items()}


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
    for key in ("versions", "online", "archived", "tips", "stopFromOthers"):
        if isinstance(d.get(key), bool):
            out[key] = d[key]
    if type(d.get("refresh")) is int and d["refresh"] in REFRESH_CHOICES:
        out["refresh"] = d["refresh"]
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


# --- what became of a session's folder ----------------------------------------------
#
# A session whose folder was renamed or moved carries on as before; one whose folder was deleted
# is over. On a Mac a folder keeps its identity number through renames and moves, also into the
# Trash, and the system finds a folder by that number. So TeamCamel notes the number of each
# session folder while it is there; when a folder is gone from its place it looks the number up:
# found somewhere = renamed or moved, and from then on the session is followed there, under the
# folder's new name; in the Trash or nowhere = deleted. Elsewhere, or for a folder TeamCamel never
# saw, a folder that is gone is only "not found".

_folders = None              # {"ids": {path: [device, number]}, "moved": {old path: new path}}, this computer's
_folders_dirty = False


def folder_ids_path():
    return STATE / ("teamcamel.folders." + (re.sub(r"[^\w.-]", "_", node_name()).strip(".") or "node"))


def _folder_table():
    global _folders
    if _folders is None:
        try:
            loaded = json.loads(folder_ids_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if not isinstance(loaded, dict):
            loaded = {}
        if "ids" not in loaded and "moved" not in loaded:      # 2.4 kept the numbers alone
            loaded = {"ids": loaded, "moved": {}}
        _folders = {"ids": dict(loaded.get("ids") or {}), "moved": dict(loaded.get("moved") or {})}
    return _folders


def save_folder_ids():
    global _folders_dirty
    if _folders_dirty and _folders is not None:
        try:
            STATE.mkdir(exist_ok=True)
            folder_ids_path().write_text(json.dumps(_folders), encoding="utf-8")
            _folders_dirty = False
        except OSError:
            pass


def _in_trash(path):
    p = path.rstrip("/") + "/"
    return "/.Trash/" in p or "/.Trashes/" in p        # the user's Trash, or another disk's


def _locate(device, number):
    """Where the folder with this identity is now (macOS), or None if there is none."""
    try:
        fd = os.open("/.vol/%d/%d" % (device, number), os.O_RDONLY)
    except (OSError, ValueError, TypeError):
        return None
    try:
        import fcntl
        return fcntl.fcntl(fd, 50, b"\0" * 1024).split(b"\0", 1)[0].decode("utf-8", "replace")   # F_GETPATH
    except OSError:
        return None
    finally:
        os.close(fd)


def _volume_of(path):
    """The device number of the nearest folder above path that still exists."""
    p = Path(path)
    for parent in [p] + list(p.parents):
        try:
            return os.stat(str(parent)).st_dev
        except OSError:
            continue
    return None


def follow_folder(path, seen):
    """(fate, where): fate None while the folder is there, under its name or a new one, or after a
    move (then where is its new place); 'deleted'; or 'missing' when TeamCamel cannot tell."""
    global _folders_dirty
    if not path:
        return None, path
    if path in seen:
        return seen[path]
    table = _folder_table()
    ids, moved = table["ids"], table["moved"]
    where = moved.get(path, path)
    if where != path and not os.path.isdir(where) and os.path.isdir(path):
        del moved[path]                  # the followed folder is gone and one of the old name is back
        where = path
        _folders_dirty = True
    result = None
    try:
        st = os.stat(where)
        if MACOS and ids.get(where) != [st.st_dev, st.st_ino]:
            ids[where] = [st.st_dev, st.st_ino]
            _folders_dirty = True
        result = (None, where)
    except OSError:
        known = ids.get(where) or ids.get(path)
        # A device number can change with a restart: only a number from the same disk is trusted.
        if MACOS and isinstance(known, list) and len(known) == 2 and _volume_of(where) == known[0]:
            found = _locate(*known)
            if found and not _in_trash(found):
                moved[path], ids[found] = found, known           # renamed or moved: followed from now on
                _folders_dirty = True
                result = (None, found)
            else:
                result = ("deleted", where)
        else:
            result = ("missing", where)
    seen[path] = result
    return result


def with_folder(entry, path, seen):
    """A renamed or moved folder: the session carries on there, under the new name. A deleted
    one: no longer needing you. One that cannot be told: noted."""
    fate, where = follow_folder(path, seen)
    entry["_dir"] = where or path            # for telling apart folders of the same name
    if where and where != path:
        entry["ws"] = ws_name(where)
    if fate:
        entry["folder"] = fate
        if fate == "deleted":
            entry["attention"] = False
    return entry


# --- sessions marked "handled" on the page ------------------------------------------
#
# A session needing you can be marked handled on the page: it then turns grey and stops
# counting until it does something new. The marks are kept in the shared folder, keyed by
# computer and session, with the session's time at marking: a new time ends the mark.

HANDLED = "teamcamel.handled"
HANDLED_KEEP_DAYS = 90
_handled_cache = (None, {})


def handled_marks():
    global _handled_cache
    path = STATE / HANDLED
    try:
        key = path.stat().st_mtime_ns
    except OSError:
        return {}
    if _handled_cache[0] == key:
        return _handled_cache[1]
    try:
        d = read_json(path)
    except (OSError, ValueError):
        return _handled_cache[1]
    marks = {k[:300]: v for k, v in (d.items() if isinstance(d, dict) else [])
             if isinstance(k, str) and type(v) is int}
    _handled_cache = (key, marks)
    return marks


def mark_handled(node, sid, since):
    """Mark (since: the session's time) or unmark (since None) one session."""
    marks = dict(handled_marks())
    key = ("%s/%s" % (node, sid))[:300]
    if since is None:
        marks.pop(key, None)
    else:
        marks[key] = since
    old = now_ms() - HANDLED_KEEP_DAYS * 86400 * 1000
    marks = dict(sorted(((k, v) for k, v in marks.items() if v >= old), key=lambda kv: kv[1])[-1000:])
    STATE.mkdir(exist_ok=True)
    path = STATE / HANDLED
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(marks, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(path))


# --- a finished turn that asks something of you --------------------------------------
#
# Claude Code reports a session idle once its turn ends, also when the turn ended with a
# question to you in plain words. The Claude desktop app keeps, per session, what the last turn
# needs from you: the wrap-up Claude gives at the end of a turn ("needsUser"), else a summary
# of the turn ("blocked" or "need_input", with what is needed). Both are cleared when the next
# turn starts. Read the way the app reads them: a wrap-up without a question means nothing is
# needed, whatever the summary says.

def turn_needs(d):
    """What the Claude app says the last, finished turn needs from you, or None."""
    wrap, summary = d.get("turnWrapUp"), d.get("postTurnSummary")
    if isinstance(wrap, dict):
        need = wrap.get("needsUser")
        return need.strip() or None if isinstance(need, str) else None
    if isinstance(summary, dict) and summary.get("status_category") in ("blocked", "need_input"):
        need = summary.get("needs_action")
        return need.strip() if isinstance(need, str) and need.strip() else "your reply"
    return None


def short(text, limit=70):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


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
        work = background[cli_id]
        if work.get("stuck"):
            # Nonstop on the processor, and nothing read, written or sent for an hour: it needs you.
            return "stuck", ", ".join(work["stuck"]), work.get("since") or since, True
        return "working", work["detail"], work.get("since") or since, False
    attention = False
    if state == "interrupted":
        recent = since and now_ms() - since < INTERRUPTED_ATTENTION_DAYS * 86400 * 1000
        attention = bool(live) or bool(recent)
    return state, detail, since, attention


def with_programs(entry, work):
    """The programs that keep a session working, for the page to name them and offer to stop them."""
    if work and entry["state"] in ("working", "stuck") and work.get("programs"):
        entry["programs"] = work["programs"]
    return entry


def effective_settings(name):
    """The settings in force on a computer: its own, else the ones for all computers."""
    own = own_settings(name)
    return own if own is not None else (shared_settings() or {})


def _safe(name):
    return re.sub(r"[^\w.-]", "_", str(name)).strip(".") or "node"


def stop_request_path(src, dst):
    """Stop requests one computer sends another: written only by the sender, so never two writers."""
    return STATE / ("teamcamel.stop.%s.to.%s" % (_safe(src), _safe(dst)))


def collect():
    global _last_registry
    registry = _last_registry = live_registry()
    by_host = {e["hostSessionId"]: e for e in registry if e.get("hostSessionId")}
    by_cli = {e["sessionId"]: e for e in registry if e.get("sessionId")}
    sessions, matched = [], set()
    desktop = desktop_sessions()
    try:
        background = background_work(registry, desktop)
    except Exception as err:   # never lose the session list over it
        print("teamcamel: background work not read: %r" % err, file=sys.stderr)
        background = {}

    seen = {}
    for d in desktop:
        if not d.get("sessionId"):
            continue
        live = by_host.get(d["sessionId"]) or by_cli.get(d.get("cliSessionId"))
        if live:
            matched.add(live["pid"])
        cwd = d.get("originCwd") or d.get("cwd") or (live or {}).get("cwd")
        if d.get("isArchived"):
            # Archived in Claude: shown grey, never needing you; its transcript is not read.
            sessions.append({
                "id": d["sessionId"],
                "title": d.get("title") or (live or {}).get("name") or "(untitled)",
                "ws": ws_name(cwd),
                "state": "completed",
                "detail": "archived",
                "since": d.get("lastActivityAt"),
                "attention": False,
                "live": bool(live),
                "link": "claude://claude.ai/epitaxy/%s" % d["sessionId"],
                "archived": True,
                "_dir": cwd,
            })
            continue
        # Work started under one of the session's earlier internal ids is the session's work too.
        for old in d.get("priorCliSessionIds") or []:
            if old in background and d.get("cliSessionId") and d["cliSessionId"] not in background:
                background[d["cliSessionId"]] = background[old]
        state, detail, since, attention = classify(live, d.get("cliSessionId"), d.get("lastActivityAt"), background)
        need = (turn_needs(d) or card_question(d.get("cliSessionId"))) if state == "completed" else None
        if need:
            # The turn is over, but it ended asking you something: like a prompt, it needs you.
            when = _as_int(d.get("lastTurnEdgeAt")) or since
            if live or (when and now_ms() - when < INTERRUPTED_ATTENTION_DAYS * 86400 * 1000):
                state, detail, since, attention = "input", short(need), when, True
        sessions.append(with_folder(with_programs({
            "id": d["sessionId"],
            "title": d.get("title") or (live or {}).get("name") or "(untitled)",
            "ws": ws_name(cwd),
            "state": state,
            "detail": detail,
            "since": since,
            "attention": attention,
            "live": bool(live),
            "link": "claude://claude.ai/epitaxy/%s" % d["sessionId"],
        }, background.get(d.get("cliSessionId"))), cwd, seen))

    # Sessions started outside the desktop app (a terminal `claude`).
    for e in registry:
        if e["pid"] in matched:
            continue
        state, detail, since, attention = classify(e, e.get("sessionId"), e.get("updatedAt"), background)
        sessions.append(with_folder(with_programs({
            "id": e.get("sessionId") or "pid-%d" % e["pid"],
            "title": e.get("name") or "(terminal session)",
            "ws": ws_name(e.get("cwd")),
            "state": state,
            "detail": detail,
            "since": since,
            "attention": attention,
            "live": True,
            "link": None,
        }, background.get(e.get("sessionId"))), e.get("cwd"), seen))

    try:
        sessions += [with_folder(x, x.pop("cwd", None), seen) for x in codex_sessions()]
    except Exception as err:   # Codex's files are not ours to rely on: never lose the Claude sessions over them
        print("teamcamel: Codex sessions skipped: %r" % err, file=sys.stderr)

    save_folder_ids()
    tell_workspaces_apart(sessions)
    sessions.sort(key=lambda s: s["id"])
    return sessions


def code_stamp():
    try:
        st = SELF.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


_page = {"web": None, "mtime": None, "body": None}


def page_body():
    """web/index.html as last seen whole. A copy caught while the shared folder is still writing it
    (it does not end in </html> yet) is never served: the last whole copy is, until it is done."""
    path = WEB / "index.html"
    if _page["web"] != WEB:
        _page.update(web=WEB, mtime=None, body=None)
    try:
        mtime = path.stat().st_mtime_ns
        if mtime != _page["mtime"]:
            body = path.read_bytes()
            if body.rstrip().endswith(b"</html>"):
                _page.update(mtime=mtime, body=body)
    except OSError:
        pass
    return _page["body"]


def page_stamp():
    """Changes whenever a new whole web/index.html is there; an open page reloads itself on a new value."""
    page_body()
    return str(_page["mtime"] or "")


class Node:
    def __init__(self):
        self.name = node_name()
        self.host = host_name()
        self.os = os_label()
        self.lock = threading.Lock()
        self.tick_lock = threading.Lock()   # one look at a time: the regular one, or one right after a stop
        self.request_stamps = {}            # stop request files for this computer, as last seen
        self.snapshot = self.build()
        self.written_hash = None
        self.written_at = 0.0
        self.code = code_stamp()
        self.code_pending = None
        self.others = {}
        self.fallback = COLLECT_EVERY
        self.stops = {}         # stop requests from other computers carried out here: {id: {"result", "words", "at"}}

    def refresh_every(self):
        """Seconds between reads: this computer's own settings, else those for all computers,
        else --interval (15 by default)."""
        own = own_settings(self.name)
        chosen = (own if own is not None else shared_settings() or {}).get("refresh")
        return chosen if chosen in REFRESH_CHOICES else self.fallback

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
                "version": __version__, "generatedAt": now_ms(), "sessions": sessions, "apps": apps,
                "programs": _programs_seen,
                "stopFromOthers": bool(effective_settings(self.name).get("stopFromOthers")) and not WINDOWS,
                "stops": dict(getattr(self, "stops", {}))}

    def tick(self, write_now=False):
        with self.tick_lock:
            self._tick(write_now)

    def _tick(self, write_now):
        snap = self.build()
        if self.carry_out_stops():
            snap = self.build()          # a program stopped: report it gone, and the outcome, at once
            write_now = True
        with self.lock:
            self.snapshot = snap
        digest = hashlib.sha1(json.dumps([snap["sessions"], snap.get("apps"), snap.get("stops"),
                                          snap.get("stopFromOthers")], sort_keys=True).encode()).hexdigest()
        age = time.time() - self.written_at
        # Written to the shared folder no oftener than every 30 seconds, nor than the reads; right
        # away after a program was stopped, so the other computers see it as soon as Dropbox brings it.
        if ((digest != self.written_hash and (write_now or age >= max(WRITE_MIN_GAP, self.refresh_every())))
                or age >= HEARTBEAT):
            try:
                STATE.mkdir(exist_ok=True)
                state_file(self.name).write_text(json.dumps(snap, indent=1), encoding="utf-8")
                self.written_hash, self.written_at = digest, time.time()
            except OSError as err:
                print("teamcamel: cannot write state file: %s" % err, file=sys.stderr)

    def run(self, interval):
        self.fallback = interval
        while True:
            try:
                self.tick()
            except Exception as err:  # keep the collector alive across a bad read
                print("teamcamel: collect failed: %r" % err, file=sys.stderr)
            self.reload_if_updated()
            # Wait for the next read second by second, so a changed setting counts at once, and
            # keep checking for a new teamcamel.py every 15 seconds even when reads are rarer.
            waited = 0
            while waited < self.refresh_every():
                time.sleep(1)
                waited += 1
                if waited % COLLECT_EVERY == 0:
                    self.reload_if_updated()
                if self.stop_requests_changed():
                    # Another computer asks to stop a program here: within a second, not at the next look.
                    try:
                        self.tick()
                    except Exception as err:
                        print("teamcamel: stop request not carried out: %r" % err, file=sys.stderr)

    def goodbye(self, why):
        """The last report before stopping: the sessions as last seen, and that it stopped."""
        with self.lock:
            snap = dict(self.snapshot)
        snap["generatedAt"] = now_ms()
        snap["stopped"] = {"at": now_ms(), "why": why}
        try:
            STATE.mkdir(exist_ok=True)
            state_file(self.name).write_text(json.dumps(snap, indent=1), encoding="utf-8")
        except OSError:
            pass
        print("%s TeamCamel stopped: %s" % (stamp(), why), flush=True)

    def send_stop(self, target, pid, since, force):
        """Asks another computer, through the shared folder, to stop one of its sessions' programs.
        Returns the request's id; that computer reports the outcome in its own report."""
        other = None
        for path in STATE.glob("*.json"):
            d = self.other_node(path)
            if d and d.get("node") == target:
                other = d
        if not other:
            raise ValueError("%s is not reporting" % target)
        if not other.get("stopFromOthers"):
            raise ValueError("%s does not take stop requests from other computers (see its settings)" % target)
        path = stop_request_path(self.name, target)
        try:
            old = read_json(path).get("requests") or []
        except (OSError, ValueError, AttributeError):
            old = []
        fresh = now_ms() - STOP_REQUEST_MINUTES * 60000
        keep = [r for r in old if isinstance(r, dict) and (r.get("at") or 0) >= fresh][-20:]
        rid = os.urandom(6).hex()
        keep.append({"id": rid, "pid": pid, "since": since, "force": bool(force), "at": now_ms()})
        STATE.mkdir(exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"from": self.name, "to": target, "requests": keep}, indent=1), encoding="utf-8")
        os.replace(str(tmp), str(path))
        return rid

    def stop_requests_changed(self):
        """True when a stop request file for this computer appeared or changed since the last look."""
        seen = {}
        for path in STATE.glob("teamcamel.stop.*.to.%s" % _safe(self.name)):
            try:
                seen[path.name] = path.stat().st_mtime_ns
            except OSError:
                pass
        changed = seen != self.request_stamps
        self.request_stamps = seen
        return changed and bool(seen)

    def carry_out_stops(self):
        """Carries out the stop requests other computers left for this one, if its settings allow.
        True when there was something to report."""
        now = now_ms()
        for rid in [k for k, v in self.stops.items() if now - v["at"] > 3 * STOP_REQUEST_MINUTES * 60000]:
            del self.stops[rid]
        files = list(STATE.glob("teamcamel.stop.*.to.%s" % _safe(self.name)))
        if not files:
            return False
        allowed = bool(effective_settings(self.name).get("stopFromOthers"))
        done = False
        for path in files:
            try:
                d = read_json(path)
            except (OSError, ValueError):
                continue
            if not isinstance(d, dict) or d.get("to") != self.name or d.get("from") in (None, self.name):
                continue
            for r in d.get("requests") or []:
                if (not isinstance(r, dict) or not isinstance(r.get("id"), str) or r["id"] in self.stops
                        or type(r.get("pid")) is not int or type(r.get("since")) is not int
                        or now - (r.get("at") or 0) > STOP_REQUEST_MINUTES * 60000):
                    continue
                if allowed:
                    result, words = stop_program(r["pid"], r["since"], bool(r.get("force")), str(d["from"]))
                else:
                    result, words = "refused", "%s does not take stop requests from other computers" % self.name
                self.stops[r["id"][:40]] = {"result": result, "words": words, "at": now_ms()}
                done = True
        return done

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
        return {"now": now_ms(), "self": self.name, "version": __version__, "pollSec": self.refresh_every(),
                "heartbeatSec": HEARTBEAT, "page": page_stamp(), "pid": os.getpid(),
                # settingsScopes: this TeamCamel keeps "mine" and "shared" settings apart (2.1); a page
                # newer than a TeamCamel that has not restarted yet must not save to an older one
                "settingsScopes": True, "settings": shared_settings(), "mySettings": own_settings(self.name),
                "handled": handled_marks(),
                "canStop": not WINDOWS,       # this TeamCamel can stop programs (2.9.4)
                "nodes": [mine] + others}


# --- page server -------------------------------------------------------------

def start_command():
    """The command that starts TeamCamel on this computer, for the "not running" page."""
    if WINDOWS:
        return 'py "%s" --ensure' % SELF
    return 'python3 "%s" --ensure' % SELF


def service_worker():
    """A helper the browser keeps for this page: when TeamCamel does not answer, it shows a page
    saying so, with how to start it, instead of the browser's own "cannot connect"."""
    page = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>TeamCamel is not running</title>
<style>body{margin:0;background:#E6EECF;color:#15180E;font:17px/1.45 -apple-system,system-ui,"Segoe UI",Roboto,sans-serif;padding:48px 32px}
main{max-width:640px}h1{font-size:22px;margin:0 0 12px}p{margin:8px 0}.muted{color:#586050;font-size:15px}
code{display:block;background:#fff;border:1px solid #C7D1AA;border-radius:6px;padding:8px 10px;margin:8px 0;
font:13px ui-monospace,Menlo,Consolas,monospace;overflow-wrap:anywhere}button{font:inherit;font-size:14px;
background:#fff;border:1px solid #586050;border-radius:4px;padding:2px 10px;cursor:pointer}</style></head>
<body><main><h1>TeamCamel is not running on this computer</h1>
<p id="seen" class="muted"></p>
<p>STARTS To start it now, run this in Terminal:</p>
<code id="cmd"></code><button id="copy" type="button">Copy</button>
<p class="muted">This page looks every 5 seconds and opens TeamCamel as soon as it answers.</p></main>
<script>
document.getElementById('cmd').textContent = CMD;
document.getElementById('copy').onclick = () => navigator.clipboard && navigator.clipboard.writeText(CMD);
try {
  const seen = Number(localStorage.getItem('teamcamel.lastSeen'));
  if (seen) document.getElementById('seen').textContent = 'Last seen running: ' + new Date(seen).toLocaleString();
} catch (e) {}
setInterval(async () => {
  try { const r = await fetch('/api/state', { cache: 'no-store' }); if (r.ok) location.reload(); } catch (e) {}
}, 5000);
</script></body></html>"""
    page = page.replace("CMD", json.dumps(start_command()), 2).replace("STARTS ", (
        "It starts by itself when you log in, and with every Claude session." if login_item_installed()
        else "It starts by itself with every Claude session.") + " ", 1)
    return """// TeamCamel %s: shows a "not running" page when TeamCamel does not answer.
const PAGE = %s;
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  if (e.request.mode !== 'navigate') return;
  e.respondWith((async () => {
    const stop = new AbortController(), timer = setTimeout(() => stop.abort(), 8000);
    try { return await fetch(e.request, { signal: stop.signal }); }
    catch (err) { return new Response(PAGE, { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }); }
    finally { clearTimeout(timer); }
  })());
});
""" % (__version__, json.dumps(page))


def make_handler(node):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                return self.reply(403, "text/plain", b"local only")
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                body = page_body()
                if body is None:
                    return self.reply(500, "text/plain", b"web/index.html missing")
                return self.reply(200, "text/html; charset=utf-8", body)
            if path == "/api/state":
                return self.reply(200, "application/json", json.dumps(node.merged()).encode())
            if path == "/teamcamel-sw.js":
                return self.reply(200, "text/javascript; charset=utf-8", service_worker().encode("utf-8"))
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
            if self.path.split("?")[0] not in ("/api/settings", "/api/handled", "/api/stop"):
                return self.reply(404, "text/plain", b"not found")
            try:
                size = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                size = -1
            if not 0 < size <= SETTINGS_MAX:
                return self.reply(413, "text/plain", b"too large")
            if self.path.split("?")[0] == "/api/stop":
                # {"node", "pid", "since", "force"}: one of the programs the page shows as a session's work
                try:
                    body = json.loads(self.rfile.read(size))
                    target, pid, since = body.get("node"), body.get("pid"), body.get("since")
                    if not isinstance(target, str) or type(pid) is not int or type(since) is not int:
                        raise ValueError("not a stop")
                except (ValueError, AttributeError):
                    return self.reply(400, "text/plain", b"not a stop")
                if target == node.name:
                    result, words = stop_program(pid, since, bool(body.get("force")))
                    if result in ("stopped", "gone"):
                        node.tick(write_now=True)    # the page shows the program gone at once, not at the next look
                    return self.reply(200, "application/json", json.dumps({"result": result, "words": words}).encode())
                try:
                    rid = node.send_stop(target, pid, since, bool(body.get("force")))
                except ValueError as err:
                    return self.reply(200, "application/json", json.dumps({"result": "refused", "words": str(err)}).encode())
                except OSError as err:
                    return self.reply(500, "text/plain", str(err).encode())
                return self.reply(200, "application/json", json.dumps({"result": "sent", "id": rid}).encode())
            if self.path.split("?")[0] == "/api/handled":
                try:
                    body = json.loads(self.rfile.read(size))
                    since = body.get("since")
                    if (not isinstance(body.get("node"), str) or not isinstance(body.get("id"), str)
                            or not (since is None or type(since) is int)):
                        raise ValueError("not a mark")
                    mark_handled(body["node"], body["id"], since)
                except (ValueError, AttributeError):
                    return self.reply(400, "text/plain", b"not a mark")
                except OSError as err:
                    return self.reply(500, "text/plain", str(err).encode())
                return self.reply(200, "application/json", json.dumps(handled_marks()).encode())
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


def _stop_note_path():
    return log_path().with_name("teamcamel.stopping")       # local: why the next stop happens


def _note_stop(why):
    """Tells the copy about to be stopped why, for its last word to the other computers."""
    try:
        path = _stop_note_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"why": why, "at": now_ms()}), encoding="utf-8")
    except OSError:
        pass


def _stop_reason():
    """Why this copy is being stopped: a fresh note left by --stop or --uninstall, else None."""
    try:
        d = read_json(_stop_note_path())
        _stop_note_path().unlink()
    except (OSError, ValueError):
        return None
    if isinstance(d, dict) and isinstance(d.get("why"), str) and now_ms() - (d.get("at") or 0) < 120000:
        return d["why"]
    return None


def stop(port, why="stopped with --stop"):
    """Stop the monitor running on this computer, the login item's copy too, until the next login
    or Claude session. True when none is left running."""
    _note_stop(why)
    if stop_login_item():
        for _ in range(25):
            if not is_running(port):
                break
            time.sleep(0.2)
    info = monitor_info(port)
    if info is None:
        print("TeamCamel is not running." if not login_item_installed() else "TeamCamel stopped.")
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
    is fed to the model) and returns at once. Where the login item is installed, it is
    the one woken, so that no second copy runs beside it.
    """
    if not is_running(port) and not start_login_item():
        spawn_background(["--port", str(port)])


# --- starting at login -----------------------------------------------------------
#
# A login item starts TeamCamel as soon as you log in (on a Mac with FileVault, that is the
# moment you unlock it after a restart: nothing of yours runs before), and starts it again if it
# ever stops. macOS: a LaunchAgent; Linux: a systemd user service; Windows: the Startup folder.
#
# On a Mac, a job started at login may open files in a cloud-storage folder (Dropbox, OneDrive,
# iCloud Drive...) only if it is an app you have allowed: macOS turns a bare script away without
# asking. So there the LaunchAgent starts a small helper app, built on that Mac from HELPER_SOURCE
# below, which starts teamcamel.py and waits for it; macOS asks once whether "TeamCamel" may use
# the folder. The app's Info.plist names the Python and the teamcamel.py it starts, nothing else.

LOGIN_LABEL = "io.github.volkovcl.teamcamel"
LOGIN_WAIT = 5                 # seconds between looks while another copy holds the page
LOGIN_READY_WAIT = 15          # seconds the login item's copy gets to start
LOGIN_ALLOW_WAIT = 600         # ... or, on a Mac, for you to answer macOS's question about the folder

HELPER_SOURCE = r"""/* TeamCamel helper: teamcamel.py --start-at-login builds this small app on your Mac.
 *
 * macOS lets a program started at login open files in a cloud-storage folder (Dropbox,
 * OneDrive, iCloud Drive...) only if it is an app you have allowed. So the login item starts
 * this app, and the app starts teamcamel.py (the two paths in its Info.plist), waits for it,
 * and passes on a request to stop. It does nothing else. */
#include <CoreFoundation/CoreFoundation.h>
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>

extern char **environ;
static pid_t child = 0;

static void pass_on(int sig) {
    if (child > 0)
        kill(child, sig);
}

/* A path from this app's own Info.plist. */
static char *setting(const char *key) {
    CFBundleRef app = CFBundleGetMainBundle();
    CFStringRef name = CFStringCreateWithCString(NULL, key, kCFStringEncodingUTF8);
    CFTypeRef value = app && name ? CFBundleGetValueForInfoDictionaryKey(app, name) : NULL;
    if (name)
        CFRelease(name);
    if (!value || CFGetTypeID(value) != CFStringGetTypeID())
        return NULL;
    CFIndex size = CFStringGetMaximumSizeOfFileSystemRepresentation((CFStringRef)value);
    char *path = malloc(size);
    if (path && CFStringGetFileSystemRepresentation((CFStringRef)value, path, size))
        return path;
    free(path);
    return NULL;
}

int main(int argc, char *argv[]) {
    char *python = setting("TeamCamelPython"), *script = setting("TeamCamelScript");
    char **args = calloc(argc + 3, sizeof *args);
    if (!python || !script || !args) {
        fputs("TeamCamel helper: its Info.plist does not say what to start\n", stderr);
        return 1;
    }
    args[0] = python;
    args[1] = script;
    args[2] = "--login-item";
    for (int i = 1; i < argc; i++)          /* such as --port 2264 */
        args[i + 2] = argv[i];

    struct sigaction pass;
    memset(&pass, 0, sizeof pass);
    pass.sa_handler = pass_on;
    sigset_t stops, before;
    sigemptyset(&stops);
    sigaddset(&stops, SIGTERM);
    sigaddset(&stops, SIGINT);
    sigaddset(&stops, SIGHUP);
    sigprocmask(SIG_BLOCK, &stops, &before);  /* a stop that comes while starting waits for the child */
    sigaction(SIGTERM, &pass, NULL);
    sigaction(SIGINT, &pass, NULL);
    sigaction(SIGHUP, &pass, NULL);
    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
    posix_spawnattr_setsigmask(&attr, &before);
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGMASK);
    int err = posix_spawn(&child, python, NULL, &attr, args, environ);
    sigprocmask(SIG_SETMASK, &before, NULL);
    if (err) {
        fprintf(stderr, "TeamCamel helper: cannot start %s: %s\n", python, strerror(err));
        return 1;
    }
    int status = 0;
    while (waitpid(child, &status, 0) < 0)
        if (errno != EINTR)
            return 1;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
"""


def login_item_path():
    if MACOS:
        return HOME / "Library" / "LaunchAgents" / (LOGIN_LABEL + ".plist")
    if WINDOWS:
        base = Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming")
        return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "TeamCamel.cmd"
    return Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "systemd" / "user" / "teamcamel.service"


def helper_app():
    """Where the Mac helper app goes."""
    return HOME / "Library" / "Application Support" / "TeamCamel" / "TeamCamel.app"


def helper_program():
    return helper_app() / "Contents" / "MacOS" / "TeamCamel"


def _login_python():
    return "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable


def helper_info():
    """The helper app's Info.plist for this copy of TeamCamel."""
    info = {
        "CFBundleIdentifier": LOGIN_LABEL, "CFBundleName": "TeamCamel", "CFBundleDisplayName": "TeamCamel",
        "CFBundleExecutable": "TeamCamel", "CFBundlePackageType": "APPL", "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1", "LSUIElement": True,
        "TeamCamelPython": _login_python(), "TeamCamelScript": str(SELF),
        "TeamCamelNote": "Built on this Mac by teamcamel.py --start-at-login. It starts TeamCamelScript with "
                         "TeamCamelPython, and nothing else. Its source: Contents/Resources/helper.c.",
    }
    place = protected_place()
    if place:   # the reason macOS shows when it asks
        info[place[2]] = ("TeamCamel runs from this folder and keeps its shared files in it, so that your "
                          "computers can see each other's sessions.")
    return info


def helper_script():
    """The teamcamel.py that the helper app on this computer starts, or None."""
    import plistlib
    try:
        with open(str(helper_app() / "Contents" / "Info.plist"), "rb") as f:
            script = plistlib.load(f).get("TeamCamelScript")
    except (OSError, ValueError, AttributeError):
        return None
    return script if isinstance(script, str) else None


def build_helper_app():
    """Builds the Mac helper app for this copy of TeamCamel, unless it is already there just as it
    would be built (so that macOS's permission for it stays). None when done, else why not."""
    import plistlib
    import tempfile
    app = helper_app()
    info, source = plistlib.dumps(helper_info()), HELPER_SOURCE.encode("utf-8")
    try:
        if ((app / "Contents" / "Info.plist").read_bytes() == info
                and (app / "Contents" / "Resources" / "helper.c").read_bytes() == source
                and helper_program().is_file() and _run_quiet(["codesign", "--verify", str(app)]) == 0):
            return None
    except OSError:
        pass
    if _run_quiet(["xcode-select", "-p"]) != 0:
        # Without them, calling the compiler would only open an offer to install them.
        return "this Mac lacks Apple's command line tools; to add them: xcode-select --install"
    work = None
    try:
        work = Path(tempfile.mkdtemp(prefix="teamcamel-app-"))
        new = work / "TeamCamel.app"
        (new / "Contents" / "MacOS").mkdir(parents=True)
        (new / "Contents" / "Resources").mkdir()
        (new / "Contents" / "Info.plist").write_bytes(info)
        (new / "Contents" / "Resources" / "helper.c").write_bytes(source)
        for step, cmd in (("compiling", ["xcrun", "clang", "-Os", "-Wall", "-framework", "CoreFoundation", "-o",
                                         str(new / "Contents" / "MacOS" / "TeamCamel"),
                                         str(new / "Contents" / "Resources" / "helper.c")]),
                          ("signing", ["codesign", "--force", "--sign", "-", "--identifier", LOGIN_LABEL, str(new)])):
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if done.returncode != 0:
                return "%s failed: %s" % (step, (done.stderr or done.stdout).strip()[:400])
        if app.exists():
            shutil.rmtree(str(app))
        app.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(new), str(app))
        return None
    except (OSError, subprocess.SubprocessError) as err:
        return str(err)
    finally:
        if work:
            shutil.rmtree(str(work), ignore_errors=True)


def remove_helper_app():
    """Takes this copy's helper app away. True when there was one."""
    if not MACOS or helper_script() != str(SELF):
        return False
    shutil.rmtree(str(helper_app()), ignore_errors=True)
    try:
        helper_app().parent.rmdir()          # the TeamCamel folder, when nothing else is in it
    except OSError:
        pass
    return not helper_app().exists()


def protected_place():
    """When this copy sits in a folder that macOS keeps from a program started at login until you
    allow it: (what macOS asks about, its privacy service, the Info.plist key for the reason). Else None."""
    lib = HOME / "Library"
    for base, name in ((lib / "CloudStorage", None), (lib / "Mobile Documents", "iCloud Drive"),
                       (HOME / "Desktop", "Desktop"), (HOME / "Documents", "Documents"),
                       (HOME / "Downloads", "Downloads")):
        try:
            part = SELF.relative_to(base).parts[0]
        except (ValueError, IndexError):
            continue
        if name in ("Desktop", "Documents", "Downloads"):
            return ("files in your %s folder" % name, "SystemPolicy%sFolder" % name, "NS%sFolderUsageDescription" % name)
        if name is None:
            service = part.split("-", 1)[0]       # Dropbox-Personal, OneDrive-Contoso, GoogleDrive-me@...
            name = {"GoogleDrive": "Google Drive"}.get(service, service) or "a cloud-storage service"
        return ("files managed by %s" % name, "FileProviderDomain", "NSFileProviderDomainUsageDescription")
    return None


def login_item_text(port=PORT, helper=False):
    extra = [] if port == PORT else ["--port", str(port)]
    if MACOS:
        import plistlib
        job = {"Label": LOGIN_LABEL,
               "ProgramArguments": ([str(helper_program())] if helper else [_login_python(), str(SELF), "--login-item"]) + extra,
               "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 15, "ProcessType": "Background",
               "WorkingDirectory": str(HOME), "StandardOutPath": str(log_path()), "StandardErrorPath": str(log_path())}
        if helper:
            job["AssociatedBundleIdentifiers"] = [LOGIN_LABEL]   # shown as "TeamCamel" in Login Items
        return plistlib.dumps(job).decode("utf-8")
    if WINDOWS:
        quiet = Path(sys.executable).with_name("pythonw.exe")
        py = str(quiet if quiet.exists() else Path(sys.executable))
        return '@echo off\r\nstart "" "%s" "%s" --ensure%s\r\n' % (py, SELF, "".join(" " + a for a in extra))
    return ("[Unit]\nDescription=TeamCamel session monitor\n\n[Service]\n"
            "ExecStart=\"%s\" \"%s\" --login-item%s\nRestart=always\nRestartSec=15\n\n"
            "[Install]\nWantedBy=default.target\n" % (sys.executable, SELF, "".join(" " + a for a in extra)))


def _login_item_file():
    try:
        return login_item_path().read_text(encoding="utf-8")
    except OSError:
        return ""


def login_item_uses_helper():
    """True when this copy's login item starts it through the Mac helper app."""
    return MACOS and str(helper_program()) in _login_item_file() and helper_script() == str(SELF)


def login_item_installed():
    """True when this copy of TeamCamel has its login item."""
    return str(SELF) in _login_item_file() or login_item_uses_helper()


def _run_quiet(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def _gui():
    return "gui/%d" % os.getuid()


def login_ready_path():
    return log_path().with_name("teamcamel.login-item.json")


def note_login_item_started():
    """As the login item's copy: says it has started (so it could open its own files)."""
    try:
        path = login_ready_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid(), "at": now_ms(), "script": str(SELF),
                                    "version": __version__}), encoding="utf-8")
    except OSError:
        pass


def login_item_ready(since, wait, hint=None, sleep=time.sleep):
    """True once the login item's copy says it started (after `since`, in ms), looked for once a
    second for up to `wait` seconds. `hint` is printed if it has not started after a few seconds."""
    for second in range(int(wait) + 1):
        try:
            d = read_json(login_ready_path())
        except (OSError, ValueError):
            d = None
        if isinstance(d, dict) and d.get("script") == str(SELF) and (d.get("at") or 0) >= since:
            return True
        if hint and second == 4:
            print(hint, flush=True)
        if second < int(wait):
            sleep(1)
    return False


def install_login_item(port=PORT):
    """Writes the login item and starts it. True when it is in place and its copy has started."""
    path, command = login_item_path(), "py teamcamel.py" if WINDOWS else "python3 teamcamel.py"
    place, helper = protected_place() if MACOS else None, False
    if MACOS:
        print("To start TeamCamel when you log in, this adds two things to this Mac:\n"
              "  - a small helper app, built here from the source inside it:\n      %s\n"
              "    (macOS lets an app, but not a bare script started at login, into a cloud-storage folder\n"
              "    such as Dropbox once you allow it; the app starts this teamcamel.py and nothing else)\n"
              "  - a login item that starts the app:\n      %s" % (helper_app(), path), flush=True)
        why = build_helper_app()
        helper = why is None
        if not helper:
            print("The helper app could not be built (%s).\nTrying without it." % why, flush=True)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(login_item_text(port, helper), encoding="utf-8")
    except OSError as err:
        remove_helper_app()
        print("Could not add the login item (%s): TeamCamel still starts with every Claude session." % err)
        return False
    since = now_ms()
    if MACOS:
        _run_quiet(["launchctl", "bootout", "%s/%s" % (_gui(), LOGIN_LABEL)])
        _run_quiet(["launchctl", "bootstrap", _gui(), str(path)])
    elif not WINDOWS:
        _run_quiet(["systemctl", "--user", "daemon-reload"])
        _run_quiet(["systemctl", "--user", "enable", "--now", "teamcamel.service"])
    if not WINDOWS:
        asks = helper and place is not None
        hint = ('macOS should now ask, on this Mac\'s screen, whether "TeamCamel" may access\n'
                '%s. Click Allow. Waiting for the answer (up to %d minutes; Ctrl-C gives up)...'
                % (place[0], LOGIN_ALLOW_WAIT // 60)) if asks else None
        try:
            ready = login_item_ready(since, LOGIN_ALLOW_WAIT if asks else LOGIN_READY_WAIT, hint)
        except KeyboardInterrupt:
            ready = False
        if not ready:
            remove_login_item(quiet=True)
            print("\nThe login item's copy of TeamCamel did not start, so the login item%s was taken away again.\n"
                  "Nothing else changed: TeamCamel runs as before, starting with every Claude session." % (
                      " and the helper app" if helper else ""))
            if asks:
                print("If macOS asked and the answer was Don't Allow, macOS remembers it and will not ask again.\n"
                      "To be asked again, run:  tccutil reset %s %s\n"
                      "and then this command again. If macOS did not ask at all, the log may say why:\n  %s"
                      % (place[1], LOGIN_LABEL, log_path()))
            elif place:
                print("Without the helper app, macOS lets a bare script started at login open %s\n"
                      "only with a permission it cannot ask for. The log may say: Operation not permitted.\n  %s"
                      % (place[0], log_path()))
            else:
                print("The log may say why:\n  %s" % log_path())
            return False
    if helper:
        print("Login item added. It starts the TeamCamel helper app, which starts TeamCamel:\n"
              "TeamCamel now also starts when you log in, and again if it stops.\n"
              "To take both away:  %s --no-start-at-login" % command)
    else:
        print("Login item added: %s\nTeamCamel now also starts when you log in, and again if it stops.\n"
              "To take it away:  %s --no-start-at-login" % (path, command))
    return True


def start_login_item():
    """Wakes the login item's copy (loading it again after --stop). True when that was done."""
    if not login_item_installed():
        return False
    if MACOS:
        if _run_quiet(["launchctl", "kickstart", "%s/%s" % (_gui(), LOGIN_LABEL)]) == 0:
            return True
        return _run_quiet(["launchctl", "bootstrap", _gui(), str(login_item_path())]) == 0
    if WINDOWS:
        return False
    return _run_quiet(["systemctl", "--user", "start", "teamcamel.service"]) == 0


def stop_login_item():
    """Stops the login item's copy until the next login (or Claude session). True when done."""
    if not login_item_installed():
        return False
    if MACOS:
        return _run_quiet(["launchctl", "bootout", "%s/%s" % (_gui(), LOGIN_LABEL)]) == 0
    if WINDOWS:
        return False
    return _run_quiet(["systemctl", "--user", "stop", "teamcamel.service"]) == 0


def remove_login_item(quiet=False):
    """Takes the login item away, and on a Mac its helper app. True when there was one for this copy."""
    if not login_item_installed():
        return False
    stop_login_item()
    if not MACOS and not WINDOWS:
        _run_quiet(["systemctl", "--user", "disable", "teamcamel.service"])
    try:
        login_item_path().unlink()
    except OSError:
        pass
    app = remove_helper_app()
    if not quiet:
        print("Login item removed: %s." % login_item_path())
        if app:
            print("Helper app removed: %s." % helper_app())
    return True


def wait_for_port(port, sleep=time.sleep):
    """As the login item's copy: while another TeamCamel already shows the page, stand by and
    take over if it stops (so that two never run, and one always does)."""
    said = False
    while is_running(port):
        if not said:
            print("%s TeamCamel already runs on port %d; standing by" % (stamp(), port), flush=True)
            said = True
        sleep(LOGIN_WAIT)


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
    _note_stop("taken out with --uninstall")
    remove_login_item()
    if stop(port, "taken out with --uninstall"):
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
    own, shared = own_settings(node_name()), shared_settings()
    chosen = (own if own is not None else shared or {}).get("refresh")
    line("Refresh", "every %d seconds (%s)" % (chosen or COLLECT_EVERY, "set for this computer" if own is not None and chosen
                                               else "set for all computers" if chosen else "the usual"))
    apps = app_versions()
    line("Versions", "Claude Code %s; Codex %s" % (apps.get("claude") or "not found", apps.get("codex") or "not found"))
    procs = marked_processes(process_table()) or []
    try:
        procs += named_programs(_last_registry, desktop_sessions(), skip={p["pid"] for p in procs})
    except Exception:
        pass
    commands = {pid: cmd for pid, _, cmd in command_lines()} if procs else {}
    for p in procs:
        if p.get("via") != "named":
            p["program"] = program_name(p["program"], commands.get(p["pid"], ""))
    busy_processes(procs)
    time.sleep(5)
    busy = busy_processes(procs)
    line("Programs from sessions", "; ".join(
        "%s: %s (processor %.0f%%, %s)%s" % (p["program"], "working" if p in busy else "idle", p["cpu"],
                                              "%.0f KB a minute moved" % (_activity.get((p["pid"], p["since"]), {}).get("rate", 0) / 1024),
                                              ", its file named by a session" if p.get("via") == "named" else "")
        for p in procs) or "none running")
    line("Stopping", "Windows: not offered" if WINDOWS else
         "from this computer's page; other computers may ask too" if effective_settings(node_name()).get("stopFromOthers")
         else "from this computer's page only")
    line("Monitor", "running at http://127.0.0.1:%d/" % port if is_running(port) else "not running")
    try:
        ours = our_hooks(read_json(SETTINGS))[1] if SETTINGS.exists() else []
    except (OSError, ValueError):
        ours = []
    want = hook_entry(port)["command"]
    line("Start hook", "installed" if any(h.get("command") == want for h in ours)
         else "installed, but for another location: run --install-hook" if ours
         else "not installed: run --install-hook")
    line("Login item", "installed: starts TeamCamel when you log in, through its helper app (%s)" % helper_app()
         if login_item_uses_helper() else "installed: starts TeamCamel when you log in (%s)" % login_item_path()
         if login_item_installed() else "none (optional: --start-at-login starts TeamCamel when you log in)")
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


def install_goodbye(node):
    """When this copy is stopped (a shutdown, a restart, a log-out, or --stop), it leaves a last word
    in its report, so the other computers say "stopped at 9:23 PM" rather than "not reporting"."""
    def goodbye(signum, frame):
        why = _stop_reason()
        if why != "handover":
            node.goodbye(why or "shutting down, restarting or logging out")
        os._exit(0)
    try:
        signal.signal(signal.SIGTERM, goodbye)
    except (ValueError, OSError, AttributeError):
        pass


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
    ap.add_argument("--start-at-login", action="store_true",
                    help="optional: also start TeamCamel when you log in, and again if it stops (adds a login item)")
    ap.add_argument("--no-start-at-login", action="store_true", help="take that login item away again")
    ap.add_argument("--login-item", action="store_true", help=argparse.SUPPRESS)   # how the login item starts it
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
        if not login_item_installed():
            print("\nOptional: TeamCamel can also start by itself when you log in, so that its page is up\n"
                  "after a restart before any Claude session. That adds a login item to this computer:\n"
                  "  %s\n%s"
                  "Only if you want it:  %s --start-at-login   (take it away with --no-start-at-login)"
                  % (login_item_path(), "and, on a Mac, a small helper app built here, which the login item starts:\n"
                     "  %s\n" % helper_app() if MACOS else "", "py teamcamel.py" if WINDOWS else "python3 teamcamel.py"))
        return
    if args.start_at_login:
        # The copy already running keeps the page; the login item's copy stands by, and takes over
        # when that one ends (at the latest at the next restart). Nothing is stopped.
        install_login_item(args.port)
        if not is_running(args.port):
            ensure_running(args.port)
        return
    if args.no_start_at_login:
        if not remove_login_item():
            print("No login item for this copy of TeamCamel. Nothing was changed.")
        ensure_running(args.port)                # the page stays up, started the usual way
        return
    if args.check:
        check(args.port)
        return
    if args.stop:
        sys.exit(0 if stop(args.port) else 1)
    if args.uninstall:
        uninstall(args.port)
        return

    if args.login_item:
        note_login_item_started()
        wait_for_port(args.port)
    node = Node()
    if args.once:
        print(json.dumps(node.snapshot, indent=1))
        return
    install_goodbye(node)
    if args.no_serve:
        node.run(args.interval)
        return

    while True:
        try:
            server = open_server(args.port, make_handler(node))
            break
        except OSError as err:
            if not args.login_item:
                sys.exit("teamcamel: cannot listen on 127.0.0.1:%d (%s). Is it already running?" % (args.port, err))
            # The login item's copy never gives up (it would only be started again): it waits.
            print("%s port %d is taken (%s); trying again in a minute" % (stamp(), args.port, err), flush=True)
            time.sleep(60)
            wait_for_port(args.port)
    threading.Thread(target=node.run, args=(args.interval,), daemon=True).start()
    print("%s TeamCamel %s on node '%s' (%s): http://127.0.0.1:%d/ (pid %d)"
          % (stamp(), __version__, node.name, node.os, args.port, os.getpid()), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
