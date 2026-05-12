import unittest

from gemini_webapi.server.app import _openai_model_ids, _resolve_model_arg


class ServerModelAliasTests(unittest.TestCase):
    def test_resolves_common_openai_gemini_model_names(self):
        aliases = {
            "gemini": "gemini-3-pro",
            "gemini-3.1-pro": "gemini-3-pro",
            "gemini-3.1-pro-preview": "gemini-3-pro",
            "gemini-3-pro-preview": "gemini-3-pro",
            "gemini-2.5-pro": "gemini-3-pro",
            "gemini-3-flash-preview": "gemini-3-flash",
            "gemini-2.5-flash": "gemini-3-flash",
            "gemini-2.5-flash-thinking": "gemini-3-flash-thinking",
        }
        for requested, resolved in aliases.items():
            with self.subTest(requested=requested):
                self.assertEqual(_resolve_model_arg(requested), resolved)

    def test_lists_compatibility_aliases(self):
        ids = _openai_model_ids()
        for model in [
            "gemini-3.1-pro-preview",
            "gemini-3-pro-preview",
            "gemini-3-flash-preview",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-thinking",
        ]:
            with self.subTest(model=model):
                self.assertIn(model, ids)


if __name__ == "__main__":
    unittest.main()
