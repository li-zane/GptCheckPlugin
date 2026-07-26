from __future__ import annotations

import os
from pathlib import Path


def main() -> None:
    # Set this before uvicorn imports app.main and constructs Settings.
    os.environ["APP_ENV"] = "production"

    import uvicorn

    uvicorn.run(
        "app.main:app",
        app_dir=str(Path(__file__).resolve().parent),
        host="127.0.0.1",
        port=5173,
    )


if __name__ == "__main__":
    main()
