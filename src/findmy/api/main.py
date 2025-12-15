from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from pathlib import Path
import shutil
import uuid

from findmy.execution.paper_execution import run_paper_execution

# ✅ 1. KHAI BÁO APP TRƯỚC
app = FastAPI(
    title="FINDMY FM – Paper Trading API",
    version="1.0",
)

# (optional) health check
@app.get("/")
def health_check():
    return {"status": "ok", "service": "FINDMY FM API"}

# ✅ 2. SAU ĐÓ MỚI ĐƯỢC DÙNG @app.post
@app.post("/paper-execution")
async def paper_execution(file: UploadFile = File(...)):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Only Excel files are supported")

    saved_path = Path("data/uploads") / file.filename
    saved_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with saved_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        result = run_paper_execution(str(saved_path))

        return {
            "status": "success",
            "result": result,
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
