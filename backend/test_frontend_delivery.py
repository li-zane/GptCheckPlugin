from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.gzip import GZipMiddleware

from app.frontend_delivery import ImmutableStaticFiles, frontend_file_response


class FrontendDeliveryTests(unittest.TestCase):
    def test_hashed_assets_are_compressed_and_cached_immutably(self) -> None:
        with TemporaryDirectory() as directory:
            asset = Path(directory) / "app-content-hash.js"
            asset.write_text("const value = 'frontend';\n" * 200, encoding="utf-8")
            app = FastAPI()
            app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)
            app.mount("/assets", ImmutableStaticFiles(directory=directory), name="assets")

            with TestClient(app) as client:
                response = client.get("/assets/app-content-hash.js", headers={"Accept-Encoding": "gzip"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-encoding"], "gzip")
        self.assertIn("immutable", response.headers["cache-control"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_html_revalidates_while_public_files_use_a_short_cache(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            index = root / "index.html"
            logo = root / "logo.png"
            index.write_text("<!doctype html><title>App</title>", encoding="utf-8")
            logo.write_bytes(b"png")
            app = FastAPI()

            @app.get("/")
            async def get_index():
                return frontend_file_response(index, html=True)

            @app.get("/logo.png")
            async def get_logo():
                return frontend_file_response(logo)

            with TestClient(app) as client:
                index_response = client.get("/")
                logo_response = client.get("/logo.png")

        self.assertEqual(index_response.headers["cache-control"], "no-cache")
        self.assertEqual(logo_response.headers["cache-control"], "public, max-age=86400")


if __name__ == "__main__":
    unittest.main()
