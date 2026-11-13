import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .auth import authenticate, ip_in_networks
from .config import get_clients, get_settings

access = logging.getLogger("linux_mcp.access")

PUBLIC_PATHS = {"/health"}


def resolve_client_ip(peer_ip: str, forwarded_for: str, real_ip: str, trusted_proxies) -> str:
    """
    Resolve the real client IP behind a reverse proxy.
    Forwarding headers are honoured only when the direct peer is a trusted proxy,
    otherwise a caller could spoof its IP by sending X-Forwarded-For itself.
    """
    if not trusted_proxies or not ip_in_networks(peer_ip, trusted_proxies):
        return peer_ip

    # Walk X-Forwarded-For right-to-left, skipping our own proxy chain
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    for hop in reversed(hops):
        if not ip_in_networks(hop, trusted_proxies):
            return hop
    if hops:
        return hops[0]
    return real_ip.strip() or peer_ip


class AuthMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        start = time.perf_counter()
        settings = get_settings()
        request_id = uuid.uuid4().hex
        client_ip = resolve_client_ip(
            peer_ip=request.client.host if request.client else "",
            forwarded_for=request.headers.get("X-Forwarded-For", ""),
            real_ip=request.headers.get("X-Real-IP", ""),
            trusted_proxies=settings.trusted_proxy_networks,
        )
        fields = {
            "event": "http.request",
            "request_id": request_id,
            "client_ip": client_ip,
            "method": request.method,
            "path": request.url.path,
            "user_agent": request.headers.get("User-Agent", ""),
            "session_id": request.headers.get("Mcp-Session-Id"),
        }

        # ── 1. Extract API Key (header only, never query string) ──────────
        auth_header = request.headers.get("Authorization", "")
        raw_key = (
            request.headers.get("X-API-Key")
            or (auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else "")
        )

        # ── 2. Verify API Key + client IP ─────────────────────────────────
        result = authenticate(raw_key, client_ip, get_clients())
        if result.client is None:
            access.warning("request rejected", extra={
                **fields, "status": result.status, "reason": result.reason, "duration_ms": _ms(start),
            })
            if result.status == 403:
                return JSONResponse({"error": "Forbidden"}, status_code=403, headers={"X-Request-ID": request_id})
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "X-Request-ID": request_id},
            )

        # ── 3. Attach identity to request state ───────────────────────────
        request.state.client = result.client
        request.state.client_ip = client_ip
        request.state.request_id = request_id

        # ── 4. Process request ─────────────────────────────────────────────
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id

        # For SSE responses this is the time until the stream started
        access.info("request completed", extra={
            **fields, "client": result.client.alias, "status": response.status_code, "duration_ms": _ms(start),
        })
        return response


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)
