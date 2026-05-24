from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.core.config import get_settings
from app.core.database import init_db
from app.services.monitor import get_monitor_service
from app.services.refresh import get_refresh_service
from app.services.runtime_config import get_runtime_config_service
from app.services.usage_refresh import get_usage_refresh_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await get_refresh_service().cleanup_stale_jobs()
    await get_runtime_config_service().auto_detect_sub2api()
    monitor = get_monitor_service()
    usage_refresh = get_usage_refresh_service()
    monitor.start()
    usage_refresh.start()
    yield
    await monitor.stop()
    await usage_refresh.stop()


settings = get_settings()

app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
)

app.include_router(api_router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
