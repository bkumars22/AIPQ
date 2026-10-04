# AI Quality Control Plane integration (`/qcp/*`)

The AI Quality Control Plane
([chaitrishodaya/ai-quality-control](https://github.com/chaitrishodaya/ai-quality-control))
calls AIPQ over a small HTTP contract. Its real-mode adapter is `adapters/aipq.py` and the
decision record is ADR 003 (real-mode rollout: ADR 005) in that repo. This document lists what AIPQ's side of that
contract does, what it wraps, and what its limits are. **Both routes are now supported.**

## Enabling

Set `QCP_ENABLED` to `1`, `true`, `yes` or `on` (default off, see `.env.example`) **on both the backend and the
ai-engine**; `docker-compose.yml` passes it through to both.

- Backend: the `/qcp/*` routes (`backend/routers/qcp.py`) are mounted only when the flag is on. With it off they do not exist (404).
- Backend and ai-engine: the rollback operation itself checks the flag, so even a mounted route or a direct call to the
  ai-engine's `POST /rollback` answers 403 while it is off.
- Everything else is unchanged: the automatic rollback does not depend on the flag.

Both routes use the backend's normal `Authorization: Bearer <JWT or project api_key>` authentication and the same
project-ownership rule as `/prompts/*`: an api_key can only touch prompts in its own project.

## Naming

The control plane identifies prompts and versions with strings; AIPQ uses numeric ids.

- `prompt_id`: a numeric string is used as the AIPQ prompt id. Anything else is looked up
  as a `prompt_name` within the caller's own project (unique per project).
- `version` / `from_version` / `to_version`: `"3"` or `"v3"` is version number 3.
  Anything else is rejected with 422.

## Routes

| Route | Status | Wraps |
|---|---|---|
| `POST /qcp/rootcause` | Supported | `GET /prompts/{id}/current` and `GET /prompts/{id}/causal-impact` (called as functions, so auth and ownership checks are the existing ones) |
| `POST /qcp/rollback` | **Supported** | The ai-engine's targeted rollback (`ai-engine/rollback.py`), through `backend/rollback_ops.py` |

### `POST /qcp/rootcause`

Request: `{incident_id, system, prompt_id, version, last_good_version, score, threshold}`
(extra fields are accepted and ignored).
Response: `{cause, suspect_version, evidence[]}`.

- `cause` is the `interpretation` text returned by ai-engine's causal-impact analysis
  (interrupted time series, current deployed version vs the version it replaced).
- `suspect_version` echoes the requested `version`.
- `evidence` contains only values that came from the request or from AIPQ itself: the
  score and threshold the caller reported (labelled as reported by the caller), the
  deployed version's stored `quality_score`, the pre/post period means, estimated effect,
  p-value and significance, the sample sizes, and the analysis caveat when present.
  Nothing is generated or filled in.

Errors:

| Code | When |
|---|---|
| 401 / 403 | missing or invalid credentials / prompt belongs to another project |
| 404 | prompt not found, or no deployed version yet |
| 409 | the requested version is not the currently deployed one. The existing analysis only covers the deployed version, so it is refused rather than answered about a different version |
| 422 | invalid body or version label |
| 502 | ai-engine unreachable. The existing causal-impact route returns a stub in that case; `/qcp` reports a failure instead of a fabricated cause |

If ai-engine has too little history it returns a "no effect" interpretation. That is
returned as-is: it is the real answer, and it states that no significant effect was found.

### `POST /qcp/rollback`

Restores a specific, **previously passing** version of a named prompt.

Request: `{incident_id, prompt_id, from_version, to_version}`, the four fields the control plane already sends, plus two
optional ones: `requested_by` (for example the human approver) and `reason`.

Response (200): `{status: "succeeded", to_version, from_version, rollback_id, requested_by}`. A refused or failed rollback is
**never** a `succeeded` body: it is an HTTP error.

**Safety rules** (enforced once, in `ai-engine/rollback.py`; the prompt row is locked while it checks and writes):

| The request asks for... | Answer |
|---|---|
| A version that passed the quality gate (`DEPLOYED`, or `ROLLED_BACK` after having been deployed, with a recorded score) | restored |
| A version that **failed** the gate, or is **still testing**, or has no recorded score | 422 and the reason, for example *"version 5 failed the quality gate and was never deployed"* |
| A version that does not exist for the prompt | 404 |
| The version that is already deployed | 409 |
| A `from_version` that is not the version currently deployed | 409 (*"version 2 is not the deployed version (version 4 is)"*): the caller's picture is stale, so nothing is changed |
| A blank reason or requester | 422 |
| Another project's prompt (api_key) | 403 |
| An unreachable ai-engine, or an unexpected ai-engine error | 502, *"no rollback was performed"* |

"Passed the quality gate": the evaluation pipeline marks a version `DEPLOYED`, with its score, only when its score meets
the dataset's threshold, and otherwise `FAILED`. A version later superseded by a rollback is `ROLLED_BACK`. So `DEPLOYED` or
`ROLLED_BACK` with a recorded score means it passed once; `FAILED` and `TESTING` never did.

**Audit trail.** Every targeted rollback writes a `MANUAL` row in the existing `rollbacks` table with the version it left and
the version it restored, the **reason**, and **who requested it**: a new nullable column `requested_by` (migration `V15`).
The recorded requester always includes the authenticated caller, for example
`alice (approver) via AI Quality Control Plane (jwt, project 1)`. A name the caller supplies is stored beside the
authenticated identity, never in place of it. If the control plane sends no reason, one naming the incident is recorded.
The automatic rollback keeps writing its `AUTOMATIC` row with no requester, as before.

## The automatic rollback is unchanged

The automatic rollback (`RollbackEngine.rollback_if_critical`, on CRITICAL drift) still picks the best-scoring of the last
five versions itself. The write it performs (mark the current version `ROLLED_BACK`, the target `DEPLOYED`, move
`prompts.current_version_id`, insert the audit row) now lives in one shared function that both rollbacks call, so there is
no second copy of it. It produces the same SQL as before, in the same order, in one transaction. That is pinned by 10
characterisation tests (`ai-engine/tests/test_rollback_engine.py`) that were written and passed against the unmodified code
first, and pass unchanged after the refactor.

## The dashboard

The AIPQ dashboard has a **Version history and rollbacks** page for each prompt (`#/prompts/{id}/rollbacks`, linked from the
prompt's page). It reads `GET /prompts/{id}/rollbacks` and shows:

- every version with its status and score, which one is live, and for each other version either a **Roll back to vN** action
  or the reason it cannot be restored (failed, still testing, no passing score, already live);
- the rollback history, automatic and manual, with when, from and to, who asked, and why.

Choosing a version opens a confirmation that names the version being left and the one being restored, requires a reason,
and takes an optional name. It calls `POST /prompts/{id}/rollback`, which uses the same operation and the same safety rules
as `/qcp/rollback`, and records `Dashboard` as the channel. Refusals appear on the page with the server's message.
With `QCP_ENABLED` off the history is still readable and the actions are disabled with an explanation. In the GitHub Pages
demo there is no backend, so the history is built from the demo versions and no rollback can be performed.

The `can restore` labels in the response are a hint for the UI; the ai-engine is what enforces the rule.

## Effect on the control plane

In real mode the control plane's diagnose and **rollback** steps can both run against AIPQ once `QCP_ENABLED` is on and
`AIPQ_BASE_URL` points at the backend. Its rollback step no longer ends as *blocked*. A refusal (for example, a version that
never passed) surfaces as an adapter error with AIPQ's message and fails the incident rather than being hidden.

Follow-up in the control-plane repo (not done here): its adapter sends the four original fields, so AIPQ records the
default reason and the authenticated caller. Sending the approver's name and the reason from the incident (`requested_by`,
`reason`) would put the human approver in AIPQ's audit row.

## What was verified

- **Unit and integration tests**: backend 66 passing (was 34); ai-engine 153 passing (was 113). The ai-engine suite has 6
  failures that were there before this work and are unrelated (the LLM-judge test and the five borderline-review pipeline tests,
  which need a live LLM and Postgres): the same six fail before and after.
- **A real run**: the real backend and ai-engine against a real Postgres (the project's own `pgvector` image), a real browser
  on the new page, and seeded prompt history. Refusals of a failed, a testing, an unknown and the live version, and of a wrong
  `from_version`, each changed nothing in the database. A rollback from the dashboard and one through `/qcp/rollback` (with a named
  approver) both changed the live version, marked the old one `ROLLED_BACK`, and wrote `MANUAL` rows with who and why. With
  `QCP_ENABLED` unset the `/qcp` route was absent (404), the dashboard action and the ai-engine both answered 403, and the history
  stayed readable.
- **Not verified**: the control plane's own adapter was not run against this build (the same four-field request was sent by
  hand); the history was seeded with fixed rows, not produced by the evaluation pipeline, which needs an LLM.

## Limits

- The ai-engine's `POST /rollback` is internal, like its `/evaluate`: it does not authenticate callers (the backend does, and checks
  project ownership). Do not expose the ai-engine outside the AIPQ network.
- The requester name is a claim recorded beside the authenticated identity; the dashboard's login is a project-level JWT, not a
  per-person account.
- A version that passed once can be restored later even if its score is now below another threshold you apply elsewhere; the rule
  is the gate it passed at deployment.
- Quality scores are stored as `REAL`, so they come back with float noise (0.93 as 0.9300000071525574); the dashboard rounds them.

## Tests

- `ai-engine/tests/test_rollback_engine.py`: the automatic rollback is pinned (10 tests).
- `ai-engine/tests/test_targeted_rollback.py`: success, restoring a `ROLLED_BACK` version, the `MANUAL` audit row with who and
  why, every refusal making zero writes, a locked prompt row, and one transaction.
- `ai-engine/tests/test_targeted_rollback_endpoint.py`: the flag, success, and each refusal's status.
- `backend/tests/test_qcp.py`: the `/qcp` routes, including rollback success, forwarded who and why, every refusal passed through,
  502 on an unreachable or failing ai-engine, ownership and credentials.
- `backend/tests/test_rollbacks.py`: the history (labels, automatic and manual rows), the dashboard action, the flag, and migration `V15`.
