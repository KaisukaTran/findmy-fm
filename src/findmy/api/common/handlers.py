from fastapi import Request
from fastapi.responses import JSONResponse
from findmy.api.common.errors import ErrorResponse


async def value_error_handler(request: Request, exc: ValueError):
    trace_id = request.state.trace_id
    return JSONResponse(
        status_code=400,
        content=ErrorResponse(
            error_code="INVALID_REQUEST",
            message=str(exc),
            trace_id=trace_id,
        ).dict(),
    )
