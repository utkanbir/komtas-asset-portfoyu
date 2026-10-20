"""Komtaş Asset Portföyü — backend.

Küçük, genel amaçlı bir belge deposu (collection/doc) + değişiklik akışı (SSE).
Ön yüz (web/index.html) Claude artifact'inden birebir alınmıştır; web/claude-shim.js
`window.claude.use("db"|"user"|"downloads")` arayüzünü bu API'ye bağlar.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .auth import User, current_user, require_writer

log = logging.getLogger("asset-portfoyu")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://portfoy:portfoy@localhost:5432/portfoy")
WEB_DIR = Path(os.getenv("WEB_DIR", Path(__file__).resolve().parents[2] / "web"))
SEED_DIR = Path(os.getenv("SEED_DIR", Path(__file__).resolve().parents[2] / "seed"))
SEED_ON_EMPTY = os.getenv("SEED_ON_EMPTY", "true").lower() == "true"
CHANNEL = "doc_changes"
NAME_RE = re.compile(r"^[A-Za-z0-9_\-.~:@+]{1,200}$")
MAX_DOC_BYTES = int(os.getenv("MAX_DOC_BYTES", str(1024 * 1024)))

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  collection  text        NOT NULL,
  id          text        NOT NULL,
  data        jsonb       NOT NULL,
  version     integer     NOT NULL DEFAULT 1,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  updated_by  text,
  PRIMARY KEY (collection, id)
);
CREATE TABLE IF NOT EXISTS document_history (
  hid         bigserial   PRIMARY KEY,
  collection  text        NOT NULL,
  id          text        NOT NULL,
  op          text        NOT NULL,
  data        jsonb,
  version     integer,
  changed_at  timestamptz NOT NULL DEFAULT now(),
  changed_by  text
);
CREATE INDEX IF NOT EXISTS document_history_doc ON document_history (collection, id, hid DESC);
"""


class Hub:
    """Postgres LISTEN/NOTIFY -> bu pod'a bağlı SSE istemcileri. Çoklu replica ile çalışır."""

    def __init__(self) -> None:
        self.queues: set[asyncio.Queue] = set()
        self.conn: asyncpg.Connection | None = None

    async def start(self) -> None:
        self.conn = await asyncpg.connect(DATABASE_URL)
        await self.conn.add_listener(CHANNEL, self._on_notify)

    async def stop(self) -> None:
        if self.conn:
            await self.conn.close()

    def _on_notify(self, _conn, _pid, _channel, payload: str) -> None:
        for q in list(self.queues):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass


hub = Hub()
pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def seed_if_empty() -> None:
    assert pool
    async with pool.acquire() as c:
        n = await c.fetchval("SELECT count(*) FROM documents")
        if n or not SEED_DIR.is_dir():
            return
        total = 0
        for f in sorted(SEED_DIR.glob("*.json")):
            coll = f.stem
            docs: dict[str, Any] = json.loads(f.read_text("utf-8"))
            for doc_id, data in docs.items():
                await c.execute(
                    "INSERT INTO documents(collection,id,data,updated_by) VALUES($1,$2,$3,'seed') ON CONFLICT DO NOTHING",
                    coll, doc_id, data)
                total += 1
        log.info("seed: %d belge yüklendi (%s)", total, SEED_DIR)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global pool
    for attempt in range(30):
        try:
            pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10, init=_init_conn)
            break
        except (OSError, asyncpg.PostgresError) as e:  # DB henüz hazır değil
            log.warning("veritabanına bağlanılamadı (%s), tekrar deneniyor…", e)
            await asyncio.sleep(2)
    else:
        raise RuntimeError("veritabanına bağlanılamadı")
    async with pool.acquire() as c:
        await c.execute("SELECT pg_advisory_lock(424242)")
        try:
            await c.execute(SCHEMA)
        finally:
            await c.execute("SELECT pg_advisory_unlock(424242)")
    if SEED_ON_EMPTY:
        await seed_if_empty()
    await hub.start()
    yield
    await hub.stop()
    await pool.close()


app = FastAPI(title="Komtaş Asset Portföyü", lifespan=lifespan, docs_url="/api/docs-ui", openapi_url="/api/openapi.json")


def _check(*names: str) -> None:
    for n in names:
        if not NAME_RE.match(n or "") or n in (".", ".."):
            raise HTTPException(400, detail={"code": "invalid_argument", "message": f"geçersiz ad: {n!r}"})


def _row(r: asyncpg.Record) -> dict:
    return {"id": r["id"], "data": r["data"], "version": r["version"],
            "updated_at": r["updated_at"].isoformat(), "updated_by": r["updated_by"]}


async def _body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_DOC_BYTES:
        raise HTTPException(413, detail={"code": "invalid_argument", "message": "belge çok büyük"})
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, detail={"code": "invalid_argument", "message": "geçersiz JSON"})
    if not isinstance(data, dict):
        raise HTTPException(400, detail={"code": "invalid_argument", "message": "belge bir JSON nesnesi olmalı"})
    return data


