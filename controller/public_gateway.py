"""Public HTTPS-only entrypoint. Runs in a dedicated Uvicorn container on the EC2.

Does NOT expose controller.main directly. Only a fixed browser allowlist is
forwarded to the isolated public controller over the Docker private network.
The private DComp stack and the demo admin APIs are never reachable here.
"""
import asyncio
import os
import re
import time
from collections import defaultdict, deque

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse

app = FastAPI(title="DComp-Drive public entry", docs_url=None, redoc_url=None, openapi_url=None)

_CONTROLLER = "http://controller:8000"
_METHODS = {
    "/": {"GET"},
    "/status": {"GET"},
    "/health": {"GET"},
    "/metrics": {"GET"},
    "/demo/config": {"GET"},
    "/files/catalog": {"GET"},
    "/files/metadata": {"GET"},
    "/files/entries": {"GET"},
    "/files/content": {"GET", "PUT"},
    "/files/directories": {"POST", "DELETE"},
    "/files/move": {"POST"},
    "/files/entry": {"DELETE"},
    "/demo/public/files/corrupt": {"POST"},
    "/demo/public/files/encryption": {"POST"},
    "/demo/public/reset": {"POST"},
    "/demo/public/nodes/activate-six": {"POST"},
    "/demo/public/persistence/snapshot": {"GET"},
    "/demo/public/persistence/restart": {"POST"},
    "/demo/public/events": {"GET"},
}
_NODE_ACTION = re.compile(r"^/demo/public/nodes/storage-[1-6]/(?:offline|restore|drain|undrain)$")
_STATIC = re.compile(r"^/ui/[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*$")
_OUT_HEADERS = {"content-type", "x-sha256", "x-version", "x-replication-status", "content-disposition"}
_SECURITY = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    "cache-control": "no-store",
}
_LIMIT_BYTES = max(1, int(os.getenv("PUBLIC_GATEWAY_MAX_BODY", "1048576")))
_REQUESTS_PER_MINUTE = max(1, int(os.getenv("PUBLIC_GATEWAY_REQUESTS_PER_MINUTE", "240")))
_hits = defaultdict(deque)
_hits_lock = asyncio.Lock()


def _allowed(method: str, path: str) -> bool:
    if path in _METHODS:
        return method in _METHODS[path]
    if (
        method == "GET"
        and _STATIC.fullmatch(path)
        # Reject dot segments even if they pass the character whitelist.
        # Otherwise /ui/../../etc/passwd could traverse the asset root.
        and all(segment not in (".", "..") for segment in path.split("/"))
    ):
        return True
    if method == "POST" and _NODE_ACTION.fullmatch(path):
        return True
    return False


async def _rate_allowed(addr: str) -> bool:
    now = time.monotonic()
    async with _hits_lock:
        # Limit unbounded growth from spoofed or rotating client addresses.
        if len(_hits) > 2048:
            for key in list(_hits):
                if not _hits[key] or _hits[key][-1] <= now - 60:
                    del _hits[key]
            if len(_hits) > 2048:
                _hits.clear()
        requests = _hits[addr]
        while requests and requests[0] <= now - 60:
            requests.popleft()
        if len(requests) >= _REQUESTS_PER_MINUTE:
            return False
        requests.append(now)
        return True


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in _SECURITY.items():
        response.headers[header] = value
    return response


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def browser_gateway(request: Request, path: str):
    actual_path = "/" + path
    if not _allowed(request.method, actual_path):
        return JSONResponse(status_code=404, content={"detail": "Endpoint indisponível no laboratório público"})
    # Direct connection: use the real TCP peer, never a forwarded user header.
    addr = request.client.host if request.client else "unknown"
    if not await _rate_allowed(addr):
        return JSONResponse(status_code=429, content={"detail": "Limite de requisições atingido"})
    if request.headers.get("content-length"):
        try:
            if int(request.headers["content-length"]) > _LIMIT_BYTES:
                return JSONResponse(status_code=413, content={"detail": "Upload acima do limite de 1 MiB"})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Content-Length inválido"})
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _LIMIT_BYTES:
            return JSONResponse(status_code=413, content={"detail": "Corpo da requisição acima do limite"})
        body.extend(chunk)
    headers = {}
    if "content-type" in request.headers:
        headers["content-type"] = request.headers["content-type"]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(65.0, connect=5.0), follow_redirects=False) as client:
            upstream = await client.request(
                request.method, _CONTROLLER + actual_path, params=request.query_params,
                content=bytes(body), headers=headers,
            )
    except httpx.HTTPError:
        return JSONResponse(status_code=502, content={"detail": "Controlador público temporariamente indisponível"})
    forwarded = {k: v for k, v in upstream.headers.items() if k.lower() in _OUT_HEADERS}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=forwarded)
