from fastapi import FastAPI

from findmy.api.sot.routes import router as sot_router
# (if audit exists)
# from findmy.api.audit.routes import router as audit_router

app = FastAPI(
    title="FINDMY API",
    version="0.6.1",
)

# 🔴 REQUIRED
app.include_router(sot_router)

# (optional)
# app.include_router(audit_router)
