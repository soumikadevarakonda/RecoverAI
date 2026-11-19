from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import dev, webhooks, merchant
from app.core.config import settings


app = FastAPI(
    title="RecoverAI",
    description="Agentic merchant revenue recovery system",
    version="0.1.0",
)

# The control plane UI is served from a different origin (frontend/serve.py on
# :5173), so the browser needs CORS to call the API directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Merchant-ID"],
)


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "recoverai-api",
    }


app.include_router(
    webhooks.router,
    prefix="/api/v1",
)

app.include_router(
    merchant.router,
    prefix="/api/v1",
)

if not settings.is_production:
    app.include_router(
        dev.router,
        prefix="/api/v1",
    )
