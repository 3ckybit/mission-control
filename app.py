"""
Mission Control — Backend
~/mission-control/app.py  (Raspberry Pi)

One central hub: Janet chat (NVIDIA NIM + Claude escalation), shared memory
facts (Upstash Redis), system status, notifications.

Run:  uvicorn app:app --host 0.0.0.0 --port 8080
Access: http://sinefoulis:8080 or http://100.107.28.116:8080 (Tailscale)

Env (.env):
  NVIDIA_API_KEY=nvapi-...
  UPSTASH_REDIS_REST_URL=rediss://...
  UPSTASH_REDIS_REST_TOKEN=...
  ANTHROPIC_API_KEY=sk-ant-...        (optional — /deep escalation)
  DAILY_BUDGET_USD=2.00
"""
import os
import json
import subprocess
import shutil
from datetime import datetime, timezone, date

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from openai import OpenAI

import redis as redis_lib

import index as knowledge_index
from learn import learn_from_exchange

# ── Config ────────────────────────────────────────────────────────────────────
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
DAILY_BUDGET_USD = float(os.environ.get("DAILY_BUDGET_USD", "2.00"))

MODEL_FAST = "meta/llama-3.1-8b-instruct"
MODEL_MAIN = "nvidia/llama-3.3-nemotron-super-49b-v1"
MODEL_LARGE = "qwen/qwen3.5-122b-a10b"
MODEL_CLAUDE = "claude-sonnet-5"  # escalation only

JANET_SYSTEM = """You are Janet — Alex's central AI assistant, running on his \
Raspberry Pi mission control. Alex Vlachos, Seville, Spain. Projects: Taxi \
Dramas (taxidramas24.gr), web agency, Etsy printables, automation stack \
(this Pi, MacBook, Obsidian vault on private GitHub, Upstash Redis shared \
memory).

Personality: sharp, warm, a little playful — like Janet from The Good Place. \
Direct and concise. You can spot problems Alex hasn't mentioned. Greek or \
English — always match Alex's language.

You have shared memory: recent facts appear below. If Alex tells you \
something worth remembering, note it clearly (the system stores it).

{memory_block}"""

nvidia = OpenAI(base_url=NVIDIA_BASE_URL, api_key=NVIDIA_API_KEY)

_redis = None
def get_redis():
    global _redis
    if _redis is None and REDIS_URL:
        _redis = redis_lib.from_url(
            REDIS_URL, password=REDIS_TOKEN,
            socket_connect_timeout=3, socket_timeout=3,
        )
    return _redis


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Mission Control")


class ChatRequest(BaseModel):
    messages: list[dict]          # [{role, content}]
    mode: str = "auto"            # auto | fast | main | deep | claude


class RememberRequest(BaseModel):
    content: str
    category: str = "general"


# ── Memory helpers ────────────────────────────────────────────────────────────
def load_recent_facts(limit: int = 15) -> list[dict]:
    r = get_redis()
    if not r:
        return []
    try:
        ids = [i.decode() if isinstance(i, bytes) else i
               for i in (r.smembers("memory:pending_sync") | r.smembers("memory:synced"))]
        facts = []
        for fid in ids:
            raw = r.get(f"memory:facts:{fid}")
            if raw:
                f = json.loads(raw)
                f["id"] = fid
                facts.append(f)
        facts.sort(key=lambda f: f["timestamp"], reverse=True)
        return facts[:limit]
    except Exception:
        return []


def store_fact(content: str, category: str, source: str = "janet") -> str:
    import uuid
    r = get_redis()
    if not r:
        return ""
    fid = str(uuid.uuid4())
    r.set(f"memory:facts:{fid}", json.dumps({
        "source": source,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "content": content,
    }))
    r.sadd("memory:pending_sync", fid)
    # feed the fast local index immediately — retrieval sees it on the next query
    try:
        knowledge_index.index_fact(fid, content, category)
    except Exception:
        pass
    return fid


