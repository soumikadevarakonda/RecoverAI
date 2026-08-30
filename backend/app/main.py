from fastapi import FastAPI

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
