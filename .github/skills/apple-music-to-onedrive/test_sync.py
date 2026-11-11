import csv
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("music_sync", Path(__file__).with_name("sync.py"))
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "Apple Music"
        self.source.mkdir()
        self.csv = self.root / "Unified Music List.csv"
        self.a = self.source / "Artist" / "a.m4a"
        self.b = self.source / "Artist" / "b.m4a"
        self.a.parent.mkdir()
        self.a.write_bytes(b"first")
        self.b.write_bytes(b"second")
        with self.csv.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["Title", "Apple Files", "Extra"])
            writer.writeheader()
            writer.writerow({"Title": "A song", "Apple Files": r"Apple Music\Artist\a.m4a; Apple Music\Artist\b.m4a",
                             "Extra": "preserve"})

    def args(self, mode):
        return [mode, "--source", str(self.source), "--csv", str(self.csv)]

    def rows(self):
        with self.csv.open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    def test_preview_is_read_only(self):
        original = self.csv.read_bytes()
        self.assertEqual(sync.main(self.args("--preview")), 0)
        self.assertEqual(self.csv.read_bytes(), original)

    def test_success_and_retry_skip_individual_files(self):
        class FakeGraph:
            calls = []

            def __init__(self, token):
                pass

            def call(self, *args):
                return 200, {}

            def upload(self, path, parts):
                self.calls.append(path.name)
                if path.name == "b.m4a" and self.calls.count("b.m4a") == 1:
                    raise sync.SyncError("test failure")
                return "id-" + path.name

        with mock.patch.object(sync, "Graph", FakeGraph), mock.patch.dict(os.environ, {"ONEDRIVE_ACCESS_TOKEN": "test"}):
            self.assertEqual(sync.main(self.args("--upload")), 1)
            row = self.rows()[0]
            self.assertEqual(row["Extra"], "preserve")
            self.assertEqual(row["Title"], "A song")
            state = json.loads(row[sync.TRACKING])
            self.assertEqual(len(state), 1)
            self.assertEqual(next(iter(state.values()))["status"], "uploaded")
            self.assertEqual(sync.main(self.args("--upload")), 0)
            self.assertEqual(FakeGraph.calls, ["a.m4a", "b.m4a", "b.m4a"])
            state = json.loads(self.rows()[0][sync.TRACKING])
            self.assertEqual(len(state), 2)

    def test_adds_new_path_without_destroying_other_rows(self):
        self.csv.write_text("Title,Apple Files,Extra\nUnrelated,,value\n", encoding="utf-8")
        library = sync.Library(self.csv)
        library.record(self.a, "Music/Artist/a.m4a", "item-id")
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Extra"], "value")
        self.assertTrue(sync.Library(self.csv).is_uploaded(self.a))
        self.assertFalse(sync.Library(self.csv).is_uploaded(self.b))

    def test_concurrent_change_not_overwritten(self):
        library = sync.Library(self.csv)
        self.csv.write_text("external change", encoding="utf-8")
        with self.assertRaisesRegex(sync.SyncError, "changed externally"):
            library.record(self.a, "Music/a.m4a", "id")
        self.assertEqual(self.csv.read_text(), "external change")

    def test_missing_token_does_not_change_csv(self):
        before = self.csv.read_bytes()
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(sync.SyncError, "ONEDRIVE_ACCESS_TOKEN"):
                sync.main(self.args("--upload"))
        self.assertEqual(self.csv.read_bytes(), before)

    def test_chunk_upload_and_conflict_policy(self):
        self.a.write_bytes(b"x" * (sync.CHUNK_SIZE + 12))
        graph = sync.Graph("token")
        calls = []

        def fake_call(method, url, data=None, headers=None, auth=True):
            calls.append((method, url, data, headers, auth))
            if url.endswith("/createUploadSession"):
                return 200, {"uploadUrl": "https://upload.example/session"}
            if len([c for c in calls if c[0] == "PUT"]) == 1:
                return 202, {}
            return 201, {"id": "remote", "name": "a.m4a", "size": self.a.stat().st_size}

        with mock.patch.object(graph, "folder", return_value="folder"), mock.patch.object(graph, "call", side_effect=fake_call):
            self.assertEqual(graph.upload(self.a, ("Music", "a.m4a")), "remote")
        self.assertEqual(json.loads(calls[0][2])["item"]["@microsoft.graph.conflictBehavior"], "fail")
        chunks = [c for c in calls if c[0] == "PUT"]
        self.assertEqual(len(chunks), 2)
        self.assertFalse(chunks[0][4])
        self.assertEqual(chunks[1][3]["Content-Range"], f"bytes {sync.CHUNK_SIZE}-{sync.CHUNK_SIZE + 11}/{sync.CHUNK_SIZE + 12}")

    def test_incomplete_upload_not_marked_success(self):
        graph = sync.Graph("token")
        responses = [(200, {"uploadUrl": "https://upload.example/session"}), (202, {})]
        with mock.patch.object(graph, "folder", return_value="folder"), mock.patch.object(graph, "call", side_effect=responses):
            with self.assertRaisesRegex(sync.SyncError, "did not complete"):
                graph.upload(self.a, ("Music", "a.m4a"))

    def test_expired_token_stops_without_tracking(self):
        class FakeGraph:
            def __init__(self, token):
                pass

            def call(self, *args):
                return 200, {}

            def upload(self, path, parts):
                raise sync.SyncError("OneDrive HTTP 401: Unauthorized")

        before = self.csv.read_bytes()
        with mock.patch.object(sync, "Graph", FakeGraph), mock.patch.dict(os.environ, {"ONEDRIVE_ACCESS_TOKEN": "test"}):
            with self.assertRaisesRegex(sync.SyncError, "expired"):
                sync.main(self.args("--upload"))
        self.assertEqual(self.csv.read_bytes(), before)

    def test_confirmed_upload_with_failed_save_is_reported(self):
        class FakeGraph:
            def __init__(self, token):
                pass

            def call(self, *args):
                return 200, {}

            def upload(self, path, parts):
                return "remote-id"

        with mock.patch.object(sync, "Graph", FakeGraph), mock.patch.object(sync.Library, "save", side_effect=OSError("locked")), \
             mock.patch.dict(os.environ, {"ONEDRIVE_ACCESS_TOKEN": "test"}):
            with self.assertRaisesRegex(sync.SyncError, "Upload completed.*tracking update failed"):
                sync.main(self.args("--upload"))


if __name__ == "__main__":
    unittest.main()
