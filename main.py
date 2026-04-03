import os
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import asyncpg
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

DATABASE_URL = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1)  # works for both Railway and Ramplify
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "changeme")
# Default access duration in days (override with ACCESS_DURATION_DAYS env var)
DEFAULT_ACCESS_DAYS = int(os.environ.get("ACCESS_DURATION_DAYS", "90"))

# Rate limiting: requests per minute per user (override with RATE_LIMIT_RPM env var)
RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "10"))
# Set ALLOWLIST_ENABLED=false to open registration to anyone
ALLOWLIST_ENABLED = os.environ.get("ALLOWLIST_ENABLED", "true").lower() == "true"
# In-memory sliding window: user_id -> list of request timestamps
_rate_buckets: dict[int, list[float]] = defaultdict(list)

pool: asyncpg.Pool = None
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)
    yield
    await pool.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")


# ── helpers ──────────────────────────────────────────────────────────────────

def generate_key() -> str:
    return "tc-" + secrets.token_urlsafe(32)


async def get_user_by_key(key: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE api_key = $1 AND active = TRUE", key
        )


def check_expiry(user) -> bool:
    """Returns True if the user's key is still valid."""
    if user["expires_at"] is None:
        return True
    now = datetime.now(timezone.utc)
    expires = user["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return now < expires


def check_rate_limit(user_id: int) -> tuple[bool, int]:
    """Sliding window rate limiter. Returns (allowed, retry_after_seconds)."""
    now = time.monotonic()
    window = 60.0
    bucket = _rate_buckets[user_id]
    # Drop timestamps outside the window
    cutoff = now - window
    _rate_buckets[user_id] = [t for t in bucket if t > cutoff]
    if len(_rate_buckets[user_id]) >= RATE_LIMIT_RPM:
        oldest = _rate_buckets[user_id][0]
        retry_after = int(window - (now - oldest)) + 1
        return False, retry_after
    _rate_buckets[user_id].append(now)
    return True, 0


# ── healthcheck ───────────────────────────────────────────────────────────────

@app.get("/healthcheck")
def healthcheck():
    return {"status": "ok"}


# ── pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {})

@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    return templates.TemplateResponse(request, "chat.html", {})


# ── registration ──────────────────────────────────────────────────────────────

@app.post("/register")
async def register(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    if not name or not email:
        raise HTTPException(400, "name and email are required")

    # Allow caller to pass explicit expires_at (ISO string) or days_valid int
    # Otherwise fall back to the server default
    expires_at = None
    if body.get("expires_at"):
        expires_at = datetime.fromisoformat(body["expires_at"]).replace(tzinfo=timezone.utc)
    elif body.get("days_valid") is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=int(body["days_valid"]))
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(days=DEFAULT_ACCESS_DAYS)

    key = generate_key()
    async with pool.acquire() as conn:
        if ALLOWLIST_ENABLED:
            on_list = await conn.fetchrow("SELECT id FROM allowlist WHERE email = $1", email)
            if not on_list:
                raise HTTPException(403, "This email isn't on the guest list. Make sure you RSVPed with this address.")
        existing = await conn.fetchrow("SELECT id FROM users WHERE email = $1", email)
        if existing:
            raise HTTPException(409, "Email already registered")
        await conn.execute(
            "INSERT INTO users (name, email, api_key, expires_at) VALUES ($1, $2, $3, $4)",
            name, email, key, expires_at,
        )
    return {
        "api_key": key,
        "expires_at": expires_at.isoformat(),
        "message": f"Welcome {name}! Keep this key safe.",
    }


# ── chat proxy ────────────────────────────────────────────────────────────────

@app.post("/chat")
async def chat(request: Request, x_api_key: str = Header(...)):
    user = await get_user_by_key(x_api_key)
    if not user:
        raise HTTPException(401, "Invalid or inactive API key")

    if not check_expiry(user):
        raise HTTPException(403, "Your access has expired. Reach out to the Ramp recruiting team.")

    allowed, retry_after = check_rate_limit(user["id"])
    if not allowed:
        raise HTTPException(
            429,
            detail=f"Slow down! You're sending messages too fast. Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

    body = await request.json()

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": body.get("model", "claude-sonnet-4-20250514"),
                "max_tokens": body.get("max_tokens", 1024),
                "messages": body.get("messages", []),
            },
        )

    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.text)

    data = resp.json()
    input_tokens = data.get("usage", {}).get("input_tokens", 0)
    output_tokens = data.get("usage", {}).get("output_tokens", 0)

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO usage_logs (user_id, input_tokens, output_tokens)
               VALUES ($1, $2, $3)""",
            user["id"], input_tokens, output_tokens,
        )

    return data


# ── streaming chat proxy ─────────────────────────────────────────────────────

@app.post("/chat-stream")
async def chat_stream(request: Request, x_api_key: str = Header(...)):
    user = await get_user_by_key(x_api_key)
    if not user:
        raise HTTPException(401, "Invalid or inactive API key")

    if not check_expiry(user):
        raise HTTPException(403, "Your access has expired. Reach out to the Ramp recruiting team.")

    allowed, retry_after = check_rate_limit(user["id"])
    if not allowed:
        raise HTTPException(
            429,
            detail=f"Slow down! You're sending messages too fast. Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

    body = await request.json()

    async def stream_response():
        input_tokens = 0
        output_tokens = 0
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": body.get("model", "claude-sonnet-4-20250514"),
                    "max_tokens": body.get("max_tokens", 4096),
                    "messages": body.get("messages", []),
                    "stream": True,
                },
            ) as resp:
                async for line in resp.aiter_lines():
                    if line:
                        yield f"{line}\n"
                        # track token usage from stream events
                        if line.startswith("data: "):
                            try:
                                import json as _json
                                evt = _json.loads(line[6:])
                                if evt.get("type") == "message_start":
                                    usage = evt.get("message", {}).get("usage", {})
                                    input_tokens = usage.get("input_tokens", 0)
                                elif evt.get("type") == "message_delta":
                                    usage = evt.get("usage", {})
                                    output_tokens = usage.get("output_tokens", 0)
                            except Exception:
                                pass
        # log usage after stream completes
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO usage_logs (user_id, input_tokens, output_tokens) VALUES ($1, $2, $3)",
                user["id"], input_tokens, output_tokens,
            )

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── leaderboard (public) ──────────────────────────────────────────────────────

@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard(request: Request):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.name,
                   COALESCE(SUM(l.input_tokens + l.output_tokens), 0) AS total_tokens,
                   COALESCE(SUM(l.input_tokens), 0)  AS input_tokens,
                   COALESCE(SUM(l.output_tokens), 0) AS output_tokens,
                   COUNT(l.id) AS num_requests,
                   MAX(l.created_at) AS last_active
            FROM users u
            LEFT JOIN usage_logs l ON l.user_id = u.id
            WHERE u.active = TRUE
            GROUP BY u.id, u.name
            ORDER BY total_tokens DESC
            """
        )
    return templates.TemplateResponse(request, "leaderboard.html", {"rows": rows})


