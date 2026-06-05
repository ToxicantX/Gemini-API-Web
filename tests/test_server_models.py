import unittest

from gemini_webapi.exceptions import VideoGenerationFailed, VideoGenerationNotSubmitted
from gemini_webapi.server.app import (
    _media_content_type_allowed,
    _error_status,
    _generation_mode_arg,
    _media_host_allowed,
    _media_record_dict,
    _openai_image_generation_output,
    _openai_model_object,
    _openai_model_ids,
    _public_media_content_path,
    _resolve_model_arg,
)
from gemini_webapi.constants import Model


class ServerModelTests(unittest.TestCase):
    def test_resolves_only_current_public_models(self):
        self.assertEqual(_resolve_model_arg("gemini"), "gemini-3.1-pro")
        self.assertEqual(_resolve_model_arg("gemini-3.1-pro"), "gemini-3.1-pro")
        self.assertEqual(_resolve_model_arg("gemini-3.5-flash"), "gemini-3.5-flash")
        self.assertEqual(
            _resolve_model_arg("gemini-3.1-flash-lite"),
            "gemini-3.1-flash-lite",
        )

    def test_removed_old_models_are_rejected(self):
        for model in (
            "gemini-3-pro",
            "gemini-3-pro-preview",
            "gemini-3.1-pro-preview",
            "gemini-3-flash",
            "gemini-3-flash-preview",
            "gemini-3-flash-thinking",
        ):
            with self.subTest(model=model):
                with self.assertRaises(ValueError):
                    _resolve_model_arg(model)

    def test_unknown_models_are_rejected(self):
        for model in (
            "gemini-3.1-pro-plus",
            "gemini-3.5-flash-advanced",
            "not-a-real-model",
        ):
            with self.subTest(model=model):
                with self.assertRaises(ValueError):
                    _resolve_model_arg(model)

    def test_lists_only_public_models(self):
        self.assertEqual(
            _openai_model_ids(),
            [
                "gemini",
                "gemini-3.1-flash-lite",
                "gemini-3.5-flash",
                "gemini-3.1-pro",
            ],
        )

    def test_openai_model_object_shape(self):
        self.assertEqual(
            _openai_model_object("gemini-3.1-pro", 123),
            {
                "id": "gemini-3.1-pro",
                "object": "model",
                "created": 123,
                "owned_by": "google",
            },
        )

    def test_openai_image_generation_output_uses_content_url(self):
        class MediaRecord:
            token = "tok-1"
            url = "https://lh3.googleusercontent.com/image.png"
            kind = "image"
            request_id = "img-1"

        data = _openai_image_generation_output(
            [MediaRecord()],
            revised_prompt="make image",
        )

        self.assertEqual(data["data"][0]["url"], "/v1/gemini/media/tok-1/content")
        self.assertEqual(data["data"][0]["revised_prompt"], "make image")

    def test_openai_image_generation_output_can_return_b64_json(self):
        class MediaRecord:
            token = "tok-1"
            url = "https://lh3.googleusercontent.com/image.png"
            kind = "image"
            request_id = "img-1"

        data = _openai_image_generation_output(
            [MediaRecord()],
            revised_prompt="make image",
            response_format="b64_json",
            media_content_loader=lambda item: b"image-bytes",
        )

        self.assertEqual(data["data"][0]["b64_json"], "aW1hZ2UtYnl0ZXM=")
        self.assertEqual(data["data"][0]["revised_prompt"], "make image")

    def test_only_media_content_path_is_public(self):
        self.assertTrue(_public_media_content_path("/v1/gemini/media/token-1/content"))
        self.assertFalse(_public_media_content_path("/v1/gemini/media"))
        self.assertFalse(_public_media_content_path("/v1/gemini/media/token-1"))
        self.assertFalse(_public_media_content_path("/v1/gemini/media/token-1/content/extra"))

    def test_static_model_enum_keeps_only_current_real_models(self):
        self.assertEqual(
            [model.model_name for model in Model],
            [
                "unspecified",
                "gemini-3.1-pro",
                "gemini-3.5-flash",
                "gemini-3.1-flash-lite",
            ],
        )

    def test_generation_mode_accepts_only_media_modes(self):
        self.assertEqual(_generation_mode_arg("image"), "image")
        self.assertEqual(_generation_mode_arg(" VIDEO "), "video")
        self.assertEqual(_generation_mode_arg("audio"), "audio")
        self.assertIsNone(_generation_mode_arg(None))
        self.assertIsNone(_generation_mode_arg(""))
        with self.assertRaises(ValueError):
            _generation_mode_arg("text")

    def test_video_generation_errors_map_to_http_statuses(self):
        self.assertEqual(_error_status(VideoGenerationNotSubmitted("missing job")), 409)
        self.assertEqual(_error_status(VideoGenerationFailed("failed")), 502)

    def test_media_record_dict_adds_proxy_content_url(self):
        class MediaRecord:
            token = "tok-1"
            url = "https://lh3.googleusercontent.com/image.png"
            kind = "image"

        data = _media_record_dict(MediaRecord())
        self.assertEqual(data["content_url"], "/v1/gemini/media/tok-1/content")
        self.assertFalse(data["cached"])

    def test_media_record_dict_marks_cached_media(self):
        class MediaRecord:
            def __init__(self):
                self.token = "tok-1"
                self.url = "https://lh3.googleusercontent.com/image.png"
                self.kind = "image"
                self.local_path = "/tmp/image.png"

        data = _media_record_dict(MediaRecord())
        self.assertTrue(data["cached"])

    def test_media_proxy_allows_only_google_hosts(self):
        self.assertTrue(
            _media_host_allowed("https://lh3.googleusercontent.com/image.png")
        )
        self.assertTrue(
            _media_host_allowed("https://storage.googleapis.com/bucket/image.png")
        )
        self.assertFalse(_media_host_allowed("https://example.com/image.png"))
        self.assertFalse(_media_host_allowed("file:///tmp/image.png"))

    def test_media_content_type_must_match_kind(self):
        self.assertTrue(_media_content_type_allowed("image", "image/png"))
        self.assertTrue(_media_content_type_allowed("video", "video/mp4"))
        self.assertTrue(_media_content_type_allowed("audio", "audio/mpeg"))
        self.assertFalse(_media_content_type_allowed("image", "text/html; charset=UTF-8"))
        self.assertFalse(_media_content_type_allowed("video", "image/png"))


if __name__ == "__main__":
    unittest.main()
