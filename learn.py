"""
learn.py — Self-Learning Loop
~/mission-control/learn.py

After every chat exchange, a FREE model (Llama 8B on NVIDIA NIM) silently
extracts durable facts worth remembering and writes them to shared memory.
Zero Anthropic tokens. Zero user effort. The system's knowledge compounds
with every command.

What counts as "learning" here (honest version):
  - NOT retraining the model (impossible client-side)
  - YES: accumulating facts, preferences, decisions, corrections into
    Redis + the local index, so every future reply retrieves better context.

Dedup: before storing, we check the index for near-identical facts.
"""
import os
import json
import logging
import hashlib

from openai import OpenAI

logger = logging.getLogger(__name__)

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
EXTRACT_MODEL = "meta/llama-3.1-8b-instruct"   # free + fast — perfect for this

_client = None
def _nvidia():
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=NVIDIA_BASE_URL,
            api_key=os.environ.get("NVIDIA_API_KEY", ""),
        )
    return _client


EXTRACT_PROMPT = """Extract durable facts from this exchange between Alex and his assistant.

A durable fact is something worth remembering NEXT WEEK: a preference, a decision, \
a project update, a correction, personal info, a commitment.
NOT durable: greetings, one-off questions, generic info, anything already obvious.

Reply ONLY with a JSON array (no markdown, no preamble). Each item:
{{"content": "<fact in one sentence, in the language it was stated>", "category": "preference|decision|project_update|correction|personal|general"}}

If nothing is worth remembering, reply: []

Exchange:
USER: {user_msg}
ASSISTANT: {assistant_msg}"""


def extract_facts(user_msg: str, assistant_msg: str) -> list[dict]:
    """Extract 0-3 durable facts from an exchange. Free model, ~1s."""
    try:
        resp = _nvidia().chat.completions.create(
            model=EXTRACT_MODEL,
            messages=[{
                "role": "user",
                "content": EXTRACT_PROMPT.format(
                    user_msg=user_msg[:1500],
                    assistant_msg=assistant_msg[:1500],
                ),
            }],
            max_tokens=300,
            temperature=0.0,
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        facts = json.loads(raw)
        if not isinstance(facts, list):
            return []
        valid = []
        for f in facts[:3]:
            if isinstance(f, dict) and f.get("content") and len(f["content"]) > 10:
                valid.append({
                    "content": f["content"][:400],
                    "category": f.get("category", "general"),
                })
        return valid
    except Exception as e:
        logger.warning(f"Fact extraction failed (non-fatal): {e}")
        return []


def content_hash(text: str) -> str:
    """Stable hash for dedup of near-identical facts."""
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def learn_from_exchange(user_msg: str, assistant_msg: str, store_fn, search_fn) -> int:
    """
    Full learning step. Called async after each chat reply.
      store_fn(content, category, source) -> fact_id   (writes to Redis)
      search_fn(query, k) -> list[dict]                (checks the local index)
    Returns number of new facts stored.
    """
    facts = extract_facts(user_msg, assistant_msg)
    stored = 0
    for f in facts:
        # dedup: if a very similar fact already indexed, skip
        similar = search_fn(f["content"], k=1)
        if similar and similar[0].get("score", 0) > 0.85:
            continue
        store_fn(f["content"], f["category"], source="janet-learn")
        stored += 1
    if stored:
        logger.info(f"Learned {stored} new fact(s) from exchange")
    return stored
