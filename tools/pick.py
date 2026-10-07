# Reads a uiautomator dump on stdin; prints "x y label" for safe tappable targets in PKG.
import re, sys, xml.etree.ElementTree as ET
pkg = sys.argv[1]
bad = re.compile(r"delete|remove|reset|clear|erase|sign ?out|log ?out|uninstall|wipe|forget|disconnect|call|send|pay|buy|purchase|grant|allow|accessibility|settings app|record", re.I)
try:
    root = ET.fromstring(sys.stdin.read())
except Exception:
    sys.exit(0)
seen = set()
for n in root.iter("node"):
    if n.get("package") != pkg or n.get("clickable") != "true" or n.get("enabled") == "false":
        continue
    label = (n.get("text") or n.get("content-desc") or "").strip()
    if not label:
        # use first labelled descendant
        for c in n.iter("node"):
            label = (c.get("text") or c.get("content-desc") or "").strip()
            if label:
                break
    if not label or bad.search(label) or label in seen or len(label) > 40:
        continue
    m = re.findall(r"\d+", n.get("bounds", ""))
    if len(m) != 4:
        continue
    x1, y1, x2, y2 = map(int, m)
    if (x2 - x1) < 20 or (y2 - y1) < 20:
        continue
    seen.add(label)
    print((x1 + x2) // 2, (y1 + y2) // 2, label.replace("\n", " "))
