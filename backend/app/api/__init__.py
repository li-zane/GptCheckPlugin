from fastapi import APIRouter

from app.api import account_editor, accounts, auth, dashboard, mailboxes, phones, settings, upstream_accounts, upstream_channels

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(dashboard.router, tags=["dashboard"])
api_router.include_router(accounts.router, prefix="/accounts", tags=["accounts"])
api_router.include_router(account_editor.router, prefix="/accounts", tags=["account-editor"])
api_router.include_router(mailboxes.router, prefix="/mailboxes", tags=["mailboxes"])
api_router.include_router(phones.router, prefix="/phones", tags=["phones"])
api_router.include_router(settings.router, prefix="/settings", tags=["settings"])
api_router.include_router(upstream_accounts.router, prefix="/upstream-accounts", tags=["upstream-accounts"])
api_router.include_router(upstream_channels.router, prefix="/upstream-channels", tags=["upstream-channels"])