# ── Budget tracking (Claude escalation guard) ─────────────────────────────────
def get_spend_today() -> float:
    r = get_redis()
    if not r:
        return 0.0
    try:
        key = f"budget:{date.today().isoformat()}"
        val = r.get(key)
        return float(val) if val else 0.0
    except Exception:
        return 0.0


def add_spend(usd: float):
    r = get_redis()
    if not r:
        return
    try:
        key = f"budget:{date.today().isoformat()}"
        r.incrbyfloat(key, usd)
        r.expire(key, 86400 * 3)
    except Exception:
        pass


# ── Routing ───────────────────────────────────────────────────────────────────
def pick_model(messages: list[dict], mode: str) -> tuple[str, str]:
    """Returns (provider, model). NVIDIA default; Claude only explicit + in budget."""
    if mode == "fast":
        return ("nvidia", MODEL_FAST)
    if mode == "main":
        return ("nvidia", MODEL_MAIN)
    if mode == "deep":
        return ("nvidia", MODEL_LARGE)
    if mode == "claude":
        if not ANTHROPIC_API_KEY:
            return ("nvidia", MODEL_LARGE)   # graceful fallback
        if get_spend_today() >= DAILY_BUDGET_USD:
            return ("nvidia", MODEL_LARGE)   # budget cap → free model
        return ("anthropic", MODEL_CLAUDE)
    # auto: route by length
    last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    n = len(last.split())
    if n < 20:
        return ("nvidia", MODEL_FAST)
    if n < 120:
        return ("nvidia", MODEL_MAIN)
    return ("nvidia", MODEL_LARGE)


def call_nvidia(messages: list[dict], model: str, system: str) -> str:
    resp = nvidia.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}] + messages,
        max_tokens=1024,
        temperature=0.7,
    )
    return resp.choices[0].message.content


def call_claude(messages: list[dict], system: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=MODEL_CLAUDE,
        max_tokens=1024,
        system=system,
        messages=messages,
    )
    # rough cost estimate: Sonnet ~$3/M in, $15/M out — assume ~2k in, 500 out
    add_spend(0.014)
    return resp.content[0].text


# ── API routes ────────────────────────────────────────────────────────────────
@app.post("/api/chat")
def chat(req: ChatRequest, background_tasks: BackgroundTasks):
    # ── RETRIEVAL: only the RELEVANT knowledge, not a dump ──
    # top-5 facts + top-2 vault snippets matched to the actual question.
    # Smarter context, ~half the tokens of the old "last 10 facts" approach.
    last_user = next((m["content"] for m in reversed(req.messages)
                      if m["role"] == "user"), "")
    memory_block = ""
    try:
        hits_facts = knowledge_index.search(last_user, k=5, kind="fact")
        hits_vault = knowledge_index.search(last_user, k=2, kind="vault")
        parts = []
        if hits_facts:
            parts.append("## Relevant memory\n" + "\n".join(
                f"- [{h['category']}] {h['content']}" for h in hits_facts))
        if hits_vault:
            parts.append("## Relevant vault notes\n" + "\n".join(
                f"- ({h['doc_id']}) {h['content'][:300]}" for h in hits_vault))
        memory_block = "\n\n".join(parts)
    except Exception:
        # index unavailable → graceful fallback to recent facts
        facts = load_recent_facts(5)
        if facts:
            memory_block = "## Recent shared memory\n" + "\n".join(
                f"- [{f['category']}] {f['content']}" for f in facts)
    system = JANET_SYSTEM.format(memory_block=memory_block)

    provider, model = pick_model(req.messages, req.mode)
    try:
        if provider == "anthropic":
            text = call_claude(req.messages, system)
        else:
            text = call_nvidia(req.messages, model, system)

        # ── LEARNING LOOP: extract durable facts in the background ──
        # Free model (Llama 8B), runs AFTER the response is sent — zero
        # added latency for Alex, zero Anthropic tokens.
        background_tasks.add_task(
            learn_from_exchange, last_user, text,
            store_fact, knowledge_index.search,
        )

        return {"reply": text, "provider": provider, "model": model,
                "spend_today": round(get_spend_today(), 3)}
    except Exception as e:
        return {"reply": f"⚠️ Error: {e}", "provider": provider, "model": model}


