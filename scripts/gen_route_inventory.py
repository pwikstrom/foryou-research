"""Regenerate docs/routes.md from the live Flask URL map.

Run from the project root:
    python scripts/gen_route_inventory.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


HEADER = """\
# HTTP route inventory

All non-static routes of the FYP web app, grouped by blueprint. Generated —
do not edit by hand; regenerate after adding or removing routes:

```bash
python scripts/gen_route_inventory.py
```

"""


def main() -> None:
    """Import the app, dump its URL map as a markdown table, write docs/routes.md."""
    from web_interface.fyp_data_hub import app

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

    out = PROJECT_ROOT / "docs" / "routes.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {out} ({len(rows)} routes)", file=sys.stderr)


if __name__ == "__main__":
    main()
