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
| Backend | FastAPI, Python 3.11, JWT auth |
| AI Engine | LangGraph, deepeval, scikit-learn (IsolationForest), SHAP |
| Frontend | React 18, TypeScript, Tailwind, Recharts |
| Database | PostgreSQL 15 + pgvector |
| Cache | Redis |
| CI/CD | GitHub Actions, reusable `aipq-evaluate` action |

## Status

Early build — architecture, schema, SDK, backend, evaluation pipeline, drift detection, and dashboard scaffolded. ARIA is the first real integration (see `docs/aria-integration.md` once wired).
