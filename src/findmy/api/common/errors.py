from pydantic import BaseModel
from typing import Optional


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    trace_id: Optional[str] = None
