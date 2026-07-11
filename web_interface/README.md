# web_interface

The Flask dashboard, HTTP API, and background worker scripts.

- Structure, auth, workers, and frontend conventions:
  [docs/web_interface.md](../docs/web_interface.md)
- Full endpoint inventory: [docs/routes.md](../docs/routes.md)
  (regenerate with `python scripts/gen_route_inventory.py`)
- Invariants (worker stdout contract, shared `process_stats.json`):
  [CONTRIBUTING.md](../CONTRIBUTING.md)

Run locally: `python web_interface/fyp_data_hub.py` → http://localhost:5002
