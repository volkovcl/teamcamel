#!/usr/bin/env python3
"""Builds what is handed to other people. Run it from anywhere:

    python3 packaging/make_package.py --tester
    python3 packaging/make_package.py --public

--tester   The friend / tester package, dist/TeamCamel-<version>-tester.zip: README.txt,
           both precheck scripts, the program and its page. It is the running program
           unchanged, except that the footer shows only the logo and the version, without
           the copyright line.

--public   The public release, with the full footer:
             dist/TeamCamel-<version>.zip   what people download: README.txt, LICENSE.txt,
                                            CHANGELOG.txt, both precheck scripts, the
                                            program and its page
             dist/github/                   the contents of the public repository: the
                                            above plus README.md, the logo sources, the
                                            tests and this packaging folder

--public also writes packaging/HOW-TO-PUBLISH.md, the author's step-by-step guide, with the
current version number filled in.

Neither ever contains the state folder (this computer's own sessions) or the inputs
folder. Everything written is checked afterwards: the list of files, the footer, the
absence of this computer's session names and private words, and a run of the unpacked copy.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP = "TeamCamel"                                   # the one folder a zip unpacks to
CREDIT = re.compile(r'<span id="credit">.*?</span>')

# The public repository: exactly these files, nothing found by searching the folder.
REPOSITORY = [
    "README.md", "LICENSE.txt", "CHANGELOG.txt", ".gitignore",
    "teamcamel.py", "precheck.sh", "precheck.bat", "web/index.html",
    "docs/banner.svg", "docs/screenshot.png",
    "logo/teamcamel-logo.svg", "logo/camel-m.svg", "logo/build_logo.py", "logo/outline.swift",
    "tests/test_teamcamel.py", "tests/linux_fixture.py", "tests/linux_e2e.sh",
    "packaging/make_package.py", "packaging/make_screenshot.py",
    "packaging/README-tester.txt", "packaging/README-public.txt",
]
EXECUTABLE = {"precheck.sh"}


def version():
    text = (ROOT / "teamcamel.py").read_text(encoding="utf-8")
    return re.search(r'^__version__ = "([^"]+)"', text, re.M).group(1)


def page():
    text = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    if len(CREDIT.findall(text)) != 1:
        sys.exit("make_package: the page's footer has no single copyright element")
    return text


def readme(name, ver):
    text = (ROOT / "packaging" / name).read_text(encoding="utf-8").replace("{version}", ver)
    home = ""
    try:
        home = (ROOT / "packaging" / "home-url.txt").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    line = "- Newer versions, and where to report a problem: %s\n" % home if home else ""
    return text.replace("{home}\n", line).encode("utf-8")


def tester_files(ver):
    """(name in the zip, content) for the tester package."""
    return [
        ("README.txt", readme("README-tester.txt", ver)),
        ("precheck.sh", (ROOT / "precheck.sh").read_bytes()),
        ("precheck.bat", (ROOT / "precheck.bat").read_bytes()),
        ("teamcamel.py", (ROOT / "teamcamel.py").read_bytes()),
        ("web/index.html", CREDIT.sub("", page()).encode("utf-8")),
    ]


def public_files(ver):
    """(name in the zip, content) for the public download."""
    return [
        ("README.txt", readme("README-public.txt", ver)),
        ("LICENSE.txt", (ROOT / "LICENSE.txt").read_bytes()),
        ("CHANGELOG.txt", (ROOT / "CHANGELOG.txt").read_bytes()),
        ("precheck.sh", (ROOT / "precheck.sh").read_bytes()),
        ("precheck.bat", (ROOT / "precheck.bat").read_bytes()),
        ("teamcamel.py", (ROOT / "teamcamel.py").read_bytes()),
        ("web/index.html", page().encode("utf-8")),
    ]


def write_zip(path, files):
    now = time.localtime()[:6]
    path.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            info = zipfile.ZipInfo(TOP + "/" + name, date_time=now)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3                                  # Unix, so the permission bits are kept
            info.external_attr = (0o100755 if name in EXECUTABLE else 0o100644) << 16
            z.writestr(info, data)


def must_not_appear():
    """Texts that may never reach a package: this computer's session titles and private words."""
    words = set()
    for p in (ROOT / "state").glob("*.json"):
        try:
            for s in json.loads(p.read_bytes()).get("sessions", []):
                if len(str(s.get("title", ""))) >= 15:
                    words.add(s["title"])
        except (OSError, ValueError, AttributeError):
            pass
    try:   # one per line; this file stays out of every package
        lines = (ROOT / "packaging" / "private-words.txt").read_text(encoding="utf-8").splitlines()
        words.update(w.strip() for w in lines if w.strip() and not w.startswith("#"))
    except OSError:
        pass
    return words


def leaks(texts):
    everything = "\n".join(t.decode("utf-8", "replace") for t in texts).lower()
    return sorted(w for w in must_not_appear() if w.lower() in everything)


