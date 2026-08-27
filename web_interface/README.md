# web_interface

The Flask dashboard, HTTP API, and background worker scripts. The package
also hosts the public unauthenticated mini-site (`routes/public_routes.py`,
`templates/public/`), the participant self-service surface
(`routes/my_collections_routes.py` plus the my-collections /
participant-studies / participant-enrichment services), a `services/`
layer of backend logic extracted from routes, and SEO/indexing
(`seo.py`: canonical link, robots.txt, sitemap).

- Structure, auth, workers, and frontend conventions:
  [docs/web_interface.md](../docs/web_interface.md)
- Full endpoint inventory: [docs/routes.md](../docs/routes.md)
  (regenerate with `python scripts/gen_route_inventory.py`)
- Invariants (worker stdout contract, shared state, and the rest):
  see the invariants list in [CONTRIBUTING.md](../CONTRIBUTING.md)

Run locally: `python web_interface/fyp_data_hub.py` → http://localhost:5002
