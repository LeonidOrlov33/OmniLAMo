"""Focused regression checks for URL safety and bounded HTTP responses."""
import base64
import os
import tempfile
import unittest
from unittest.mock import patch

import server
import tools


class FakeResponse:
    def __init__(self, url, *, status=200, headers=None, chunks=()):
        self.url = url
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks
        self.closed = False
        self.encoding = "utf-8"

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308)

    @property
    def is_permanent_redirect(self):
        return self.status_code in (301, 308)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP error")

    def iter_content(self, chunk_size):
        yield from self._chunks

    def close(self):
        self.closed = True


class SafeGetTests(unittest.TestCase):
    def test_ipv4_mapped_ipv6_loopback_is_rejected(self):
        ok, _ = tools._safe_public_url("http://[::ffff:127.0.0.1]/")
        self.assertFalse(ok)

    @patch.object(tools, "_safe_public_url", return_value=(True, ""))
    @patch.object(tools.requests, "get")
    def test_redirect_is_checked_manually_and_closed(self, get, _safe_url):
        redirect = FakeResponse(
            "https://public.example/start", status=302,
            headers={"Location": "https://public.example/final"},
        )
        final = FakeResponse("https://public.example/final")
        get.side_effect = [redirect, final]

        result = tools._safe_get("https://public.example/start", stream=True)

        self.assertIs(result, final)
        self.assertTrue(redirect.closed)
        self.assertEqual(get.call_count, 2)
        self.assertTrue(all(call.kwargs["allow_redirects"] is False
                            for call in get.call_args_list))
        final.close()

    @patch.object(tools, "_safe_public_url", return_value=(True, ""))
    @patch.object(tools.requests, "get")
    def test_oversized_stream_is_rejected_and_response_closed(self, get, _safe_url):
        response = FakeResponse("https://public.example/file", chunks=[b"abc"])
        get.return_value = response

        with self.assertRaises(ValueError):
            tools._safe_get("https://public.example/file", max_bytes=2)

        self.assertTrue(response.closed)

    @patch.object(tools, "_safe_public_url", return_value=(True, ""))
    @patch.object(tools.requests, "get")
    def test_declared_oversized_stream_is_rejected_and_closed(self, get, _safe_url):
        response = FakeResponse(
            "https://public.example/file", headers={"Content-Length": "3"}
        )
        get.return_value = response

        with self.assertRaises(ValueError):
            tools._safe_get("https://public.example/file", stream=True, max_bytes=2)

        self.assertTrue(response.closed)


class UploadIsolationTests(unittest.TestCase):
    def test_invalid_base64_is_rejected(self):
        with patch.object(server, "get_user", return_value={"ok": True}):
            result = server.upload_file(server.UploadRequest(
                api_key="u" * 32, filename="report.txt", content_b64="aGVs!bG8="
            ))
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Файл повреждён при передаче")

    def test_oversized_decoded_upload_is_rejected(self):
        with (patch.object(server, "get_user", return_value={"ok": True}),
              patch.object(server, "MAX_UPLOAD_BYTES", 3)):
            result = server.upload_file(server.UploadRequest(
                api_key="u" * 32, filename="report.txt",
                content_b64=base64.b64encode(b"four").decode("ascii"),
            ))
        self.assertFalse(result["success"])
        self.assertIn("больше", result["error"])

    def test_uploads_are_isolated_by_api_key_and_paths_are_not_exposed(self):
        with tempfile.TemporaryDirectory() as root:
            with (patch.object(server, "get_user", return_value={"ok": True}),
                  patch.object(server, "user_upload_dir",
                               side_effect=lambda key: os.path.join(root, key))):
                first = server.upload_file(server.UploadRequest(
                    api_key="u" * 32, filename="../report.txt",
                    content_b64=base64.b64encode(b"first").decode("ascii"),
                ))
                second = server.upload_file(server.UploadRequest(
                    api_key="v" * 32, filename="../report.txt",
                    content_b64=base64.b64encode(b"second").decode("ascii"),
                ))

            self.assertTrue(first["success"])
            self.assertTrue(second["success"])
            self.assertEqual(first["path"], "report.txt")
            self.assertNotIn(root, first["path"])
            self.assertTrue(os.path.isfile(os.path.join(root, "u" * 32, "report.txt")))
            self.assertTrue(os.path.isfile(os.path.join(root, "v" * 32, "report.txt")))


if __name__ == "__main__":
    unittest.main()
