# app/ — React frontend (web-tool phase)

Scaffold placeholder. The Phase-1 dashboards (Dataset Inventory, Metadata Explorer,
Data Linkage, AI/ML Readiness, Slide gallery — see `docs/ARCHITECTURE.md`) are built
here next, mirroring `university-app/university_app/app/` (React + TS + Vite + Tailwind +
shadcn + recharts, served by nginx).

Wired into `docker-compose.yaml` under the `webtool` profile:

```
docker compose --profile webtool up --build app
```
