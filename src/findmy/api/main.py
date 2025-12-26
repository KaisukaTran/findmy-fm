from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from pathlib import Path
import shutil
import uuid
import os
from typing import Optional

from findmy.execution.paper_execution import run_paper_execution

# ✅ 1. KHAI BÁO APP TRƯỚC
app = FastAPI(
    title="FINDMY FM – Paper Trading API",
    version="1.0",
)

# Environment configuration
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "data/uploads"))
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_MIME_TYPES = {
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# (optional) health check
@app.get("/")
def health_check():
    return {"status": "ok", "service": "FINDMY FM API"}

# ✅ 2. SAU ĐÓ MỚI ĐƯỢC DÙNG @app.post
@app.post("/paper-execution")
async def paper_execution(file: UploadFile = File(...)):
    """
    Execute paper trading orders from an Excel file.
    
    Args:
        file: Excel file containing purchase orders (MIME type must be Excel).
    
    Returns:
        JSON response with execution results including orders, trades, and positions.
        
    Raises:
        HTTPException: 400 if file is not Excel, too large, or missing headers.
        HTTPException: 500 if processing fails.
    """
    # Validate MIME type
    if file.content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type: {file.content_type}. Only Excel files are supported.",
        )

    # Validate file extension
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=400,
            detail="Only Excel files (.xlsx, .xls) are supported",
        )

    # Validate file size
    if file.size and file.size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Maximum size is {MAX_FILE_SIZE / 1024 / 1024:.0f}MB",
        )

    # Generate safe filename with UUID to prevent collisions
    safe_filename = f"{uuid.uuid4()}_{file.filename}"
    saved_path = UPLOAD_DIR / safe_filename

    try:
        # Write file to disk
        with saved_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # Process the uploaded file
        result = run_paper_execution(str(saved_path))

        return {
            "status": "success",
            "result": result,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid Excel file: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Processing error: {str(e)}")
    finally:
        # Clean up uploaded file after processing
        try:
            if saved_path.exists():
                saved_path.unlink()
        except Exception as e:
            # Log cleanup error but don't fail the response
            print(f"Warning: Failed to delete temporary file {saved_path}: {e}")
