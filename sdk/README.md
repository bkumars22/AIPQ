# aipq-sdk

Git for prompts — automatic quality gates, drift detection, and intelligent
rollback for LLM prompts in production.

Part of [AIPQ](https://github.com/bkumars22/AIPQ), a full platform (backend,
evaluation pipeline, drift detection, dashboard) for versioning and
continuously evaluating prompts against a golden dataset. This package is
the client library + CLI you install into your own project; it talks to an
AIPQ backend over HTTP.

## Install

```bash
pip install aipq-sdk
```

## SDK in 5 lines

```python
from aipq import AIPQClient, aipq_prompt

aipq = AIPQClient(api_key=os.getenv("AIPQ_API_KEY"), project_id=os.getenv("AIPQ_PROJECT_ID"))

@aipq_prompt(name="aria_socratic_system", dataset="aria_adversarial_golden", threshold=0.90)
async def get_system_prompt() -> str:
    return "You are ARIA — a Socratic AI tutor. RULE 1: NEVER give direct answers. ..."
```

Every call checks whether the prompt text changed since the last deployed
version. If it did, AIPQ evaluates it against the golden dataset before
letting your app use it — a failing score raises `PromptQualityError` and
blocks your app from starting with a bad prompt. If AIPQ itself is
unreachable, this fails *open* (logs a warning, returns the unvalidated
text) rather than blocking your app on an AIPQ outage.

Report real usage for drift monitoring:

```python
asyncio.create_task(
    aipq.report_usage(
        prompt_name="aria_socratic_system",
        output=response_text,
        context=retrieved_context,
        quality_score=measured_compliance,
    )
)
```

## CLI

The same package installs an `aipq` command for local/CI use:

```bash
aipq evaluate --prompt-name aria_socratic_system --prompt-file prompt.txt \
    --dataset aria_adversarial_golden --threshold 0.90

aipq versions list --prompt-name aria_socratic_system
```

Reads `AIPQ_API_KEY` / `AIPQ_PROJECT_ID` / `AIPQ_BASE_URL` from the
environment (or `--api-key` / `--project-id` / `--base-url` flags). Exits 0
on pass or unchanged content, 1 on a failing quality gate, 2 on a
usage/connection error.

There's also a reusable GitHub Action wrapping the same evaluation flow —
see [`bkumars22/AIPQ/.github/actions/aipq-evaluate`](https://github.com/bkumars22/AIPQ/tree/main/.github/actions/aipq-evaluate).

## Links

- [Full platform README](https://github.com/bkumars22/AIPQ) — architecture, backend, dashboard, self-hosting
- [Issues](https://github.com/bkumars22/AIPQ/issues)
