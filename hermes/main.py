from fastapi import FastAPI
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from hermes.config import settings
from hermes.db import init_db
from hermes.telegram_bot import router as telegram_router
from hermes.twilio_voice import router as twilio_router
from hermes.web import router as web_router

app = FastAPI(title="HermesOrch", version="0.1.0")

# Trust forwarded headers from ngrok/Cloudflare so request.url reflects the
# public URL Twilio hit (needed for signature validation).
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "ollama": settings.ollama_base_url,
        "model": settings.ollama_model,
    }


app.include_router(web_router)
app.include_router(twilio_router)
app.include_router(telegram_router)
