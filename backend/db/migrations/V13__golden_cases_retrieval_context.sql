-- Optional retrieval context per golden case, for RAG-style prompts (the
-- ones that answer from retrieved documents rather than from the model's
-- own knowledge). NULL for every existing case — AIPQ's cases were written
-- for plain system-prompt evaluation, not RAG, so validators/rag_validator.py
-- treats a case with no context here as "not applicable" rather than an
-- error. Populate it (a JSON array of context strings) to opt a case into
-- RAGAS scoring.
ALTER TABLE golden_cases
    ADD COLUMN retrieval_context JSONB;
