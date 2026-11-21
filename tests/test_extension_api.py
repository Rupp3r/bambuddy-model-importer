import asyncio
import unittest
from unittest.mock import patch

import app as importer


class ExtensionApiContractTests(unittest.TestCase):
    def test_versioned_extension_routes_are_registered(self):
        paths = {route.path for route in importer.app.routes}
        self.assertIn("/api/extension/v1/session", paths)
        self.assertIn("/api/extension/v1/info", paths)
        self.assertIn("/api/extension/v1/resolve", paths)
        self.assertIn("/api/extension/v1/import-batch", paths)
        self.assertIn("/api/extension/v1/import-direct", paths)

    def test_legacy_importer_routes_remain_registered(self):
        paths = {route.path for route in importer.app.routes}
        self.assertIn("/api/session", paths)
        self.assertIn("/api/resolve", paths)
        self.assertIn("/api/import-batch", paths)
        self.assertIn("/api/import-direct", paths)

    def test_extension_cors_is_limited_to_extension_origins(self):
        cors = next(
            middleware
            for middleware in importer.app.user_middleware
            if middleware.cls.__name__ == "CORSMiddleware"
        )
        self.assertEqual(
            cors.kwargs["allow_origin_regex"],
            r"^(chrome-extension|moz-extension)://[a-z0-9-]+$",
        )
        self.assertFalse(cors.kwargs["allow_credentials"])
        self.assertIn("X-Importer-Password", cors.kwargs["allow_headers"])

    def test_info_endpoint_uses_the_shared_contract(self):
        with patch.object(importer, "check_password") as check_password:
            response = asyncio.run(importer.extension_api_info("signed-session-token"))
        check_password.assert_called_once_with("signed-session-token")
        self.assertEqual(response["api_major"], importer.EXTENSION_API_CONTRACT["api_major"])
        self.assertEqual(response["contract_revision"], importer.EXTENSION_API_CONTRACT["contract_revision"])
        self.assertEqual(response["features"], importer.EXTENSION_API_CONTRACT["features"])
        self.assertEqual(response["sources"], importer.EXTENSION_API_CONTRACT["sources"])

    def test_shared_contract_contains_the_required_extension_capabilities(self):
        available = set(importer.EXTENSION_API_CONTRACT["features"])
        self.assertTrue({"multi-file-folders", "source-tags", "direct-file-import"}.issubset(available))

    def test_shared_contract_advertises_shapekit_and_bambuddy_makerworld(self):
        sources = {source["id"]: source for source in importer.EXTENSION_API_CONTRACT["sources"]}
        self.assertIn("shapekit.cc", sources["shapekit"]["hosts"])
        self.assertIn("makerworld.com", sources["makerworld"]["hosts"])
        self.assertIn("Bambu Cloud login", sources["makerworld"]["requirement"])


if __name__ == "__main__":
    unittest.main()
