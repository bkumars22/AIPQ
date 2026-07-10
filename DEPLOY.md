# Deploying AIPQ to Render

`render.yaml` at the repo root defines all 5 pieces of the stack (Postgres
+ pgvector, Redis, backend, ai-engine, frontend) as a Render Blueprint.
Render reads that file and creates/updates all 5 services together — but a
few things need a human in Render's dashboard, since they involve secrets
or values that don't exist until after the first deploy.

## 1. Create the Blueprint

1. Render dashboard → **New** → **Blueprint**.
2. Connect the `bkumars22/AIPQ` GitHub repo (Render will ask to install its
   GitHub App if you haven't already — that's a one-time authorization on
   your GitHub account, not something I can do for you).
3. Render parses `render.yaml` and shows all 5 resources for review —
   check plans/regions if you want something other than the `free`/`oregon`
   defaults, then confirm.
4. During this step Render will prompt you for every `sync: false` env var
   in the blueprint. For the ones you don't have a real value for yet
   (`VITE_DEV_JWT`, `CORS_ORIGINS`), leave them blank for now — steps 3-4
   below cover coming back to fill them in once the other services exist.

## 2. Add at least one LLM key

The `aipq-ai-engine` service needs a real LLM key or deepeval scoring has
no judge to call (every evaluation will fail with score 0, same as this
local dev environment has been running with all session). In Render's
dashboard → `aipq-ai-engine` → Environment, set one of:

- `GROQ_API_KEY` (free tier, fastest to get)
- `ANTHROPIC_API_KEY`
- `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_DEPLOYMENT_NAME`

Saving triggers a redeploy of that one service automatically.

## 3. Confirm the frontend's API URL

`render.yaml` hardcodes `VITE_API_URL: https://aipq-backend.onrender.com`
(Render can't string-interpolate its own service URLs into this file).
That's almost certainly right, but Render only guarantees it if the
`aipq-backend` subdomain wasn't already taken by someone else. After the
backend service is created:

1. Check its actual URL on its dashboard page (top of the page, next to
   the service name).
2. If it doesn't match `https://aipq-backend.onrender.com`, edit
   `render.yaml`'s `VITE_API_URL` value to match, commit, and push — Render
   redeploys the frontend automatically on push.

## 4. Mint the frontend's admin token

The dashboard authenticates with a single long-lived JWT baked into the
frontend build (`VITE_DEV_JWT`) — the same mechanism the local Docker setup
already uses, not something new to Render. It has to be signed with
`aipq-backend`'s `JWT_SECRET`, which Render only generates once that
service is created, so this is unavoidably a post-deploy step:

1. `aipq-backend` → Environment tab → copy the generated `JWT_SECRET` value.
2. Run this locally (needs `python-jose[cryptography]`; `pip install
   "python-jose[cryptography]"` if you don't have it) — a baked-in frontend
   token needs to outlive `auth/jwt.py`'s default 12h expiry, so this signs
   one with a 1-year expiry directly instead of calling `create_access_token`:
   ```bash
   python -c "
   from datetime import datetime, timedelta, timezone
   from jose import jwt
   payload = {'sub': 'admin', 'project_id': 0, 'exp': datetime.now(timezone.utc) + timedelta(days=365)}
   print(jwt.encode(payload, '<paste the JWT_SECRET from step 1>', algorithm='HS256'))
   "
   ```
3. `aipq-frontend` → Environment tab → set `VITE_DEV_JWT` to that value →
   save (triggers a rebuild — static sites bake env vars in at build time,
   so this is the only way to update it).

## 5. Set CORS_ORIGINS on the backend

Once you know the frontend's actual URL (same place as step 3, on
`aipq-frontend`'s dashboard page):

`aipq-backend` → Environment → set `CORS_ORIGINS` to that URL (add the
GitHub Pages demo URL too, comma-separated, if you want both frontends able
to call this backend — e.g.
`https://aipq-frontend.onrender.com,https://bkumars22.github.io`).

## 6. Seed real data (optional)

A fresh deploy has zero projects/prompts. Either use the dashboard once
live (needs a real admin login flow, which doesn't exist yet — the
`VITE_DEV_JWT` from step 4 *is* the admin session), or register a project
via `POST /projects/register` with that JWT and follow the same flow used
throughout local development this session (see `cli/aipq_cli.py` /
`sdk/aipq/client.py` for examples).

## What's already handled automatically

- **Database migrations** run on every backend startup (`backend/db/migrate.py`,
  wired into `main.py`'s lifespan) — a fresh Postgres instance gets all 11
  migrations applied on first boot, no manual `psql -f` needed.
- **pgvector** is supported on Render's managed Postgres (`CREATE EXTENSION
  vector;`, already in migration V1) — confirmed via Render's docs, no
  workaround needed.
- **backend ↔ ai-engine** communication uses Render's private network
  (`fromService: hostport`) — `backend/config.py`'s `ai_engine_url()`
  handles Render's bare `host:port` format the same way it already handles
  Docker Compose's full `http://ai-engine:8002`.

## Known limitation

`VITE_DEV_JWT` is a single static admin token baked into public JS — fine
for a demo/portfolio deployment, not how you'd want a real multi-user
product to authenticate. Building a real login flow is a separate piece of
work, not part of this deploy.
