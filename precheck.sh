#!/bin/sh
# TeamCamel precheck for macOS and Linux. Run it first:   sh precheck.sh
#
# Says whether anything TeamCamel needs is missing on this computer and what to
# do about it. It looks for Python here, then lets TeamCamel check the rest.
# Changes nothing on this computer.

cd "$(dirname "$0")" || exit 1

if [ ! -f teamcamel.py ]; then
    echo "TeamCamel precheck"
    echo "  [MISSING] teamcamel.py next to this script"
    echo "            -> download the complete TeamCamel folder again and run precheck.sh from inside it"
    echo "Result: 1 item missing."
    exit 1
fi

for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1 &&
       "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
        exec "$py" teamcamel.py --precheck "$@"
    fi
done

echo "TeamCamel precheck"
echo "  [MISSING] Python 3.9 or newer"
if [ "$(uname)" = "Darwin" ]; then
    echo "            -> in Terminal run:  xcode-select --install   (Apple's free Command Line Tools include Python)"
    echo "               or install Python from https://www.python.org/downloads/ , then run this again"
else
    echo "            -> install it with your system's package manager, for example:  sudo apt install python3"
    echo "               then run this again"
fi
echo "Result: 1 item missing. The other components are checked once Python is there."
exit 1
