# RAG Console — Kimchi Frontend

A modern, dashboard-style React + TypeScript frontend for the `production-rag` FastAPI backend.

## Quick start

```bash
npm install
npm run dev
```

The Vite dev server runs on port `3000` to match the backend CORS config.

Copy `.env.example` to `.env` and adjust if your backend is not on `http://localhost:8000`:

```bash
cp .env.example .env
```

## Available scripts

| Script | Purpose |
|--------|---------|
| `npm run dev` | Start Vite dev server on port 3000 |
| `npm run build` | Type-check and build for production |
| `npm run preview` | Preview the production build |
| `npm run typecheck` | Run TypeScript with no emit |
| `npm run lint` | Run ESLint |
| `npm run test` | Run Vitest tests |
| `npm run test:watch` | Run Vitest in watch mode |

## Application structure

```text
src/
  app/             App shell, routes, query client
  api/             Typed API client and endpoint wrappers
  components/      Reusable common, copilot, documents, and observability components
  hooks/           React hooks for state, queries, and persistence
  lib/             Formatting, validation, and observability utilities
  pages/           Top-level page components
  styles/          CSS tokens, global styles, and shared primitives
  test/            Test utilities, MSW server, and setup
```

## Design

This frontend follows the brief in `frontend_design.md`:

- Exactly three top-level tabs: **Copilot**, **AI Observability**, and **Documents**.
- Dark "ink and signal" theme with a light mode toggle.
- Dashboard-style cards, metric summaries, and dense data views.
- Accessible keyboard navigation, focus management, and `aria-live` announcements.
- No cloud/database credentials, direct Pinecone code, fake metrics, or fake progress.

## Backend API

All data comes from the FastAPI backend at `VITE_API_BASE_URL`. The frontend uses:

- `GET /health` — connection status
- `POST /ask` — Copilot question
- `POST /documents`, `GET /documents/{id}`, `PUT /documents/{id}`, `DELETE /documents/{id}` — document management
- `GET /traces`, `GET /traces/{trace_id}` — trace search and detail
- `GET /logs`, `GET /logs/{trace_id}` — log search and correlated logs
