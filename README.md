# AIPQ — AI Prompt Quality & Drift Management

> Git for prompts — with automatic quality gates, drift detection, and intelligent rollback.

## The problem

When prompts change in production AI systems, quality silently drops. Nobody knows which change caused it. Nobody can roll back to the last good version automatically.

**Real proof this problem exists:** [ARIA](https://github.com/bkumars22/ARIA) — an AI tutor — had its Socratic compliance start at 22.2%. Reaching 100% took 3 days of manually iterating prompts against a golden dataset, by hand, with no version history and no automatic quality gate. AIPQ automates that entire loop.

## What it does

1. **Prompt version control** — every prompt change is versioned like a git commit: who changed it, a diff of what changed, the quality score it achieved.
2. **Quality gate before deployment** — every new prompt version is run against a golden dataset automatically. Below threshold → deployment blocked.
3. **Drift detection** — deployed prompts are monitored continuously. An IsolationForest model flags when the *same* prompt starts producing lower-quality output over time (model drift, not prompt drift).
4. **Automatic rollback** — on critical drift, AIPQ finds the best-scoring previous version and rolls back automatically, with a full diff in the alert.
5. **A/B testing** — run two prompt versions simultaneously, split traffic, auto-promote the winner after N samples.
6. **Python SDK** — a single `@aipq_prompt` decorator wraps any prompt-returning function in any of your projects (QAIP, ARIA, ZENTRAVIX, SCIP) and gets version control, quality gating, and drift reporting for free.

## Architecture

```
React Dashboard (:3001) ──REST+WS──▶ FastAPI Backend (:8001) ──▶ PostgreSQL+pgvector (:5433)
                                            │                              ▲
                                            ▼                              │
                                     AI Engine (:8002) ───────────────── Redis (:6380)
                                     LangGraph eval pipeline
                                     IsolationForest drift + SHAP
                                     APScheduler (15-min monitoring)

Python SDK (installed into ARIA/QAIP/SCIP/ZENTRAVIX) ──▶ Backend REST API
GitHub Action (any project's CI) ──▶ Backend /evaluations ──▶ blocks merge on quality drop
```

See `docs/` (or ask for the full architecture writeup) for the complete data model and node-by-node pipeline design.

## Quick start

```bash
cp .env.example .env   # fill in at least one LLM provider key
docker compose up --build
```

- Backend: http://localhost:8001
- AI Engine: http://localhost:8002
- Dashboard: http://localhost:3001

The dashboard has no login page yet — it authenticates with a dashboard JWT
(admin session, cross-project visibility) read from `frontend/.env`'s
`VITE_DEV_JWT`. Mint one with:

```bash
cd backend
python -c "
from auth.jwt import create_access_token
print(create_access_token(subject='admin', extra_claims={'project_id': 0}))
"
```

Then run the frontend separately for hot-reload during development:

```bash
cd frontend
npm install
npm run dev   # http://localhost:3001
```

## SDK in 5 lines

```python
from aipq import AIPQClient, aipq_prompt

aipq = AIPQClient(api_key=os.getenv("AIPQ_API_KEY"), project_id=os.getenv("AIPQ_PROJECT_ID"))

@aipq_prompt(name="aria_socratic_system", dataset="aria_adversarial_golden", threshold=0.90)
async def get_system_prompt() -> str:
    return "You are ARIA — a Socratic AI tutor. RULE 1: NEVER give direct answers. ..."
```

Every call checks whether the prompt text changed since the last deployed version. If it did, AIPQ evaluates it against the golden dataset before letting your app use it — a failing score raises `PromptQualityError` and blocks your app from starting with a bad prompt.

## Tech stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI, Python 3.11, JWT (dashboard) + api_key (SDK) dual auth |
| AI Engine | LangGraph, deepeval (GEval), scikit-learn (IsolationForest), SHAP |
| Frontend | React 19, TypeScript, Vite, Tailwind v4, React Query |
| Database | PostgreSQL 15 + pgvector |
| Cache | Redis |
| CI/CD | GitHub Actions, reusable `aipq-evaluate` action |

## Status

Built and verified end-to-end against a real Postgres + Redis stack (no mocked DB/cache in any of the testing below) — this isn't just scaffolding:

- **Schema, SDK, backend, evaluation pipeline, drift detection**: all built and tested against live services (14 SDK unit tests + direct integration tests through the real HTTP API and LangGraph pipeline).
- **Dashboard**: shows real registered projects (ARIA, QAIP) with live quality scores, expandable per-prompt version history, and drift status/root-cause hints — pulling from the same live backend, not sample data.
- **ARIA integration**: `aria_socratic_system` registered, versioned, and deliberately drifted during testing to prove the full loop — a CRITICAL-severity IsolationForest detection triggered a real automatic rollback (v2 → v1), visible in the dashboard today.
- **QAIP integration**: Stage 5's defect-explanation prompt is version-controlled through AIPQ (`qaip_defect_explanation`, gated against a 10-case golden dataset), with verified fail-open behavior when AIPQ is unreachable or evaluation fails — QAIP's pipeline never breaks either way.
- **AIMO integration**: `aipq_connector.check_aipq_root_cause` is wired into AIMO's `generate_root_cause` node and into a newly-implemented `detectors/hallucination.py` (real deepeval `FaithfulnessMetric`, Redis-cached). Verified directly against ARIA's live CRITICAL-drift state, correctly producing "Root cause: prompt change v1". Not yet automatic end-to-end: AIMO's incident evidence doesn't carry the AIPQ project/prompt mapping yet, so this fires when called, not on its own.

**Not yet built**: A/B testing (schema exists, no endpoints/UI), Version Comparison / Evaluation Results / Golden Dataset Manager dashboard pages, the CLI, and the reusable GitHub Action.
