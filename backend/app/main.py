from fastapi import FastAPI

from app.api.routes import webhooks, merchant


app = FastAPI(
    title="RecoverAI",
    description="Agentic merchant revenue recovery system",
    version="0.1.0",
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
