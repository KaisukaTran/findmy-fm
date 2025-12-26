from fastapi import FastAPI

from findmy.api.sot.routes import router as sot_router
# (nếu có audit)
# from findmy.api.audit.routes import router as audit_router

app = FastAPI(
    title="FINDMY API",
    version="0.6.1",
)

# 🔴 BẮT BUỘC PHẢI CÓ
app.include_router(sot_router)

# (optional)
# app.include_router(audit_router)
