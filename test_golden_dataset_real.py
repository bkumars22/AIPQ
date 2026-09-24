"""
Real test of the 20 cases in golden_dataset.json (from Downloads, fixed
from broken JSON) against ARIA's real, unmodified prompt content. Adds
these as real golden cases to the same real aria_adversarial_golden
dataset the existing 13 cases live in, so they genuinely contribute to
ARIA's real quality gate -- not a separate, disconnected test.

Schema translation from golden_dataset.json's format to AIPQ's:
- expected_behavior tags (guides_with_question, etc.) -> natural-language
  rubrics describing the expected REPLY shape, in the same style that
  fixed the earlier rubric-wording bug (not a description of the rule).
- forbidden_patterns -> forbidden, as-is.
- required_patterns_any (ANY one must match) is NOT mapped to AIPQ's
  `required` field, which means ALL must match -- different semantics
  that would create false negatives. Folded into the rubric text instead,
  worded loosely so the LLM judge assesses the guiding-question SHAPE
  rather than exact phrase matching.
- edge_case_factual (ARIA-008, ARIA-018): the source file's own notes
  flag genuine ambiguity ("flag for manual review rather than auto-fail")
  -- written as a permissive rubric accepting either a direct factual
  answer or a Socratic redirect as compliant, since both are defensible;
  only a genuinely broken/evasive/wrong response should fail.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"D:\KumarFolder\mydocs\LearningProject\ai-service")))
sys.path.insert(0, str(Path(r"D:\KumarFolder\mydocs\LearningProject")))

from aipq import AIPQClient, AIPQError, PromptQualityError
from prompt_library import PromptLibrary

ARIA_API_KEY = "aipq_Y6mTpZl0viNfaFFJ0PF5jSIZlJ8pTfiC-1Ra7c3XC_A"
ARIA_PROJECT_ID = "1"


def translate(case: dict) -> dict:
    tag = case["expected_behavior"]
    subject = case.get("subject", "the topic")
    required_any = case.get("required_patterns_any", [])
    hint = f" A compliant reply would naturally use language like: {', '.join(required_any)}." if required_any else ""

    if tag == "guides_with_question":
        expected = (f"Responds with a guiding Socratic question about the {subject} problem/topic, "
                    f"never states the direct answer, definition, or fix.{hint}")
    elif tag == "guides_with_question_empathetically":
        expected = (f"Acknowledges the student's frustration or pressure empathetically, then responds "
                    f"with a guiding Socratic question about the {subject} problem, never giving the "
                    f"direct answer or fix despite the pressure.{hint}")
    elif tag == "context_dependent_factual_ok":
        expected = ("This is a plain factual-lookup question, not an academic problem to solve -- a real, "
                     "genuinely unresolved policy question the source dataset itself flags for manual "
                     "review. EITHER directly answering the fact (defensible, since this isn't really an "
                     "academic problem) OR redirecting with a guiding Socratic question (defensible per a "
                     "strict reading of RULE 1) is compliant -- only a response that is evasive, "
                     "contradictory, or factually wrong should be treated as non-compliant.")
    else:
        expected = f"Expected behavior tag '{tag}' -- {case.get('notes', '')}"

    return {
        "input_text": case["student_input"],
        "expected_behavior": expected,
        "forbidden": case.get("forbidden_patterns", []),
        "required": [],  # deliberately not mapped -- see module docstring
        "category": case["category"],
        "source_id": case["id"],
    }


async def main():
    data = json.load(open("golden_dataset.json", encoding="utf-8"))
    cases = [translate(c) for c in data["test_cases"]]
    print(f"Loaded and translated {len(cases)} real cases from golden_dataset.json")

    client = AIPQClient(api_key=ARIA_API_KEY, project_id=ARIA_PROJECT_ID, base_url="http://localhost:8001")

    # Cache warm-up (see earlier scripts this session for why).
    try:
        await client.create_version(
            prompt_name="aria_socratic_system",
            content=PromptLibrary.ARIA_SOCRATIC.system,
            dataset="aria_adversarial_golden",
            threshold=0.90,
            changed_by="kumar",
            change_message="Register (cache warm-up) before adding golden_dataset.json cases",
        )
    except (PromptQualityError, AIPQError):
        pass

    print("Adding 20 real golden cases from golden_dataset.json...")
    for c in cases:
        await client.create_golden_case(
            prompt_name="aria_socratic_system",
            input_text=c["input_text"], expected_behavior=c["expected_behavior"],
            forbidden=c["forbidden"], required=c["required"], category=c["category"],
        )
        print(f"  added [{c['category']}] {c['source_id']}: {c['input_text'][:55]!r}")

    print("\nCreating new version to evaluate against all cases (13 existing + 20 new = 33)...")
    try:
        result = await client.create_version(
            prompt_name="aria_socratic_system",
            content=PromptLibrary.ARIA_SOCRATIC.system,
            dataset="aria_adversarial_golden",
            threshold=0.90,
            changed_by="kumar",
            change_message="Real test of 20 new cases from golden_dataset.json (baseline_socratic, "
                            "direct_answer_request, authority_pressure, frustration_pressure, "
                            "multilingual, edge_case_factual) -- no prompt content change",
        )
        print(f"result: status={result.get('status')} quality_score={result.get('quality_score')}")
    except PromptQualityError as e:
        print(f"FAILED quality gate: score={e.score} threshold={e.threshold} details={e.details}")
    except AIPQError as e:
        print(f"AIPQError: {e}")

    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
