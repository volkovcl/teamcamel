TeamCamel {version}

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
- ". . ." unfolds the older sessions of a workspace. A workspace ("ws") is
  the folder a session works in.
- When you have both Claude and Codex sessions, each one carries a small
  "Claude" or "Codex" label.
- Click a session's name to open that session in its app. This works for
  sessions on the computer you are at.
- The page refreshes itself every 15 seconds.


OTHER COMMANDS

Type them the same way as in step 3 (python3 on macOS and Linux, python on
Windows).

    python3 teamcamel.py --check       what TeamCamel finds on this computer
    python3 teamcamel.py --stop        stop it until the next Claude session
    python3 teamcamel.py --uninstall   take it out of Claude's start-up and
                                       stop it; then delete the TeamCamel folder



GOOD TO KNOW

- TeamCamel reads files that Claude Code keeps on your computer and serves
  its page to this computer only. It uses no tokens and contacts no online
  service. The one thing it changes is the start-up line added in step 3,
  which "--uninstall" takes out again.
- It shows Claude Code sessions that run on your computers, started from the
  Claude desktop app's Code tab or from a terminal. It does not show cloud
  sessions, ordinary claude.ai chats or Cowork sessions.
- Codex needs no setup: if it is installed, its sessions show. Codex
  publishes no live status, so TeamCamel works it out from the session's
  transcript. "Awaits input" is recognized when a command asks for wider
  permissions or Codex asks a question; another kind of prompt may show as
  "working". TeamCamel starts with Claude Code, so on a computer that has
  only Codex you start it yourself:  python3 teamcamel.py
- It relies on files whose form Anthropic and OpenAI may change with any
  update. If an update breaks it, look for a newer TeamCamel.
- Tested on macOS and Linux. Windows support is new and has not yet been
  confirmed on a real Windows computer.
- To show a name other than the computer's own, put that name in a one-line
  text file called "nodename" inside your .claude folder.
- TeamCamel is an independent project. It is not made or endorsed by
  Anthropic or OpenAI. Claude is a trademark of Anthropic; Codex is a
  product of OpenAI.
{home}

LICENSE

Free to use, copy, change and share. It may not be sold, nor may a product
or service whose value comes mainly from it. The terms are the MIT License
with the Commons Clause: see LICENSE.txt.
Copyright (c) 2026 Aleksandr A. Volkov. Version history: CHANGELOG.txt.
