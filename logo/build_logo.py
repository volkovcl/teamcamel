#!/usr/bin/env python3
"""Builds teamcamel-logo.svg: the camel "M" between TEA and CAMEL, all as outlines.

    python3 build_logo.py            (on a Mac; needs the swift command)

The layout is the designer's concept: letters 197.93 units tall on a baseline at
y = 666.37, TEA ending at x = 383.98, CAMEL starting at x = 612.93, and the camel
(camel-m.svg) exactly where it was drawn. The letters are traced from PT Sans
Regular, so the result contains shapes only and needs no font to display.
"""
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
FONT, SIZE, BASELINE = "PT Sans", 197.93, 666.37
TEA_END, CAMEL_START = 383.98, 612.93


def trace(text, x):
    out = subprocess.run(["swift", str(HERE / "outline.swift"), FONT, text, str(SIZE), "%.2f" % x, str(BASELINE)],
                         capture_output=True, text=True, check=True)
    info = dict(kv.split("=") for kv in out.stderr.split())
    return out.stdout.strip(), float(info["width"]), float(info["capHeight"]), info["font"]


def numbers(d):
    """Absolute x and y values of a path made of M/L/Q/C/Z commands with absolute coordinates."""
    v = [float(n) for n in re.findall(r"-?\d+\.?\d*", d)]
    return v[0::2], v[1::2]


_, tea_width, cap, font = trace("TEA", 0)
tea, _, _, _ = trace("TEA", TEA_END - tea_width)
camel_letters, _, _, _ = trace("CAMEL", CAMEL_START)
camel = re.search(r'<path[^>]*\sd="([^"]+)"', (HERE / "camel-m.svg").read_text(encoding="utf-8")).group(1)

xs = numbers(tea)[0] + numbers(camel_letters)[0]
left, right = min(xs), max(xs)
top = min(BASELINE - cap, 524.5)          # 524.5: top of the camel's humps as drawn
pad = 2
box = "%.0f %.0f %.0f %.0f" % (left - pad, top - pad, right - left + 2 * pad, BASELINE - top + 2 * pad)

svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="%s" role="img" aria-label="TeamCamel">
  <title>TeamCamel</title>
  <!-- TeamCamel logo. Camel "M" and concept: Aleksandr A. Volkov, 2026.
       Lettering traced from %s (ParaType), a free font; shapes only, no font needed. -->
  <path class="letters" d="%s"/>
  <path class="camel" d="%s"/>
  <path class="letters" d="%s"/>
</svg>
''' % (box, font, tea, camel, camel_letters)
(HERE / "teamcamel-logo.svg").write_text(svg, encoding="utf-8")
print("font %s, TEA width %.2f, cap height %.2f, viewBox %s, %d bytes" % (font, tea_width, cap, box, len(svg)))
