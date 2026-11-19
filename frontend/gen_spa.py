"""
Assembles the single-page control plane (index.html) from the view templates.

    python frontend/gen_spa.py          # write frontend/index.html
    python frontend/gen_spa.py --check  # exit 1 if index.html is out of date

Edit the files in frontend/views/, never index.html directly.
"""
import sys
from pathlib import Path

FRONTEND_DIR = Path(__file__).resolve().parent
VIEWS_DIR = FRONTEND_DIR / "views"
OUTPUT = FRONTEND_DIR / "index.html"

# Order matters: layout_top opens <html>/<body>/<main>, layout_bottom closes them.
VIEW_ORDER = [
    "layout_top.html",
    "view_command.html",
    "view_incidents.html",
    "view_decision.html",
    "view_campaign.html",
    "view_trace.html",
    "layout_bottom.html",
]


def build() -> str:
    parts = []
    for name in VIEW_ORDER:
        path = VIEWS_DIR / name
        if not path.exists():
            raise FileNotFoundError(f"Missing view template: {path}")
        parts.append(path.read_text(encoding="utf-8").rstrip("\n") + "\n")
    return "\n".join(parts)


def main() -> int:
    html = build()
    if "--check" in sys.argv[1:]:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != html:
            print("index.html is out of date; run: python frontend/gen_spa.py")
            return 1
        print("index.html is up to date")
        return 0
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"Wrote {OUTPUT} ({len(html.splitlines())} lines) from {len(VIEW_ORDER)} views")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
