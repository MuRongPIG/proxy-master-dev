# Master UI

Standalone dashboard UI for operating and monitoring master server APIs.
Upgraded with Vue 3 + ECharts.

## Features

- Separate from master server deployment.
- Input API base URL and token in top bar.
- Dashboard tabs: Overview, Proxies, Tasks, Nodes, Operations.
- Charts powered by ECharts (tier distribution + task status).
- Auto refresh and activity log for operations.

## Run

Option 1: open index directly in browser.

Note: CDN scripts are used for Vue/ECharts, so internet access is required on first load.

Option 2 (recommended): run a local static server.

```bash
cd master_ui
python -m http.server 5173
```

Then open:

- http://127.0.0.1:5173

## CORS

If UI and API are on different origins, start master with CORS enabled:

```bash
python -m master_server.main --host 0.0.0.0 --port 62071 --db-path proxy_checker.db --node-token your-token --ui-cors-origins "http://127.0.0.1:5173,http://localhost:5173"
```

Use `*` to allow all origins in development.

## Notes

- Top bar `Auto Refresh(s)` controls dashboard polling interval.
- In Operations tab, all maintenance actions are wired to existing master APIs.
- Token is persisted in browser local storage for convenience.
