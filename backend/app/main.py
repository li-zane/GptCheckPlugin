from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.middleware.gzip import GZipMiddleware

from app.api import api_router
from app.core.config import get_settings
from app.core.database import init_db
from app.core.validation import sanitized_request_validation_handler
from app.frontend_delivery import ImmutableStaticFiles, frontend_file_response
from app.services.monitor import get_monitor_service
from app.services.notification_dispatcher import get_notification_dispatcher
from app.services.discord_commands import get_discord_command_service
from app.services.refresh import get_refresh_service
from app.services.runtime_config import get_runtime_config_service
from app.services.upstream_rate_sync import get_upstream_rate_sync_service
from app.services.upstream_channels import get_upstream_service
from app.services.usage_refresh import get_usage_refresh_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await get_refresh_service().cleanup_stale_jobs()
    await get_runtime_config_service().auto_detect_sub2api()
    monitor = get_monitor_service()
    upstream_rate_sync = get_upstream_rate_sync_service()
    usage_refresh = get_usage_refresh_service()
    notification_dispatcher = get_notification_dispatcher()
    discord_commands = get_discord_command_service()
    monitor.start()
    upstream_rate_sync.start()
    usage_refresh.start()
    notification_dispatcher.start()
    discord_commands.start()
    yield
    await monitor.stop()
    await upstream_rate_sync.stop()
    await usage_refresh.stop()
    await notification_dispatcher.stop()
    await discord_commands.stop()
    await get_upstream_service().stop_background_tasks()


settings = get_settings()

app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_exception_handler(RequestValidationError, sanitized_request_validation_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)

app.include_router(api_router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
frontend_index = frontend_dist / "index.html"
frontend_assets = frontend_dist / "assets"

if frontend_index.is_file():
    if frontend_assets.is_dir():
        app.mount(
            "/assets",
            ImmutableStaticFiles(directory=frontend_assets),
            name="frontend-assets",
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def frontend_app(full_path: str) -> FileResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found.")
        candidate = (frontend_dist / full_path).resolve()
        if frontend_dist in candidate.parents and candidate.is_file():
            return frontend_file_response(candidate, html=candidate.suffix.lower() == ".html")
        return frontend_file_response(frontend_index, html=True)
