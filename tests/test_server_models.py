import unittest

from gemini_webapi.server.app import _openai_model_ids, _resolve_model_arg


class ServerModelTests(unittest.TestCase):
    def test_resolves_only_public_default_and_pro_name(self):
        self.assertEqual(_resolve_model_arg("gemini"), "gemini-3-pro")
        self.assertEqual(_resolve_model_arg("gemini-3.1-pro"), "gemini-3-pro")
        self.assertEqual(_resolve_model_arg("gemini-3-flash"), "gemini-3-flash")
        self.assertEqual(
            _resolve_model_arg("gemini-3-flash-thinking"),
            "gemini-3-flash-thinking",
        )

    def test_lists_only_public_models(self):
        self.assertEqual(
            _openai_model_ids(),
            [
                "gemini",
                "gemini-3.1-pro",
                "gemini-3-flash",
                "gemini-3-flash-thinking",
            ],
        )


if __name__ == "__main__":
    unittest.main()
