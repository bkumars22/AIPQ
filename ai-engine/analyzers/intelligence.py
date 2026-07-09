"""
PromptIntelligenceAnalyzer — static/heuristic analysis of a prompt BEFORE
the full evaluation pipeline runs, so obvious gaps (missing multilingual
coverage, over-complexity, near-duplicate of a version that already got
rolled back) can be caught and fixed without burning a full golden-dataset
evaluation cycle.

Not yet wired into version creation (see routers/prompts.py's
create_prompt_version / evaluators/pipeline.py) — this file implements the
analyzer only, per this task's scope. The intended integration: run
analyze_coverage + complexity_score + similarity_to_failed synchronously
when POST /prompts/versions is called, surface generate_recommendations()
in the response so the dashboard can show them immediately, and only then
kick off the background LangGraph evaluation — that ordering is what saves
the "one full eval cycle per change" the docstring below describes.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("aipq.analyzers.intelligence")

# ── Coverage keyword lists ────────────────────────────────────────────────

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "jailbreak_resistance": ["override", "ignore", "bypass", "disable", "unlock"],
    "authority_pressure": ["teacher", "admin", "system", "principal", "developer"],
    "frustration_manipulation": ["please", "struggling", "give up", "just tell me"],
    "prompt_injection": ["system:", "assistant:", "human:"],
    "indirect_leakage": ["therefore", "so the answer", "which means", "equals"],
}

# 5 representative non-English scripts (Unicode block ranges) — checked
# independently of the keyword lists above so multilingual_bypass scores in
# the same clean 20%-per-hit increments as a 5-keyword category would.
SCRIPT_PATTERNS: dict[str, str] = {
    "devanagari": r"[ऀ-ॿ]",  # Hindi, Marathi, ...
    "tamil": r"[஀-௿]",
    "arabic": r"[؀-ۿ]",
    "cjk": r"[一-鿿]",          # Chinese (and shared Japanese kanji)
    "cyrillic": r"[Ѐ-ӿ]",     # Russian, ...
}

RULE_PATTERN = re.compile(r"(?i)\brule\s*\d+\b")
EXAMPLE_PATTERN = re.compile(r"(?i)\bexample\b")
FORBIDDEN_PATTERN = re.compile(r"(?i)\b(forbidden|must not|do not|never)\b")

COMPLEXITY_WARNING_THRESHOLD = 8.0
SIMILARITY_HIGH_RISK_THRESHOLD = 0.85
COVERAGE_GAP_THRESHOLD = 0.7


def _category_score(prompt_lower: str, keywords: list[str]) -> float:
    matched = sum(1 for kw in keywords if kw.lower() in prompt_lower)
    return round(matched / len(keywords), 4) if keywords else 0.0


def _count_rules(prompt: str) -> int:
    """Rules + forbidden-pattern directives — shared by complexity_score and token_efficiency."""
    return len(RULE_PATTERN.findall(prompt)) + len(FORBIDDEN_PATTERN.findall(prompt))


class PromptIntelligenceAnalyzer:
    def __init__(self, embedding_model_name: str = "all-MiniLM-L6-v2"):
        self.embedding_model_name = embedding_model_name
        self._embedder = None  # lazy-loaded, sentence-transformers is a heavy import

    # ── 1. Adversarial category coverage ──────────────────────────────────

    def analyze_coverage(self, prompt: str) -> dict[str, float]:
        prompt_lower = prompt.lower()
        coverage = {
            category: _category_score(prompt_lower, keywords)
            for category, keywords in CATEGORY_KEYWORDS.items()
        }
        scripts_detected = sum(1 for pattern in SCRIPT_PATTERNS.values() if re.search(pattern, prompt))
        coverage["multilingual_bypass"] = round(scripts_detected / len(SCRIPT_PATTERNS), 4)
        return coverage

    # ── 2. Complexity ──────────────────────────────────────────────────────

    def complexity_score(self, prompt: str) -> float:
        rules = len(RULE_PATTERN.findall(prompt))
        examples = len(EXAMPLE_PATTERN.findall(prompt))
        forbidden = len(FORBIDDEN_PATTERN.findall(prompt))
        return round(min(10.0, float(rules + examples + forbidden)), 2)

    # ── 3. Similarity to previously rolled-back versions ──────────────────

    def _load_embedder(self):
        if self._embedder is None:
            from sentence_transformers import SentenceTransformer
            self._embedder = SentenceTransformer(self.embedding_model_name)
        return self._embedder

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(y * y for y in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    async def similarity_to_failed(self, prompt: str, project_id: int) -> float:
        """
        Embeds `prompt` and compares it (cosine similarity) against every
        ROLLED_BACK version's content for this project. Returns the highest
        similarity found (0.0 if there are no rolled-back versions to compare
        against) — callers apply the >0.85 HIGH RISK threshold themselves.
        """
        from db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT pv.content FROM prompt_versions pv
                JOIN prompts p ON p.id = pv.prompt_id
                WHERE p.project_id = $1 AND pv.status = 'ROLLED_BACK'
                """,
                project_id,
            )

        if not rows:
            return 0.0

        try:
            embedder = self._load_embedder()
            prompt_vec = embedder.encode(prompt).tolist()
            best = 0.0
            for row in rows:
                failed_vec = embedder.encode(row["content"]).tolist()
                best = max(best, self._cosine_similarity(prompt_vec, failed_vec))
            return round(best, 4)
        except ImportError:
            logger.warning("sentence-transformers not installed — falling back to word-overlap similarity")
            prompt_words = set(prompt.lower().split())
            best = 0.0
            for row in rows:
                failed_words = set(row["content"].lower().split())
                if not prompt_words or not failed_words:
                    continue
                jaccard = len(prompt_words & failed_words) / len(prompt_words | failed_words)
                best = max(best, jaccard)
            return round(best, 4)

    # ── 4. Token efficiency ────────────────────────────────────────────────

    def token_efficiency(self, prompt: str, best_version_content: Optional[str] = None) -> dict:
        """
        Approximates tokens via whitespace word count (no tokenizer dependency
        for this simple ratio). `best_version_content`, when given, lets the
        caller compare this prompt's efficiency against the best-performing
        version on file — not part of the method's required signature but
        needed to actually do the "compare to best performing version" this
        method is meant to do.
        """
        token_count = len(prompt.split())
        rules_count = _count_rules(prompt)
        efficiency = round(rules_count / token_count, 4) if token_count else 0.0

        result = {"token_count": token_count, "rules_count": rules_count, "efficiency": efficiency}

        if best_version_content:
            best_tokens = len(best_version_content.split())
            best_rules = _count_rules(best_version_content)
            best_efficiency = round(best_rules / best_tokens, 4) if best_tokens else 0.0
            result["best_version_efficiency"] = best_efficiency
            result["efficiency_delta"] = round(efficiency - best_efficiency, 4)

        return result

    # ── 5. Recommendations ─────────────────────────────────────────────────

    def generate_recommendations(self, analysis: dict) -> list[str]:
        recommendations: list[str] = []

        coverage = analysis.get("coverage", {})
        for category, score in coverage.items():
            if score < COVERAGE_GAP_THRESHOLD:
                pct = round(score * 100)
                label = category.replace("_", " ")
                if category == "multilingual_bypass":
                    recommendations.append(
                        f"Add explicit Hindi/Tamil examples to cover multilingual bypass (currently {pct}%)"
                    )
                else:
                    recommendations.append(f"Improve coverage of {label} (currently {pct}%)")

        complexity = analysis.get("complexity")
        if complexity is not None and complexity > COMPLEXITY_WARNING_THRESHOLD:
            recommendations.append(f"Simplify prompt — complexity {complexity} may reduce consistency")

        similarity = analysis.get("similarity_to_failed")
        if similarity is not None and similarity > SIMILARITY_HIGH_RISK_THRESHOLD:
            recommendations.append(
                f"HIGH RISK — {round(similarity * 100)}% similar to a previously rolled-back version"
            )

        return recommendations
