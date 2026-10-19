"""Check the deployed CORS origin configuration without starting API services."""

import ast
from pathlib import Path
import re
import unittest


class CloudflareCORSTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / "api" / "api.py"
        tree = ast.parse(source.read_text())
        middleware = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_middleware"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "CORSMiddleware"
        )
        cls.pattern = re.compile(next(
            ast.literal_eval(keyword.value) for keyword in middleware.keywords
            if keyword.arg == "allow_origin_regex"
        ))

    def test_owned_preview_origins(self):
        for prefix in ["", "pr-92-", "a1b2c3d4-", "manual-"]:
            with self.subTest(prefix=prefix):
                self.assertIsNotNone(self.pattern.fullmatch(
                    f"https://{prefix}lanis-ui-preview.bennetjoanwegener.workers.dev"
                ))

    def test_unrelated_cloudflare_origins_are_blocked(self):
        for origin in [
            "https://attacker.workers.dev",
            "https://pr-92-lanis-ui-preview.attacker.workers.dev",
            "https://pr-92-other-app.bennetjoanwegener.workers.dev",
            "https://pr-92-lanis-ui-preview.bennetjoanwegener.workers.dev.evil.com",
            "https://evil.pr-92-lanis-ui-preview.bennetjoanwegener.workers.dev",
            "http://pr-92-lanis-ui-preview.bennetjoanwegener.workers.dev",
            "https://pr-92-lanis-ui-preview.bennetjoanwegener.workers.dev:8443",
        ]:
            with self.subTest(origin=origin):
                self.assertIsNone(self.pattern.fullmatch(origin))


if __name__ == "__main__":
    unittest.main()
