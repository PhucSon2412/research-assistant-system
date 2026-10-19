"""Uniform error bodies: ``{"error": "<code>"}``.

Validation errors list only field locations and error types. FastAPI's
default body echoes the submitted ``input``, which could be a password or a
token, so it is replaced.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.cookies import clear_session_cookie
from app.config import get_settings


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, *, message: str | None = None, clear_session: bool = False,
                 headers: dict[str, str] | None = None, extra: dict[str, int | str] | None = None):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.clear_session = clear_session
        self.headers = headers or {}
        self.extra = extra or {}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        body: dict[str, int | str] = {**exc.extra, "error": exc.code}
        if exc.message:
            body["message"] = exc.message
        response = JSONResponse(body, status_code=exc.status_code, headers=exc.headers)
        if exc.clear_session:
            clear_session_cookie(response, get_settings())
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"loc": [str(part) for part in err.get("loc", ())], "type": str(err.get("type", ""))}
            for err in exc.errors()
        ]
        return JSONResponse({"error": "invalid_request", "fields": fields}, status_code=422)
