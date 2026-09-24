"""Bundle the editable source into a standalone, offline-previewable index.html.

Run: python build.py
The resulting index.html works by itself as a clearly labelled, browser-local
preview, and uses the shared SQLite-backed API when served by app.py.
"""
from pathlib import Path

base = Path(__file__).resolve().parent
template = (base / "source/page.html").read_text(encoding="utf-8")
css = (base / "source/styles.css").read_text(encoding="utf-8")
js = (base / "source/client.js").read_text(encoding="utf-8")
assert template.count("/* BUNDLED_STYLES */") == template.count("/* BUNDLED_SCRIPT */") == 1
result = template.replace("/* BUNDLED_STYLES */", css).replace("/* BUNDLED_SCRIPT */", js)
(base / "index.html").write_text(result, encoding="utf-8")
print(f"Built {base / 'index.html'} ({len(result):,} characters)")
