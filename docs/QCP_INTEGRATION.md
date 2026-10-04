# AI Quality Control Plane integration (`/qcp/*`)

The AI Quality Control Plane
([chaitrishodaya/ai-quality-control](https://github.com/chaitrishodaya/ai-quality-control))
calls AIPQ over a small HTTP contract. Its real-mode adapter is `adapters/aipq.py` and the
decision record is ADR 003 in that repo. This document lists what AIPQ's side of that
contract does, what it wraps, and what is a gap.

## Enabling

The routes live in `backend/routers/qcp.py` and are mounted only when `QCP_ENABLED` is
`1`, `true`, `yes` or `on` (default off, see `.env.example`). With the flag off the routes
do not exist (404) and nothing else in the backend changes.

Both routes use the backend's normal `Authorization: Bearer <JWT or project api_key>`
authentication and the same project-ownership rule as `/prompts/*`: an api_key can only
touch prompts in its own project.

## Naming

The control plane identifies prompts and versions with strings; AIPQ uses numeric ids.

- `prompt_id`: a numeric string is used as the AIPQ prompt id. Anything else is looked up
  as a `prompt_name` within the caller's own project (unique per project).
- `version` / `from_version` / `to_version`: `"3"` or `"v3"` is version number 3.
  Anything else is rejected with 422.

## Routes

| Route | Status | Wraps |
|---|---|---|
| `POST /qcp/rootcause` | Real | `GET /prompts/{id}/current` and `GET /prompts/{id}/causal-impact` (called as functions, so auth and ownership checks are the existing ones) |
| `POST /qcp/rollback` | **Gap: always 501** | nothing (see below) |

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

### `POST /qcp/rollback`: gap

Request: `{incident_id, prompt_id, from_version, to_version}`. Response: **501** with an
explanatory message, after authentication and body validation.

Why: AIPQ's only rollback is `RollbackEngine.rollback_if_critical` in
`ai-engine/detectors/drift_detector.py`. It runs automatically on CRITICAL drift and picks
its own target (the best-scoring of the last five versions). There is no operation that
rolls a prompt back to a version chosen by the caller, and the `rollbacks` table's
`MANUAL` trigger type is not written by any code. Adding one would be new business logic,
not a wrapper, so it is left out.

What closing the gap needs (not done): a targeted rollback function that performs the same
updates as `RollbackEngine` (mark the current version `ROLLED_BACK`, mark the target
`DEPLOYED`, set `prompts.current_version_id`, insert a `rollbacks` row with
`triggered_by = 'MANUAL'`), which `/qcp/rollback` and any future manual-rollback route
could then share.

## Effect on the control plane

In real mode the control plane's diagnose step can work against AIPQ once `QCP_ENABLED`
is on and `AIPQ_BASE_URL` points at the backend. Its rollback step will fail with a 501
(`AdapterError`). Until the gap above is closed, real-mode runs cannot complete the
rollback step.

Neither route has been exercised against a live AIPQ deployment or the control plane's
real-mode adapter; the tests below use a faked database pool and a mocked ai-engine.

## Tests

`backend/tests/test_qcp.py` covers: flag off (routes absent, existing routes unaffected),
flag on, root cause success by name and by numeric id, extra fields, non-current version
(409), ai-engine down (502), unknown prompt (404), no deployed version (404), bad version
label and missing field (422), another project's prompt (403), missing credentials (401),
and rollback (501, 401, 422).
