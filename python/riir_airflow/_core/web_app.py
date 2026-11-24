import json
from pathlib import Path

from riir_airflow._lowlevel import health_response
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

_DOCS = Path(__file__).with_name("api_docs")
_SCHEMA = json.loads((_DOCS / "openapi.json").read_text(encoding="utf-8"))
_SWAGGER = (_DOCS / "swagger.html").read_text(encoding="utf-8")
_REDOC = (_DOCS / "redoc.html").read_text(encoding="utf-8")
_OAUTH_REDIRECT = (_DOCS / "oauth2-redirect.html").read_text(encoding="utf-8")


async def h(request: Request) -> Response:
    body, status_code, media_type = health_response()
    return Response(body, status_code=status_code, media_type=media_type)


async def openapi(request: Request) -> JSONResponse:
    root_path = request.scope.get("root_path", "").rstrip("/")
    schema = {**_SCHEMA, "servers": [{"url": root_path}]} if root_path else _SCHEMA
    return JSONResponse(schema)


async def swagger_ui_html(request: Request) -> HTMLResponse:
    root_path = request.scope.get("root_path", "").rstrip("/")
    return HTMLResponse(_SWAGGER.replace("/__ROOT_PATH__", root_path))


async def redoc_html(request: Request) -> HTMLResponse:
    root_path = request.scope.get("root_path", "").rstrip("/")
    return HTMLResponse(_REDOC.replace("/__ROOT_PATH__", root_path))


async def swagger_ui_redirect(request: Request) -> HTMLResponse:
    return HTMLResponse(_OAUTH_REDIRECT)


async def http_error(request: Request, exc: HTTPException) -> Response:
    headers = exc.headers
    if exc.status_code < 200 or exc.status_code in {204, 205, 304}:
        return Response(status_code=exc.status_code, headers=headers)
    if exc.status_code == 405 and headers and headers.get("Allow") == "HEAD, GET":
        headers = {**headers, "Allow": "GET, HEAD"}
    return JSONResponse(
        {"detail": exc.detail}, status_code=exc.status_code, headers=headers
    )


def create_web_app() -> Starlette:
    health_route = Route("/health", h, methods=["GET"])
    # Starlette automatically adds HEAD to GET routes. Keep /health GET-only.
    health_route.methods.remove("HEAD")

    return Starlette(
        routes=[
            Route("/openapi.json", openapi),
            Route("/docs", swagger_ui_html),
            Route("/docs/oauth2-redirect", swagger_ui_redirect),
            Route("/redoc", redoc_html),
            health_route,
        ],
        exception_handlers={HTTPException: http_error},
    )