def check_zip(path, files, ver, credit):
    problems = []
    with zipfile.ZipFile(path) as z:
        names = sorted(z.namelist())
        if names != sorted(TOP + "/" + n for n, _ in files):
            problems.append("unexpected list of files: %s" % names)
        if z.testzip():
            problems.append("the zip is damaged")
        html = z.read(TOP + "/web/index.html").decode("utf-8")
        has_credit = "©" in html and 'id="credit"' in html
        if has_credit != credit:
            problems.append("the footer %s the copyright line" % ("lacks" if credit else "still has"))
        for need in ('<svg class="logo"', '<span id="ver"></span>'):
            if need not in html:
                problems.append("the page lacks %s" % need)
        if z.read(TOP + "/teamcamel.py") != (ROOT / "teamcamel.py").read_bytes():
            problems.append("the program differs from the running one")
        if any("state" in n.split("/") or n.endswith(".json") for n in names):
            problems.append("a state or data file is in the zip")
        found = leaks(z.read(n) for n in names)
        if found:
            problems.append("private text in the zip: %s" % found[:5])
        if credit:   # the public download: the sale restriction must travel with it
            terms = z.read(TOP + "/LICENSE.txt").decode("utf-8")
            if "Commons Clause" not in terms or "MIT License" not in terms:
                problems.append("LICENSE.txt is not the MIT License with the Commons Clause")
            if "Commons Clause" not in z.read(TOP + "/README.txt").decode("utf-8"):
                problems.append("README.txt does not name the license terms")
        if not (z.getinfo(TOP + "/precheck.sh").external_attr >> 16) & 0o100:
            problems.append("precheck.sh is not marked executable")
        with tempfile.TemporaryDirectory() as tmp:
            z.extractall(tmp)
            prog = str(Path(tmp) / TOP / "teamcamel.py")
            said = subprocess.run([sys.executable, prog, "--version"], capture_output=True, text=True, timeout=120)
            if said.stdout.strip() != "TeamCamel " + ver:
                problems.append("the unpacked copy reports %r" % said.stdout.strip())
            pre = subprocess.run([sys.executable, prog, "--precheck", "--port", "47399"],
                                 capture_output=True, text=True, timeout=180)
            if "TeamCamel %s precheck" % ver not in pre.stdout or "Result:" not in pre.stdout:
                problems.append("the precheck of the unpacked copy did not run: %s" % (pre.stderr or pre.stdout)[-200:])
    return problems


def write_repository(folder):
    problems = []
    if folder.exists():
        shutil.rmtree(folder)
    for name in REPOSITORY:
        src = ROOT / name
        if not src.is_file():
            problems.append("missing: %s" % name)
            continue
        dst = folder / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
        dst.chmod(0o755 if name in EXECUTABLE else 0o644)
    written = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())
    if written != sorted(REPOSITORY):
        problems.append("unexpected files in the repository folder: %s" % sorted(set(written) ^ set(REPOSITORY)))
    texts = [(folder / n).read_bytes() for n in written if not n.endswith(".png")]
    found = leaks(texts)
    if found:
        problems.append("private text in the repository folder: %s" % found[:5])
    return problems


def report(title, files, path):
    print(title)
    for name, data in files:
        print("  %-16s %6d bytes" % (name, len(data)))
    print("  -> %s  (%d bytes, sha256 %s)" % (path.relative_to(ROOT), path.stat().st_size,
                                             hashlib.sha256(path.read_bytes()).hexdigest()[:16]))


def main():
    ap = argparse.ArgumentParser(description="Build a TeamCamel package")
    kind = ap.add_mutually_exclusive_group(required=True)
    kind.add_argument("--tester", action="store_true", help="the friend / tester zip (footer without the copyright line)")
    kind.add_argument("--public", action="store_true", help="the public download and the public repository folder")
    args = ap.parse_args()
    ver = version()
    dist = ROOT / "dist"

    if args.tester:
        files, path = tester_files(ver), dist / ("TeamCamel-%s-tester.zip" % ver)
        write_zip(path, files)
        problems = check_zip(path, files, ver, credit=False)
        if problems:
            path.unlink()
            sys.exit("NOT built:\n  " + "\n  ".join(problems))
        report("TeamCamel %s tester package" % ver, files, path)
        print("checked: file list, footer without the copyright line, no private text, unpacked copy runs")
        return

    files, path, folder = public_files(ver), dist / ("TeamCamel-%s.zip" % ver), dist / "github"
    write_zip(path, files)
    problems = check_zip(path, files, ver, credit=True) + write_repository(folder)
    if problems:
        path.unlink()
        shutil.rmtree(folder, ignore_errors=True)
        sys.exit("NOT built:\n  " + "\n  ".join(problems))
    guide = (ROOT / "packaging" / "HOW-TO-PUBLISH.template.md").read_text(encoding="utf-8")
    guide = guide.replace("{version}", ver).replace("{files}", str(len(REPOSITORY)))
    (ROOT / "packaging" / "HOW-TO-PUBLISH.md").write_text(guide, encoding="utf-8")
    report("TeamCamel %s public download" % ver, files, path)
    print("TeamCamel %s public repository folder" % ver)
    print("  -> %s  (%d files)" % (folder.relative_to(ROOT), len(REPOSITORY)))
    print("checked: file lists, full footer, license terms, no private text, unpacked copy runs")
    print("your steps: packaging/HOW-TO-PUBLISH.md")


if __name__ == "__main__":
    main()
