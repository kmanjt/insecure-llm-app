import base64
import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from .config import settings

# Endpoints that bypass auth + size limit. /health stays open so external
# probes (Container Apps, monitoring) can hit it without credentials.
_PUBLIC_PATHS = {"/health"}


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return _challenge()
        try:
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
            user, _, password = decoded.partition(":")
        except Exception:
            return _challenge()
        if not (
            hmac.compare_digest(user, settings.basic_auth_username)
            and hmac.compare_digest(password, settings.basic_auth_password)
        ):
            return _challenge()
        return await call_next(request)


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    # Content-Length-based check. Chunked uploads without a length header
    # bypass this; the upload handler enforces a second check after read.
    async def dispatch(self, request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        length = request.headers.get("content-length")
        if length and int(length) > settings.max_upload_bytes:
            return JSONResponse(
                {"detail": f"request body exceeds limit of {settings.max_upload_bytes} bytes"},
                status_code=413,
            )
        return await call_next(request)


def _challenge() -> Response:
    return Response(
        content="authentication required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="insecure-llm-app"'},
    )
