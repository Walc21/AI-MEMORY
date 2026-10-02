"""Official API contract: pagination, boundaries, retry and upload reconciliation."""

from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

from mimir.io import IOError
from mimir.io.drive import DriveAPI, DriveError, _GoogleRedirect


class APIContracts(unittest.TestCase):
    def test_pagination_yields_every_page_and_enforces_file_limit(self):
        api = DriveAPI(None)
        calls = []
        def page(path, **options):
            calls.append(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query))
            return {"files": [{"id": "first"}], "nextPageToken": "next"} if len(calls) == 1 else {"files": [{"id": "second"}]}
        with patch.object(api, "json", side_effect=page):
            self.assertEqual([row["id"] for row in api.files("incoming")], ["first", "second"])
        self.assertEqual(calls[1]["pageToken"], ["next"])
        calls.clear()
        with patch.object(api, "json", side_effect=page), self.assertRaises(IOError):
            list(api.files("incoming", max_files=1))

    def test_folder_collision_refuses_ambiguous_bootstrap(self):
        api = DriveAPI(None)
        with patch.object(api, "json", return_value={"files": [{"id": "a"}, {"id": "b"}]}), self.assertRaisesRegex(IOError, "homônimas"):
            api.folder("Entrada", "home")

    def test_native_download_uses_provider_export_not_binary_media(self):
        api = DriveAPI(None)
        with patch.object(api, "request", return_value=b"document") as request:
            api.download({"id": "native", "mimeType": "application/vnd.google-apps.document"}, 100)
        path = request.call_args.args[0]
        self.assertIn("/export?", path)
        self.assertIn("wordprocessingml", path)

    def test_upload_resolves_lost_response_using_reserved_identity(self):
        api = DriveAPI(None)
        job = {"id": "job1", "remote_reserved_id": "remote1"}
        remote = {"id": "remote1", "appProperties": {"mimir_output_id": "job1"}}
        with patch.object(api, "metadata", return_value=remote), patch.object(api, "json") as create:
            self.assertEqual(api.upload(job, {}, Path("must-not-be-read")), remote)
            create.assert_not_called()
        with patch.object(api, "metadata", return_value={"appProperties": {"mimir_output_id": "someone-else"}}), self.assertRaises(IOError):
            api.upload(job, {}, Path("must-not-be-read"))

    def test_first_upload_contains_reserved_id_and_verified_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "payload.json"
            data = b'{"answer":"hello"}\n'
            path.write_bytes(data)
            job = {"id": "job1", "remote_reserved_id": "remote1", "file_name": "output.json", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            api = DriveAPI(None)
            with patch.object(api, "metadata", side_effect=[DriveError(404, "notFound"), {"id": "remote1"}]), patch.object(api, "json", return_value={"id": "remote1"}) as create:
                self.assertEqual(api.upload(job, {"output_folder_id": "outgoing"}, path), {"id": "remote1"})
            body = create.call_args.kwargs["body"]
            self.assertIn(b'"id": "remote1"', body)
            self.assertIn(b'"parents": ["outgoing"]', body)
            self.assertIn(data, body)

    def test_redirect_never_leaks_bearer_or_escapes_google_hosts(self):
        request = urllib.request.Request("https://www.googleapis.com/drive/v3/files/f?alt=media", headers={"Authorization": "Bearer private-placeholder"})
        handler = _GoogleRedirect()
        for target in ("https://evil.example/data", "http://www.googleapis.com/data"):
            with self.assertRaises(IOError):
                handler.redirect_request(request, None, 302, "Found", {}, target)
        following = handler.redirect_request(request, None, 302, "Found", {}, "https://download.googleusercontent.com/data")
        self.assertIsNone(following.get_header("Authorization"))

    @unittest.skipUnless(importlib.util.find_spec("google") is not None, "Install [drive]")
    def test_http_retry_size_bounds_and_no_blind_write_retry(self):
        class Credentials:
            def before_request(self, transport, method, url, headers):
                headers["Authorization"] = "Bearer private-placeholder"
        api = DriveAPI(Credentials())
        failure = lambda: urllib.error.HTTPError("https://www.googleapis.com", 429, "busy", {}, io.BytesIO(b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}'))
        with patch("urllib.request.OpenerDirector.open", side_effect=[failure(), io.BytesIO(b"ok")]) as opening, patch("time.sleep"):
            self.assertEqual(api.request("/drive/v3/about"), b"ok")
            self.assertEqual(opening.call_count, 2)
        with patch("urllib.request.OpenerDirector.open", side_effect=failure()) as opening:
            with self.assertRaises(DriveError):
                api.request("/drive/v3/files", method="POST", body={})
            self.assertEqual(opening.call_count, 1)
        with patch("urllib.request.OpenerDirector.open", return_value=io.BytesIO(b"four")), self.assertRaises(IOError):
            api.request("/drive/v3/about", maximum=3)


if __name__ == "__main__":
    unittest.main()
