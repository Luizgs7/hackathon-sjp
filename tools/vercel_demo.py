"""Vercel entrypoint for the disposable public prototype, not municipal production."""
import os
import re
import secrets
import tempfile
import time
import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

DATA = Path(tempfile.gettempdir()) / "dataforge-prototype"
DATA.mkdir(parents=True, exist_ok=True)
os.environ.update({
    "DEMO_DATA_DIR": str(DATA), "PLATAFORMA_DB": str(DATA / "plataforma.db"),
    "LEGADO_DB": str(DATA / "legado.db"),
    "LEGADO_URL": "http://dataforge.internal/legado",
    "PLATAFORMA_URL": "http://dataforge.internal",
    "API_KEY_LEGADO": secrets.token_urlsafe(32),
    "SESSION_SECRET": os.getenv("SESSION_SECRET") or secrets.token_urlsafe(48),
    "SEED_HISTORICO": "true",
})

import httpx
import produto
import legado
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.testclient import TestClient

app = produto.app
INTERNAL_SECRET = secrets.token_urlsafe(32)

@asynccontextmanager
async def demo_lifespan(_app):
    # Serverless requests cannot depend on an always-running outbox worker.
    yield

app.router.lifespan_context = demo_lifespan
produto.init_db()
produto.em_segundo_plano = lambda fn, *args: fn(*args)

class LocalIntegration:
    """Keep both simulated applications and their API contracts in one function."""
    def __getattr__(self, name):
        return getattr(httpx, name)

    def post(self, url, **kwargs):
        if url.startswith("http://dataforge.internal/"):
            with TestClient(app, raise_server_exceptions=False) as client:
                kwargs.pop("timeout", None)
                kwargs.setdefault("headers", {})["x-dataforge-internal"] = INTERNAL_SECRET
                return client.post(url.removeprefix("http://dataforge.internal"), **kwargs)
        return httpx.post(url, **kwargs)

produto.httpx = legado.httpx = LocalIntegration()

@legado.app.middleware("http")
async def legacy_prefix(request, call_next):
    response = await call_next(request)
    location = response.headers.get("location")
    if location and location.startswith("/"):
        response.headers["location"] = "/legado" + location
    if "text/html" in response.headers.get("content-type", ""):
        body = b"".join([part async for part in response.body_iterator]).decode("utf-8")
        body = re.sub(r'((?:action|hx-get|hx-post)=[\"\'])(/[^\"\']*)', r'\1/legado\2', body)
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return HTMLResponse(body, status_code=response.status_code, headers=headers)
    return response

app.mount("/legado", legado.app)
REQUESTS = defaultdict(deque)

@app.middleware("http")
async def prototype_controls(request, call_next):
    if request.method == "POST":
        identity = request.headers.get("x-vercel-forwarded-for") or (request.client.host if request.client else "unknown")
        now = time.monotonic()
        recent = REQUESTS[identity]
        while recent and recent[0] < now - 60:
            recent.popleft()
        if len(recent) >= 20:
            return JSONResponse({"detail": "Muitas tentativas. Aguarde um minuto e tente novamente."},
                                status_code=429, headers={"Retry-After": "60"})
        recent.append(now)
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    if "text/html" in response.headers.get("content-type", ""):
        response.headers["cache-control"] = "no-store"
    return response

# Only the generated public demo uses Blob. Local product databases stay untouched.
if os.getenv("BLOB_READ_WRITE_TOKEN"):
    from demo_store import BlobStore
    shared = BlobStore(os.environ["BLOB_READ_WRITE_TOKEN"],
                       {"platform": produto.DB_PATH, "legacy": legado.DB_PATH})
    database_lock = asyncio.Lock()

    estado_blob = {"leitura_em": 0.0, "pausado_ate": 0.0}
    LEITURA_VALIDA_S = 4     # várias requisições seguidas reaproveitam o último snapshot (poucas operações no Blob)
    PAUSA_APOS_FALHA_S = 30  # Blob indisponível (403, cota, rede): atende com o banco local e só tenta de novo depois

    @app.middleware("http")
    async def shared_demo_records(request, call_next):
        internal = request.headers.get("x-dataforge-internal", "")
        if secrets.compare_digest(internal, INTERNAL_SECRET) or request.url.path.startswith("/static/"):
            return await call_next(request)
        leitura = request.method in ("GET", "HEAD", "OPTIONS")
        agora = time.monotonic()
        # modo degradado: sem o Blob o site continua no ar, com os dados desta instância (a demonstração não cai)
        if agora < estado_blob["pausado_ate"] or (leitura and agora - estado_blob["leitura_em"] < LEITURA_VALIDA_S):
            return await call_next(request)
        async with database_lock:
            lease = None
            try:
                lease = await shared.load(write=not leitura)
                estado_blob["leitura_em"] = time.monotonic()
                # O snapshot compartilhado pode vir de uma versão anterior: aplica as colunas e tabelas novas antes de atender.
                await asyncio.to_thread(produto.init_db)
                response = await call_next(request)
                body = b"".join([part async for part in response.body_iterator])
                try:
                    await shared.finish(lease, commit=response.status_code < 500)
                except Exception:  # a requisição já foi atendida: não repete; só pausa o Blob por um tempo
                    estado_blob["pausado_ate"] = time.monotonic() + PAUSA_APOS_FALHA_S
                lease = None
                result = Response(body, status_code=response.status_code)
                # Preserve every Set-Cookie header, including the signed Ana profile.
                result.raw_headers = [(k, v) for k, v in response.raw_headers if k != b'content-length']
                result.headers['content-length'] = str(len(body))
                return result
            except Exception:
                if lease:
                    try:
                        await shared.finish(lease, commit=False)
                    except Exception:
                        pass
                estado_blob["pausado_ate"] = time.monotonic() + PAUSA_APOS_FALHA_S
                await asyncio.to_thread(produto.init_db)
                return await call_next(request)
