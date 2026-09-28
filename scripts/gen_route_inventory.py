"""Regenerate docs/routes.md from the live Flask URL map.

Run from the project root:
    python scripts/gen_route_inventory.py           # rewrite docs/routes.md
    python scripts/gen_route_inventory.py --check   # exit 1 if it is stale

tests/unit/test_routes_doc.py runs the same comparison in the unit gate.
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


HEADER = """\
# HTTP route inventory

All non-static routes of the For You Data Hub web app, grouped by blueprint. Generated —
do not edit by hand; regenerate after adding or removing routes:

```bash
python scripts/gen_route_inventory.py
```

"""


ROUTES_DOC = PROJECT_ROOT / "docs" / "routes.md"


def render(app) -> str:
    """Return the markdown inventory of ``app``'s URL map (docs/routes.md content)."""
    rows = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        methods = ",".join(sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS")))
        blueprint = rule.endpoint.split(".")[0] if "." in rule.endpoint else "(app)"
        rows.append((blueprint, rule.rule, methods, rule.endpoint))
    rows.sort()

    lines = [HEADER, "| Blueprint | Path | Methods | Endpoint |", "|---|---|---|---|"]
    lines += [f"| {bp} | `{rule}` | {methods} | `{ep}` |" for bp, rule, methods, ep in rows]
    lines.append(f"\n{len(rows)} routes total.")
    return "\n".join(lines) + "\n"


def main(check: bool = False) -> int:
    """Import the app and write (or, with ``check``, verify) docs/routes.md."""
    from web_interface.fyp_data_hub import app

    content = render(app)
    if check:
        if ROUTES_DOC.read_text(encoding="utf-8") != content:
            print(
                f"{ROUTES_DOC} is stale — run: python scripts/gen_route_inventory.py",
                file=sys.stderr,
            )
            return 1
        print(f"{ROUTES_DOC} is current", file=sys.stderr)
        return 0
    ROUTES_DOC.write_text(content, encoding="utf-8")
    print(f"Wrote {ROUTES_DOC}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if docs/routes.md is stale")
    sys.exit(main(check=parser.parse_args().check))