# ── admin (internal) ──────────────────────────────────────────────────────────

@app.get("/admin")
async def admin(request: Request, secret: str = ""):
    if secret != ADMIN_SECRET:
        raise HTTPException(403, "Forbidden")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.name, u.email, u.api_key, u.active, u.expires_at,
                   COALESCE(SUM(l.input_tokens + l.output_tokens), 0) AS total_tokens,
                   COUNT(l.id) AS num_requests,
                   MAX(l.created_at) AS last_active
            FROM users u
            LEFT JOIN usage_logs l ON l.user_id = u.id
            GROUP BY u.id, u.name, u.email, u.api_key, u.active, u.expires_at
            ORDER BY total_tokens DESC
            """
        )
    now = datetime.now(timezone.utc)
    return templates.TemplateResponse(request, "admin.html", {"rows": rows, "now": now})



@app.get("/admin/allowlist")
async def get_allowlist(secret: str = ""):
    if secret != ADMIN_SECRET:
        raise HTTPException(403, "Forbidden")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.email, a.added_at, u.id AS user_id
            FROM allowlist a
            LEFT JOIN users u ON u.email = a.email
            ORDER BY a.added_at DESC
            """
        )
    return [{"email": r["email"], "added_at": r["added_at"].isoformat(), "registered": r["user_id"] is not None} for r in rows]


@app.post("/admin/allowlist/add")
async def add_to_allowlist(request: Request):
    body = await request.json()
    if body.get("secret") != ADMIN_SECRET:
        raise HTTPException(403, "Forbidden")
    emails = [e.strip().lower() for e in body.get("emails", []) if e.strip()]
    if not emails:
        raise HTTPException(400, "No emails provided")
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO allowlist (email) VALUES ($1) ON CONFLICT DO NOTHING",
            [(e,) for e in emails],
        )
    return {"added": len(emails)}


@app.post("/admin/allowlist/remove")
async def remove_from_allowlist(request: Request):
    body = await request.json()
    if body.get("secret") != ADMIN_SECRET:
        raise HTTPException(403, "Forbidden")
    email = body.get("email", "").strip().lower()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM allowlist WHERE email = $1", email)
    return {"ok": True}


@app.post("/admin/deactivate")
async def deactivate_user(request: Request):
    body = await request.json()
    if body.get("secret") != ADMIN_SECRET:
        raise HTTPException(403, "Forbidden")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET active = FALSE WHERE email = $1", body["email"])
    return {"ok": True}


@app.post("/admin/set-expiry")
async def set_expiry(request: Request):
    body = await request.json()
    if body.get("secret") != ADMIN_SECRET:
        raise HTTPException(403, "Forbidden")
    expires_at = datetime.fromisoformat(body["expires_at"]).replace(tzinfo=timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET expires_at = $1 WHERE email = $2",
            expires_at, body["email"]
        )
    return {"ok": True}
