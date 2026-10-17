# Live run of TeamCamel on a Linux machine that has no Claude Code.
#
# On the Linux machine, with the TeamCamel folder copied to ~/tc-test:
#     python3 ~/tc-test/tests/linux_fixture.py              (stand-in Claude files, real processes)
#     cd ~/tc-test && python3 teamcamel.py --install-hook --port 2270
#     bash ~/tc-test/tests/linux_e2e.sh
# To see another computer on the Linux page, copy that computer's state/<name>.json
# into ~/tc-test/state/ first. Afterwards remove ~/tc-test, ~/.claude, ~/.config/Claude
# and ~/.local/state/teamcamel from the test machine.
PORT=2270
LOG=~/.local/state/teamcamel/teamcamel.log
mon() { pgrep -f '[t]eamcamel.py --port '"$PORT"; }          # [t] keeps pgrep from matching itself
http() { curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/$1"; }
nodes() { curl -s -m 5 "http://127.0.0.1:$PORT/api/state" | python3 -c '
import json, sys
for n in json.load(sys.stdin)["nodes"]:
    print("  node %-8s system %-20s TeamCamel %-4s %d sessions, %d needing you" % (
        n["node"], n.get("os"), n.get("version"), len(n["sessions"]), sum(s["attention"] for s in n["sessions"])))'; }

echo "===== the start hook, run exactly as stored (monitor stopped first) ====="
for p in $(mon); do kill "$p"; done; sleep 1
echo "monitor copies before: $(mon | wc -l)"
CMD=$(python3 -c 'import json, os; print(json.load(open(os.path.expanduser("~/.claude/settings.json")))["hooks"]["SessionStart"][0]["hooks"][0]["command"])')
echo "stored command: $CMD"
OUT=$(echo '{}' | sh -c "$CMD"); echo "hook exit=$? stdout_bytes=${#OUT}"
sleep 2
P=$(mon); echo "monitor pid=$P parent=$(ps -o ppid= -p "$P" | tr -d ' ') (1 = detached from what started it); page HTTP $(http)"
echo '{}' | sh -c "$CMD"; sleep 1
echo "copies after a second hook run: $(mon | wc -l)"

echo; echo "===== Linux shows a Mac (this Mac's real report is in the Linux folder) ====="
sleep 16; nodes

echo; echo "===== restarts itself when the program file changes ====="
N0=$(wc -l < "$LOG"); P=$(mon); touch ~/tc-test/teamcamel.py
for i in $(seq 1 30); do tail -n +$((N0 + 1)) "$LOG" | grep -q 'changed; restarting' && break; sleep 2; done
tail -n +$((N0 + 1)) "$LOG"; sleep 2
echo "pid before $P, after $(mon); page HTTP $(http); data HTTP $(http api/state)"

echo; echo "===== requests from another address are refused ====="
echo "Host header of another machine: HTTP $(curl -s -m 5 -o /dev/null -w '%{http_code}' -H 'Host: 192.168.1.5' "http://127.0.0.1:$PORT/api/state")"
echo "listening on: $(ss -ltn 2>/dev/null | grep ":$PORT " | awk '{print $4}')"

echo; echo "===== --check ====="
cd ~/tc-test && python3 teamcamel.py --check --port $PORT