@app.get("/api/knowledge/stats")
def knowledge_stats():
    """Index size + growth — feeds a 'brain size' widget on the dashboard."""
    return knowledge_index.stats()


@app.post("/api/knowledge/reindex")
def knowledge_reindex():
    """Pull vault mirror + rebuild vault index. Call from cron or the UI."""
    n = knowledge_index.refresh_vault_and_reindex()
    return {"ok": True, "vault_chunks": n}


@app.post("/api/remember")
def remember(req: RememberRequest):
    fid = store_fact(req.content, req.category)
    return {"ok": bool(fid), "id": fid}


@app.get("/api/facts")
def facts(limit: int = 25):
    return {"facts": load_recent_facts(limit)}


@app.get("/api/status")
def status():
    """Pi vitals + services + budget — feeds the dashboard tiles."""
    def sh(cmd: list[str]) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return ""

    # CPU temp (Pi)
    temp_raw = sh(["cat", "/sys/class/thermal/thermal_zone0/temp"])
    cpu_temp = round(int(temp_raw) / 1000, 1) if temp_raw.isdigit() else None

    # Load + uptime
    load1 = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
    uptime = sh(["uptime", "-p"])

    # Disk
    du = shutil.disk_usage("/")
    disk_pct = round(du.used / du.total * 100, 1)

    # Services
    def svc(name: str) -> str:
        out = sh(["systemctl", "is-active", name])
        return out or "unknown"

    # Redis reachability
    redis_ok = False
    try:
        r = get_redis()
        redis_ok = bool(r and r.ping())
    except Exception:
        redis_ok = False

    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "cpu_temp_c": cpu_temp,
        "load_1m": load1,
        "uptime": uptime,
        "disk_used_pct": disk_pct,
        "services": {
            "telegram-agent": svc("telegram-agent"),
            "tailscaled": svc("tailscaled"),
        },
        "redis": "connected" if redis_ok else "down",
        "nvidia_key": bool(NVIDIA_API_KEY),
        "claude_key": bool(ANTHROPIC_API_KEY),
        "spend_today_usd": round(get_spend_today(), 3),
        "budget_usd": DAILY_BUDGET_USD,
    }


@app.get("/api/notifications")
def notifications():
    """Alerts/tips: derived from status + Redis notifications list."""
    notes = []
    st = status()
    if st["services"]["telegram-agent"] != "active":
        notes.append({"level": "critical", "text": "Telegram bot service down — systemctl restart telegram-agent"})
    if st["cpu_temp_c"] and st["cpu_temp_c"] > 75:
        notes.append({"level": "warn", "text": f"Pi running hot: {st['cpu_temp_c']}°C"})
    if st["disk_used_pct"] > 85:
        notes.append({"level": "warn", "text": f"Disk {st['disk_used_pct']}% full"})
    if st["redis"] != "connected":
        notes.append({"level": "critical", "text": "Redis unreachable — shared memory offline"})
    if st["spend_today_usd"] > st["budget_usd"] * 0.8:
        notes.append({"level": "warn", "text": f"Claude spend at {st['spend_today_usd']}$ / {st['budget_usd']}$ budget"})

    # custom notifications pushed by other agents: list "notifications"
    r = get_redis()
    if r:
        try:
            for raw in r.lrange("notifications", 0, 10):
                notes.append(json.loads(raw))
        except Exception:
            pass

    if not notes:
        notes.append({"level": "ok", "text": "All systems nominal ✨"})
    return {"notifications": notes}


# ── Static frontend ───────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def index():
    return FileResponse("static/index.html")
