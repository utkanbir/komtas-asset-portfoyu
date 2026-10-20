#!/usr/bin/env python3
"""Claude artifact'inden alınan index.html'e claude-shim.js'i ekler (idempotent).

Kullanım:  python3 scripts/inject_shim.py <artifact-index.html> web/index.html
"""
import sys

TAG = '<script src="claude-shim.js"></script>'
src, dst = sys.argv[1], sys.argv[2]
html = open(src, encoding="utf-8").read()
if TAG not in html:
    i = html.find("<head>")
    if i < 0:
        sys.exit("<head> bulunamadı")
    i += len("<head>")
    html = html[:i] + '<meta charset="utf-8">' + TAG + html[i:]
open(dst, "w", encoding="utf-8").write(html)
print("yazıldı:", dst)
