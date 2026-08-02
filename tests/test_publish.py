"""Publishing the export to object storage rather than to git.

The service key must never reach the repository, and a machine without one
must still produce its local export rather than failing the whole run.
"""

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from punktlig import publish


class FakeResponse:
    def __init__(self):
        self.closed = False

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


class LoadEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "secrets.env"

    def test_a_missing_file_is_not_an_error(self):
        self.assertEqual(publish.load_env(self.path), {})

    def test_comments_and_blank_lines_are_ignored(self):
        self.path.write_text("# a comment\n\nA=1\n", encoding="utf-8")
        self.assertEqual(publish.load_env(self.path), {"A": "1"})

    def test_a_value_may_contain_equals_signs(self):
        self.path.write_text("KEY=ab=cd==\n", encoding="utf-8")
        self.assertEqual(publish.load_env(self.path)["KEY"], "ab=cd==")


class SettingsTest(unittest.TestCase):
    def test_missing_credentials_are_reported_not_guessed(self):
        with self.assertRaises(publish.NotConfigured):
            publish.settings({})

    def test_a_key_without_a_url_is_still_not_configured(self):
        with self.assertRaises(publish.NotConfigured):
            publish.settings({publish.ENV_KEY: "k"})

    def test_the_bucket_has_a_default(self):
        url, key, bucket = publish.settings(
            {publish.ENV_URL: "https://x.supabase.co", publish.ENV_KEY: "k"}
        )
        self.assertEqual(bucket, publish.DEFAULT_BUCKET)

    def test_a_trailing_slash_does_not_double_up(self):
        url, _, _ = publish.settings(
            {publish.ENV_URL: "https://x.supabase.co/", publish.ENV_KEY: "k"}
        )
        self.assertEqual(url, "https://x.supabase.co")


class UploadTest(unittest.TestCase):
    ENV = {publish.ENV_URL: "https://x.supabase.co", publish.ENV_KEY: "secret-key"}

    def setUp(self):
        self.seen = {}

        @contextmanager
        def opener(request, timeout=None):
            self.seen["url"] = request.full_url
            self.seen["method"] = request.get_method()
            self.seen["headers"] = {k.lower(): v for k, v in request.header_items()}
            self.seen["body"] = request.data
            yield FakeResponse()

        self.opener = opener

    def test_it_posts_the_payload_as_json(self):
        publish.upload({"score": 1, "navn": "Bekkestua"},
                       env=self.ENV, opener=self.opener)
        self.assertEqual(json.loads(self.seen["body"]),
                         {"score": 1, "navn": "Bekkestua"})

    def test_norwegian_characters_are_not_escaped(self):
        publish.upload({"stop": "Grønland"}, env=self.ENV, opener=self.opener)
        self.assertIn("Grønland".encode("utf-8"), self.seen["body"])

    def test_it_upserts_so_readers_never_see_a_gap(self):
        publish.upload({}, env=self.ENV, opener=self.opener)
        self.assertEqual(self.seen["headers"].get("x-upsert"), "true")

    def test_it_authenticates_with_the_service_key(self):
        publish.upload({}, env=self.ENV, opener=self.opener)
        self.assertEqual(self.seen["headers"].get("authorization"),
                         "Bearer secret-key")

    def test_it_targets_the_bucket_object(self):
        publish.upload({}, env=self.ENV, opener=self.opener)
        self.assertEqual(
            self.seen["url"],
            "https://x.supabase.co/storage/v1/object/punktlig/data.json",
        )

    def test_it_returns_the_public_url_readers_should_use(self):
        got = publish.upload({}, env=self.ENV, opener=self.opener)
        self.assertEqual(
            got, "https://x.supabase.co/storage/v1/object/public/punktlig/data.json"
        )


if __name__ == "__main__":
    unittest.main()
