from fastapi import APIRouter

from app.api import accounts, auth, dashboard, mailboxes, phones, settings

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(dashboard.router, tags=["dashboard"])
api_router.include_router(accounts.router, prefix="/accounts", tags=["accounts"])
api_router.include_router(mailboxes.router, prefix="/mailboxes", tags=["mailboxes"])
api_router.include_router(phones.router, prefix="/phones", tags=["phones"])
api_router.include_router(settings.router, prefix="/settings", tags=["settings"])
