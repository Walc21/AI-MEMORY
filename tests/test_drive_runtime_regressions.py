"""Provider failures must not stop recovery or strand the output queue."""

from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

from mimir import Memory
from mimir.io import Channels, IOError
from mimir.io.drive import DriveAPI, DriveError, DriveSync


def folder(identity, parent="root"):
    return {"id": identity, "name": identity,
            "mimeType": "application/vnd.google-apps.folder", "parents": [parent],
            "trashed": False, "ownedByMe": True, "shared": False}


class DeliveryDrive:
    """List failure after a page does not affect writes to the separate folder."""

    def __init__(self):
        self.rows = {row["id"]: row for row in
                     (folder("home"), folder("incoming", "home"), folder("outgoing", "home"))}

    def profile(self):
        return {"emailAddress": "test@example.com"}

    def metadata(self, identity):
        return deepcopy(self.rows[identity])

    def files(self, identity, maximum):
        yield folder("nested", "incoming")
        raise DriveError(503, "backendError")

    def reserve(self):
        return "reserved-output"

    def upload(self, job, config, path):
        return {"id": job["remote_reserved_id"], "name": job["file_name"],
                "parents": [config["output_folder_id"]], "size": str(job["size"]),
                "sha256Checksum": job["sha256"], "md5Checksum": job["md5"]}


class SyncRuntimeRegressions(unittest.TestCase):
    def test_later_listing_failure_still_delivers_pending_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            channels = Channels(Memory(Path(temporary) / "memory"))
            channels.configure(folder("home"), folder("incoming", "home"),
                               folder("outgoing", "home"), "test@example.com")
            job = channels.enqueue({"answer": "A completed result"})
            result = DriveSync(channels, DeliveryDrive()).sync()
            self.assertFalse(result["complete"])
            self.assertEqual(result["delivered"][0]["id"], job["id"])
            self.assertEqual(result["failures"][0]["input_folder_id"], "incoming")
            self.assertIn("503", result["failures"][0]["error"])
            self.assertEqual(channels.pending(), [])

    def test_bootstrap_search_follows_empty_partial_pages(self):
        api = DriveAPI(None)
        calls = []
        existing = folder("existing", "home")

        def response(path, **options):
            self.assertNotIn("method", options, "Existing folders must not be duplicated")
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            calls.append(query)
            if "pageToken" not in query:
                return {"files": [], "nextPageToken": "next-page"}
            return {"files": [existing]}

        with patch.object(api, "json", side_effect=response):
            self.assertEqual(api.folder("Entrada", "home"), existing)
        self.assertEqual(calls[1]["pageToken"], ["next-page"])

    def test_bootstrap_detects_same_name_across_partial_pages(self):
        api = DriveAPI(None)
        with patch.object(api, "json", side_effect=[
            {"files": [folder("one", "home")], "nextPageToken": "next-page"},
            {"files": [folder("two", "home")]}]):
            with self.assertRaisesRegex(IOError, "homônimas"):
                api.folder("Entrada", "home")


@unittest.skipUnless(importlib.util.find_spec("google") is not None, "Install [drive]")
class OAuthRuntimeRegressions(unittest.TestCase):
    class Credentials:
        def before_request(self, transport, method, url, headers):
            headers["Authorization"] = "Bearer test-placeholder"

    def test_revoked_refresh_token_is_an_actionable_private_error(self):
        from google.auth.exceptions import RefreshError
        credentials = self.Credentials()
        with patch.object(credentials, "before_request", side_effect=RefreshError("private credential detail")):
            with self.assertRaises(IOError) as failure:
                DriveAPI(credentials).request("/drive/v3/about")
        self.assertIn("autenti", str(failure.exception).lower())
        self.assertNotIn("private credential detail", str(failure.exception))

    def test_refresh_transport_failure_is_recoverable_by_watch(self):
        from google.auth.exceptions import TransportError
        credentials = self.Credentials()
        with patch.object(credentials, "before_request", side_effect=TransportError("private transport detail")):
            with self.assertRaises(IOError) as failure:
                DriveAPI(credentials).request("/drive/v3/about")
        self.assertNotIn("private transport detail", str(failure.exception))

    def test_empty_provider_error_list_preserves_status_and_get_retry(self):
        error = urllib.error.HTTPError("https://www.googleapis.com", 503, "Unavailable", {},
                                       io.BytesIO(json.dumps({"error": {"errors": [], "status": "UNAVAILABLE"}}).encode()))
        with patch("urllib.request.OpenerDirector.open", side_effect=[error, io.BytesIO(b"ok")]), patch("time.sleep"):
            self.assertEqual(DriveAPI(self.Credentials()).request("/drive/v3/about"), b"ok")

    def test_transient_get_network_failure_retries_but_post_does_not(self):
        network_error = urllib.error.URLError("connection reset")
        with patch("urllib.request.OpenerDirector.open", side_effect=[network_error, io.BytesIO(b"ok")]) as opening, patch("time.sleep"):
            self.assertEqual(DriveAPI(self.Credentials()).request("/drive/v3/about"), b"ok")
            self.assertEqual(opening.call_count, 2)
        with patch("urllib.request.OpenerDirector.open", side_effect=network_error) as opening, patch("time.sleep"):
            with self.assertRaises(IOError):
                DriveAPI(self.Credentials()).request("/drive/v3/files", method="POST", body={})
            self.assertEqual(opening.call_count, 1)


if __name__ == "__main__":
    unittest.main()