async def _notify(c: asyncpg.Connection, coll: str, doc_id: str, op: str) -> None:
    await c.execute("SELECT pg_notify($1,$2)", CHANNEL, json.dumps({"collection": coll, "id": doc_id, "op": op}))


# ---------------------------------------------------------------- API
@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    assert pool
    async with pool.acquire() as c:
        await c.fetchval("SELECT 1")
    return {"ok": True}


@app.get("/api/me")
async def me(user: User = Depends(current_user)):
    return user.public()


@app.get("/api/collections/{coll}")
async def list_collection(coll: str, order_by: str | None = None, user: User = Depends(current_user)):
    _check(coll)
    assert pool
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM documents WHERE collection=$1 ORDER BY id", coll)
    docs = [_row(r) for r in rows]
    if order_by:
        _check(order_by)

        def key(d):
            v = d["data"].get(order_by)
            if v is None:
                return (2, 0, "")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return (0, v, "")
            return (1, 0, str(v))
        docs.sort(key=key)
    return docs


@app.get("/api/docs/{coll}/{doc_id}")
async def get_doc(coll: str, doc_id: str, user: User = Depends(current_user)):
    _check(coll, doc_id)
    assert pool
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT * FROM documents WHERE collection=$1 AND id=$2", coll, doc_id)
    if not r:
        raise HTTPException(404, detail={"code": "not_found"})
    return _row(r)


@app.put("/api/docs/{coll}/{doc_id}")
async def set_doc(coll: str, doc_id: str, request: Request, user: User = Depends(require_writer)):
    _check(coll, doc_id)
    data = await _body(request)
    assert pool
    async with pool.acquire() as c, c.transaction():
        r = await c.fetchrow(
            """INSERT INTO documents(collection,id,data,updated_by) VALUES($1,$2,$3,$4)
               ON CONFLICT (collection,id) DO UPDATE SET data=EXCLUDED.data, version=documents.version+1,
               updated_at=now(), updated_by=EXCLUDED.updated_by RETURNING *""",
            coll, doc_id, data, user.id)
        await c.execute("INSERT INTO document_history(collection,id,op,data,version,changed_by) VALUES($1,$2,'set',$3,$4,$5)",
                        coll, doc_id, data, r["version"], user.id)
        await _notify(c, coll, doc_id, "set")
    return _row(r)


@app.post("/api/collections/{coll}")
async def add_doc(coll: str, request: Request, user: User = Depends(require_writer)):
    _check(coll)
    data = await _body(request)
    doc_id = secrets.token_urlsafe(15).replace("-", "x").replace("_", "y")
    assert pool
    async with pool.acquire() as c, c.transaction():
        r = await c.fetchrow("INSERT INTO documents(collection,id,data,updated_by) VALUES($1,$2,$3,$4) RETURNING *",
                             coll, doc_id, data, user.id)
        await c.execute("INSERT INTO document_history(collection,id,op,data,version,changed_by) VALUES($1,$2,'add',$3,1,$4)",
                        coll, doc_id, data, user.id)
        await _notify(c, coll, doc_id, "add")
    return _row(r)


@app.delete("/api/docs/{coll}/{doc_id}")
async def delete_doc(coll: str, doc_id: str, user: User = Depends(require_writer)):
    _check(coll, doc_id)
    assert pool
    async with pool.acquire() as c, c.transaction():
        r = await c.fetchrow("DELETE FROM documents WHERE collection=$1 AND id=$2 RETURNING *", coll, doc_id)
        if r:
            await c.execute("INSERT INTO document_history(collection,id,op,data,version,changed_by) VALUES($1,$2,'delete',$3,$4,$5)",
                            coll, doc_id, r["data"], r["version"], user.id)
            await _notify(c, coll, doc_id, "delete")
    return {"ok": True, "deleted": bool(r)}


@app.get("/api/export")
async def export_all(user: User = Depends(current_user)):
    """Tüm veriyi seed biçiminde döker (yedek / taşıma için)."""
    assert pool
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT collection,id,data FROM documents ORDER BY collection,id")
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["collection"], {})[r["id"]] = r["data"]
    return out


@app.get("/api/events")
async def events(request: Request, user: User = Depends(current_user)):
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    hub.queues.add(q)

    async def gen():
        try:
            yield "retry: 3000\nevent: ready\ndata: {}\n\n"
            last = time.monotonic()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: change\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    pass
                if time.monotonic() - last > 15:
                    yield ": ping\n\n"
                    last = time.monotonic()
        finally:
            hub.queues.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Statik dosyalar (docs/ PDF'leri dahil) dahil her şeyi kimlik doğrulamanın arkasında tutar."""
    if request.url.path not in ("/healthz", "/readyz"):
        try:
            current_user(request)
        except HTTPException as exc:
            return JSONResponse(exc.detail, status_code=exc.status_code, headers=getattr(exc, "headers", None))
    return await call_next(request)


@app.exception_handler(HTTPException)
async def http_exc(_req: Request, exc: HTTPException):
    d = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(d, status_code=exc.status_code, headers=getattr(exc, "headers", None))


# ---------------------------------------------------------------- statik ön yüz
@app.get("/")
async def index(user: User = Depends(current_user)):
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
