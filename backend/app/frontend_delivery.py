from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope


IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
PUBLIC_FILE_CACHE_CONTROL = "public, max-age=86400"
HTML_CACHE_CONTROL = "no-cache"


class ImmutableStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = IMMUTABLE_CACHE_CONTROL
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def frontend_file_response(path: Path, *, html: bool = False) -> FileResponse:
    return FileResponse(
        path,
        headers={
            "Cache-Control": HTML_CACHE_CONTROL if html else PUBLIC_FILE_CACHE_CONTROL,
            "X-Content-Type-Options": "nosniff",
        },
    )
