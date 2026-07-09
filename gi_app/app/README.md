# Web app — GI Cancer Data Platform

The Phase-1 dashboard for browsing everything ingested into Postgres and object
storage. Built in the same style as the reference `university-app` frontend:

**React 18 + TypeScript + Vite · Tailwind + shadcn/ui (Radix) · recharts · lucide-react · sonner**

It is a client-side SPA served as static files by nginx in production.

## Run it

Part of the default stack — from `gi_app/`:

```bash
docker compose up -d --build
```

Then open **http://localhost:5173**. The app calls the API at `VITE_API_URL`
(default `http://localhost:8008`), baked in at **build time** — change it in
`.env` / the compose `app` build arg and rebuild (`docker compose build app`) to
retarget.

Local dev (hot reload, talks to the running API):

```bash
npm install
npm run dev          # http://localhost:5173
npm run build        # tsc type-check + production build to dist/
```

## What it shows

- **Overview** (`/`) — KPI tiles (datasets, cases, samples, slides, WSI assets,
  stored size), a cases-by-dataset chart, and the dataset index. Click a dataset
  to drill in.
- **Dataset detail** (`/datasets/:id`) — loaded-record counts, key distributions
  (sex / vital status / AJCC stage / sample & slide type), overall-survival
  summary + histogram, metadata completeness meters, the downloaded-WSI slide
  manifest (with signed **Open** links), the case→sample→slide→WSI linkage chain
  + integrity checks, and ingestion-run history.

## Structure

```
src/
  App.tsx              # router (Overview + DatasetDetail under a shared Layout)
  lib/
    api.ts             # the entire API layer: typed interfaces + one fetch fn per endpoint
    utils.ts           # cn() + number/byte/date formatters
    theme.ts           # chart color palette
  components/
    Layout.tsx         # brand sidebar + content shell
    Section.tsx, StatCard.tsx, StatusPill.tsx, MetricBar.tsx, DistributionChart.tsx
    ui/                # shadcn primitives (button, card, badge)
  pages/
    Overview.tsx
    DatasetDetail.tsx
```

Every panel loads independently (`Promise.allSettled`), so one failing endpoint
degrades a single card rather than blanking the page.
