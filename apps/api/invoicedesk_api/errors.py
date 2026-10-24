"""Contract error shape: ``{"error": {"code": "<slug>", "message": "<plain English>"}}``."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from invoicedesk.contracts.preprocess import RejectedInput
from invoicedesk.masking import mask_text
from invoicedesk.pipeline.corrections import InvalidCorrection
from invoicedesk.pipeline.services import DuplicateVote, NotAnApprover, SelfApproval
from invoicedesk.status import IllegalTransition
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = ["ApiError", "error_body", "install_error_handlers"]

log = logging.getLogger("invoicedesk.api")

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "rejected_input",
    422: "validation_error",
    429: "rate_limited",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_body(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": mask_text(message) or ""}}


def _json(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content=error_body(code, message))


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _json(exc.status, exc.code, exc.message)

    @app.exception_handler(SelfApproval)
    async def _self(_: Request, exc: SelfApproval) -> JSONResponse:
        return _json(403, "self_approval", "You uploaded this invoice, so you cannot approve it.")

    @app.exception_handler(NotAnApprover)
    async def _not_approver(_: Request, exc: NotAnApprover) -> JSONResponse:
        return _json(403, "forbidden", "Only an approver or owner can vote on a held invoice.")

    @app.exception_handler(DuplicateVote)
    async def _dup(_: Request, exc: DuplicateVote) -> JSONResponse:
        return _json(409, "duplicate_vote", "You already voted on this invoice.")

    @app.exception_handler(InvalidCorrection)
    async def _bad_corr(_: Request, exc: InvalidCorrection) -> JSONResponse:
        return _json(422, "invalid_correction", str(exc))

    @app.exception_handler(IllegalTransition)
    async def _illegal(_: Request, exc: IllegalTransition) -> JSONResponse:
        return _json(409, "illegal_transition", str(exc))

    @app.exception_handler(RejectedInput)
    async def _rejected(_: Request, exc: RejectedInput) -> JSONResponse:
        return _json(422, "rejected_input", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        msg = first.get("msg", "invalid request")
        return _json(422, "validation_error", f"{loc}: {msg}" if loc else str(msg))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "error")
        detail = exc.detail if isinstance(exc.detail, str) else code
        return _json(exc.status_code, code, detail)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error: %s", mask_text(f"{type(exc).__name__}: {exc}"))
        return _json(500, "internal_error", "Something went wrong on our side. Try again.")
