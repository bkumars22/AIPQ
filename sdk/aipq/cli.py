#!/usr/bin/env python3
"""
AIPQ CLI — manual prompt evaluation and version inspection from a terminal
or a CI step, using the same AIPQClient the SDK's @aipq_prompt decorator
uses under the hood.

    aipq evaluate --prompt-name aria_socratic_system --prompt-file prompt.txt \
        --dataset aria_adversarial_golden --threshold 0.90

    aipq versions list --prompt-name aria_socratic_system

Credentials/connection come from AIPQ_API_KEY / AIPQ_PROJECT_ID / AIPQ_BASE_URL
env vars (matching the SDK's own convention) unless overridden by flags.

Only wraps endpoints that actually exist in the backend today — rollback
and diff aren't exposed as REST endpoints yet (rollback is currently
automatic-only, triggered internally by ai-engine's drift detector), so
those aren't CLI commands here.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .client import AIPQClient
from .exceptions import AIPQError, PromptQualityError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252, which can't print checkmark/cross


def _client_from_args(args: argparse.Namespace) -> AIPQClient:
    api_key = args.api_key or os.environ.get("AIPQ_API_KEY")
    project_id = args.project_id or os.environ.get("AIPQ_PROJECT_ID")
    base_url = args.base_url or os.environ.get("AIPQ_BASE_URL", "http://localhost:8001")

    if not api_key or not project_id:
        print("error: AIPQ_API_KEY and AIPQ_PROJECT_ID are required (env var or --api-key/--project-id)", file=sys.stderr)
        sys.exit(2)

    return AIPQClient(api_key=api_key, project_id=project_id, base_url=base_url)


async def cmd_evaluate(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        if not os.path.exists(args.prompt_file):
            print(f"error: prompt file not found: {args.prompt_file}", file=sys.stderr)
            return 2
        with open(args.prompt_file, encoding="utf-8") as f:
            content = f.read()

        # get_current_version() only resolves once a prompt_id is cached from
        # an earlier call in this process, so on a cold CLI invocation we
        # register first (idempotent) to get the prompt_id, then fetch its
        # current version directly to correctly detect "unchanged".
        prompt_id = await client._ensure_prompt_registered(args.prompt_name, args.dataset, args.threshold)
        current = await client._request("GET", f"/prompts/{prompt_id}/current", critical=True, treat_404_as_none=True)

        if current is not None and current["content"] == content:
            print(f"AIPQ: '{args.prompt_name}' unchanged — skipping evaluation.")
            return 0

        result = await client.create_version(
            prompt_name=args.prompt_name, content=content, dataset=args.dataset,
            threshold=args.threshold, changed_by="cli", change_message="aipq evaluate",
        )
        print(f"✅ Prompt quality: {result['quality_score']:.2f} (threshold: {args.threshold}) "
              f"— '{args.prompt_name}' v{result['version_number']} deployed.")
        return 0
    except PromptQualityError as exc:
        print(f"❌ {exc}")
        return 1
    except AIPQError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        await client.aclose()


async def cmd_versions_list(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        prompt_id = await client._ensure_prompt_registered(args.prompt_name, dataset="", threshold=0.85)
        result = await client._request("GET", f"/prompts/{prompt_id}/versions", critical=True)
        versions = result.get("versions", []) if result else []
        if not versions:
            print(f"No versions yet for '{args.prompt_name}'.")
            return 0
        print(f"{'Version':<10}{'Status':<14}{'Score':<8}{'Changed by':<14}Deployed")
        for v in versions:
            score = f"{v['quality_score']:.2f}" if v["quality_score"] is not None else "—"
            deployed = v["deployed_at"] or "—"
            print(f"v{v['version_number']:<9}{v['status']:<14}{score:<8}{v['changed_by']:<14}{deployed}")
        return 0
    except AIPQError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        await client.aclose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aipq", description="AIPQ CLI")
    parser.add_argument("--api-key", help="overrides AIPQ_API_KEY")
    parser.add_argument("--project-id", help="overrides AIPQ_PROJECT_ID")
    parser.add_argument("--base-url", help="overrides AIPQ_BASE_URL")

    subparsers = parser.add_subparsers(dest="command", required=True)

    evaluate = subparsers.add_parser("evaluate", help="Create a new version from a file and run the quality gate")
    evaluate.add_argument("--prompt-name", required=True)
    evaluate.add_argument("--prompt-file", required=True, help="Path to a file containing the current prompt text")
    evaluate.add_argument("--dataset", required=True, help="Golden dataset name")
    evaluate.add_argument("--threshold", type=float, default=0.85)
    evaluate.set_defaults(func=cmd_evaluate)

    versions = subparsers.add_parser("versions", help="Inspect version history")
    versions_sub = versions.add_subparsers(dest="versions_command", required=True)
    versions_list = versions_sub.add_parser("list", help="List all versions of a prompt")
    versions_list.add_argument("--prompt-name", required=True)
    versions_list.set_defaults(func=cmd_versions_list)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    exit_code = asyncio.run(args.func(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
