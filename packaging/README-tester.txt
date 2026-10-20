TeamCamel {version} - test copy

TeamCamel is one web page that shows which of your Claude Code sessions need
you: awaiting your input, interrupted, still working, or completed. If you
also use Codex, its sessions are on the same page. It reads Claude's and
Codex's files on your own computer only. It uses no tokens and sends nothing
anywhere.


YOU NEED

- macOS, Windows or Linux
- Claude Code, used at least once: the Code tab of the Claude desktop app, or
  the "claude" command in a terminal
- Python 3.9 or newer (the precheck tells you if it is missing)


SET UP

The easy way: double-click web/index.html inside the TeamCamel folder. It
shows these steps in your browser, with the commands for your folder ready to
copy, and tells you when TeamCamel is running.

1. Unzip, and put the TeamCamel folder where it can stay, for example in your
   home folder. Keep its files together.
   To see several computers on one page, put it instead inside a folder that
   syncs between them (Dropbox, iCloud Drive, OneDrive, Google Drive), use
   that same folder on every computer, and do steps 2 and 3 on each. Each
   computer then shows all of them.

2. Precheck. It changes nothing and tells you if anything is missing.

   macOS / Linux   Open Terminal, type "cd " (with the space), drag the
                   TeamCamel folder onto the Terminal window, press Return.
                   Then type:

                       sh precheck.sh

   Windows         Double-click precheck.bat

3. Install and start. This is done once.

   macOS / Linux   In the same Terminal window:

                       python3 teamcamel.py --install-hook

   Windows         Open the TeamCamel folder in File Explorer, click the
                   address bar, type cmd and press Enter. Then type:

                       python teamcamel.py --install-hook

                   (if "python" is not found, type "py" in its place)

   This adds one start-up line to Claude's settings file, after saving a
   backup of that file, so that TeamCamel starts by itself with every Claude
   session. It also starts TeamCamel right away.

4. Open the page in your browser and bookmark it:

       http://127.0.0.1:2263/


READING THE PAGE

- Red: the session awaits your input, or was interrupted. Blue: working.
  Gray: completed.
- "Working in background" means Claude's turn is over but work it started
  still runs: a command or a helper agent sent to the background, or a
  program the session started that is still busy, such as a render. It turns
  to completed when that work ends.
- The number after your computer's name counts the sessions that need you. It
  also shows at the front of the browser tab's title.
- ". . ." unfolds the older sessions of a workspace.
- When you have both Claude and Codex sessions, each one carries a small
  "Claude" or "Codex" label.
- Click a session's name to open that session in its app. This works for
  sessions on the computer you are at.
- The page refreshes itself every 15 seconds.
- Under each computer's name: the Claude Code and Codex versions it runs.
  Green: the latest. Grey: not the latest. Red: may be outdated, that is
  behind a big version (for Claude Code the first two numbers, such as 2.1;
  for Codex, which is still at 0.x, four releases or more, about a month).
  Hover over a version to see the latest one.


SETTINGS

The gear at the top left opens the settings. They apply either to all your
computers or only to the computer you are at, in every browser on it; you
choose at the top of the panel. Both are kept in the shared TeamCamel folder.

- Computers: show or hide each one, and choose their order. The first one
  shown fills the left column.
- Show: sessions of Claude, Codex or both, and their versions; computers by
  operating system (macOS, Windows, Linux).
- Look up the latest versions online, once a day: off until you turn it on.
  The page then asks the public npm registry, where Claude Code and Codex are
  published, for their latest version numbers; nothing about you or your
  sessions is sent. While it is off, the latest version seen on your
  computers counts as latest.
- Colors: background, awaits input, working, completed. "Reset colors"
  brings back the usual ones.
- When a session starts needing you: a pop-up notice, a sound, a blinking
  tab title, in any combination ("Try it" shows what you chose). They work
  while the page is open in a browser tab, also when the tab is in the
  background.


OTHER COMMANDS

Type them the same way as in step 3 (python3 on macOS and Linux, python on
Windows).

    python3 teamcamel.py --check       what TeamCamel finds on this computer
    python3 teamcamel.py --stop        stop it until the next Claude session
    python3 teamcamel.py --uninstall   take it out of Claude's start-up and
                                       stop it; then delete the TeamCamel folder


TESTING NOTES

- It runs daily on macOS and has been run on Linux. On Windows nobody has run
  it yet: if you are on Windows, you are the first.
- New in this version: the gear at the top left (settings), and the
  versions of Claude Code and Codex under each computer's name. If you turn
  on notices, please say whether they reach you.
- Also new lately: "working in background". If a session shows as
  completed while something it started is still running, or as working
  after that work has ended, please say so.
- Double-clicking web/index.html shows the setup steps. If you set up from
  there, please say whether they worked as shown.
- If you use Codex, please check that what the page says about a Codex
  session matches what Codex shows.
- If something goes wrong or looks odd, please send back what the precheck
  prints and what "--check" prints. Both list counts and folder names only,
  never the names or contents of your sessions.
