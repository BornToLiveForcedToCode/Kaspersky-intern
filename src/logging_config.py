from __future__ import annotations

import logging
from time import perf_counter
from uuid import uuid4

from starlette.requests import Request

logger = logging.getLogger("vllm.audit")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(handler)


async def audit_middleware(request: Request, call_next):
    """Log that an inference API route was called, without logging its content."""
    path = request.url.path
    is_inference_route = path.startswith(("/v1/", "/v2/", "/inference"))
    if not is_inference_route:
        return await call_next(request)

    request_id = uuid4().hex
    started = perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception as exc:
        # Do not use logger.exception(): exception messages/tracebacks can
        # occasionally include data derived from request payloads.
        logger.warning(
            "inference_request_failed request_id=%s method=%s status=%d "
            "error_type=%s duration_ms=%.2f",
            request_id,
            request.method,
            status_code,
            type(exc).__name__,
            (perf_counter() - started) * 1000,
        )
        raise
    finally:
        logger.info(
            "inference_request request_id=%s method=%s status=%d duration_ms=%.2f",
            request_id,
            request.method,
            status_code,
            (perf_counter() - started) * 1000,
        )
