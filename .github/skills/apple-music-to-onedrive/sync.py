#!/usr/bin/env python3
"""Scan local Apple Music files and track confirmed OneDrive uploads."""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
from datetime import datetime, timezone
from urllib import error, parse, request

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "Apple Music"
CSV_PATH = ROOT / "Unified Music List.csv"
TRACKING = "OneDrive Uploads"
EXTENSIONS = {".mp3", ".m4a", ".m4p", ".flac", ".wav", ".aac", ".ogg", ".wma", ".alac", ".aiff", ".aif"}
GRAPH = "https://graph.microsoft.com/v1.0"
CHUNK_SIZE = 320 * 1024 * 32


class SyncError(Exception):
    pass


def digest(path):
    return hashlib.sha256(path.read_bytes()).digest()


def key_for(path, csv_dir):
    return os.path.normcase(os.path.abspath(os.path.join(csv_dir, path.replace("\\", os.sep))))


class Library:
    def __init__(self, path):
        self.path = path
        if not path.is_file():
            raise SyncError(f"Library CSV not found: {path}")
        self.hash = digest(path)
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            self.fields = reader.fieldnames
            if not self.fields or not {"Title", "Apple Files"}.issubset(self.fields):
                raise SyncError("Library CSV must contain 'Title' and 'Apple Files' columns.")
            if len(set(self.fields)) != len(self.fields):
                raise SyncError("Library CSV has duplicate column names.")
            self.rows = list(reader)
        if any(None in row or any(value is None for value in row.values()) for row in self.rows):
            raise SyncError("Library CSV contains malformed rows.")
        if TRACKING not in self.fields:
            self.fields.append(TRACKING)
        self.index = {}
        self.states = {}
        for row in self.rows:
            raw = row.get(TRACKING) or "{}"
            try:
                state = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SyncError(f"Invalid {TRACKING} JSON: {exc}") from exc
            if not isinstance(state, dict) or any(
                not isinstance(k, str) or not isinstance(v, dict) for k, v in state.items()
            ):
                raise SyncError(f"Invalid {TRACKING} data.")
            self.states[id(row)] = state
            for item in (row.get("Apple Files") or "").split("; "):
                if item:
                    key = key_for(item, path.parent)
                    if key in self.index:
                        raise SyncError(f"Apple file appears in multiple rows: {item}")
                    self.index[key] = (row, item)

    def lookup(self, path):
        return self.index.get(key_for(str(path), self.path.parent))

    def is_uploaded(self, path):
        match = self.lookup(path)
        if not match:
            return False
        row, item = match
        return self.states[id(row)].get(item, {}).get("status") == "uploaded"

    def record(self, path, destination, item_id):
        match = self.lookup(path)
        if match:
            row, item = match
        else:
            item = os.path.relpath(path, self.path.parent).replace("/", "\\")
            row = {field: "" for field in self.fields}
            row.update({"Title": path.stem, "Apple Files": item})
            if "Status" in row:
                row["Status"] = "Apple"
            if "Found In" in row:
                row["Found In"] = "Apple"
            self.rows.append(row)
            self.states[id(row)] = {}
            self.index[key_for(str(path), self.path.parent)] = (row, item)
        self.states[id(row)][item] = {
            "status": "uploaded", "destination": destination, "item_id": item_id,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        row[TRACKING] = json.dumps(self.states[id(row)], ensure_ascii=False, separators=(",", ":"))
        self.save()

    def save(self):
        if digest(self.path) != self.hash:
            raise SyncError("Library CSV changed externally; refusing to overwrite it.")
        name = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", dir=self.path.parent,
                                             prefix=".music-sync-", suffix=".tmp", delete=False) as f:
                name = f.name
                writer = csv.DictWriter(f, fieldnames=self.fields)
                writer.writeheader()
                writer.writerows(self.rows)
                f.flush()
                os.fsync(f.fileno())
            if digest(self.path) != self.hash:
                raise SyncError("Library CSV changed externally; refusing to overwrite it.")
            os.replace(name, self.path)
            name = None
            self.hash = digest(self.path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)


def scan(source):
    if not source.is_dir():
        raise SyncError(f"Apple Music folder not found: {source}")
    errors = []
    files = []
    def onerror(exc):
        errors.append(str(exc))
    for folder, dirs, names in os.walk(source, onerror=onerror, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(folder) / d).is_symlink()]
        for name in names:
            path = Path(folder) / name
            if path.suffix.lower() in EXTENSIONS and not path.is_symlink():
                files.append(path)
    if errors:
        raise SyncError("Unable to scan all folders: " + "; ".join(errors))
    return sorted(files, key=lambda p: str(p).casefold())


class Graph:
    def __init__(self, token):
        self.token = token
        self.folder_cache = {}

    def call(self, method, url, data=None, headers=None, auth=True):
        headers = dict(headers or {})
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        req = request.Request(url, data=data, method=method, headers=headers)
        try:
            with request.urlopen(req, timeout=120) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else {}
        except error.HTTPError as exc:
            # Never include the request URL: upload session URLs contain credentials.
            raise SyncError(f"OneDrive HTTP {exc.code}: {exc.reason}") from exc
        except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise SyncError(f"OneDrive request failed: {type(exc).__name__}") from exc

    def folder(self, parts):
        if not self.folder_cache:
            _, drive = self.call("GET", GRAPH + "/me/drive/root")
            self.folder_cache[()] = drive["id"]
        prefix = ()
        for part in parts:
            parent = self.folder_cache[prefix]
            prefix += (part,)
            if prefix in self.folder_cache:
                continue
            url = GRAPH + "/me/drive/items/" + parse.quote(parent, safe="") + ":/" + parse.quote(part, safe="") + ":"
            try:
                _, obj = self.call("GET", url)
            except SyncError as exc:
                if not str(exc).startswith("OneDrive HTTP 404:"):
                    raise
                body = json.dumps({"name": part, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}).encode()
                _, obj = self.call("POST", GRAPH + "/me/drive/items/" + parse.quote(parent, safe="") + "/children",
                                   body, {"Content-Type": "application/json"})
            if "folder" not in obj or not obj.get("id"):
                raise SyncError(f"Destination component is not a folder: {part}")
            self.folder_cache[prefix] = obj["id"]
        return self.folder_cache[prefix]

    def upload(self, path, parts):
        size = path.stat().st_size
        if size == 0:
            raise SyncError("Empty file cannot be uploaded with an upload session.")
        parent = self.folder(parts[:-1])
        url = GRAPH + "/me/drive/items/" + parse.quote(parent, safe="") + ":/" + parse.quote(parts[-1], safe="") + ":/createUploadSession"
        body = json.dumps({"item": {"@microsoft.graph.conflictBehavior": "fail"}}).encode()
        _, session = self.call("POST", url, body, {"Content-Type": "application/json"})
        upload_url = session.get("uploadUrl", "")
        parsed = parse.urlparse(upload_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise SyncError("OneDrive returned an invalid upload session URL.")
        result = None
        with path.open("rb") as f:
            offset = 0
            while offset < size:
                chunk = f.read(min(CHUNK_SIZE, size - offset))
                if not chunk:
                    raise SyncError("Source file changed during upload.")
                end = offset + len(chunk) - 1
                status, result = self.call("PUT", upload_url, chunk, {
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                }, auth=False)
                offset += len(chunk)
                if offset < size and status != 202:
                    raise SyncError(f"Unexpected upload status: {status}")
                if offset == size and status not in (200, 201):
                    raise SyncError(f"Upload did not complete (HTTP {status}).")
        if not result or result.get("size") != size or result.get("name") != parts[-1] or not result.get("id"):
            raise SyncError("OneDrive did not confirm the expected uploaded file.")
        return result["id"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--upload", action="store_true")
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--csv", type=Path, default=CSV_PATH)
    parser.add_argument("--target", default="Music")
    args = parser.parse_args(argv)
    source = args.source.resolve()
    target = PurePosixPath(args.target.replace("\\", "/"))
    if target.is_absolute() or not target.parts or any(p in (".", "..") for p in target.parts):
        raise SyncError("Target must be a relative OneDrive folder path without '..'.")
    library = Library(args.csv.resolve())
    files = scan(source)
    pending = [p for p in files if not library.is_uploaded(p)]
    print(f"Scanned: {len(files)}; recorded uploaded: {len(files) - len(pending)}; pending: {len(pending)}")
    print(f"Pending size: {sum(p.stat().st_size for p in pending) / (1024 ** 3):.2f} GiB")
    if args.preview or not pending:
        return 0
    token = os.environ.get("ONEDRIVE_ACCESS_TOKEN")
    if not token:
        raise SyncError("Set ONEDRIVE_ACCESS_TOKEN to a delegated Graph token for the intended OneDrive account.")
    graph = Graph(token)
    # Validate access before any writes to the remote drive or library.
    graph.call("GET", GRAPH + "/me/drive")
    succeeded = 0
    failed = 0
    for path in pending:
        relative = path.relative_to(source)
        parts = (*target.parts, *relative.parts)
        destination = "/".join(parts)
        try:
            before = path.stat()
            item_id = graph.upload(path, parts)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise SyncError("Source changed during upload; verify remote file manually.")
        except (SyncError, OSError) as exc:
            if str(exc).startswith("OneDrive HTTP 401:"):
                raise SyncError("OneDrive authorization failed or expired; sign in again before retrying.") from exc
            failed += 1
            print(f"FAILED {relative}: {exc}", file=sys.stderr)
            continue
        # A failed CSV write is fatal: continuing would lose confirmed upload tracking.
        try:
            library.record(path, destination, item_id)
        except (SyncError, OSError, csv.Error) as exc:
            raise SyncError(f"Upload completed for {relative}, but its tracking update failed: {exc}. "
                            "Reconcile the remote file before retrying.") from exc
        succeeded += 1
        print(f"Uploaded: {relative}")
    print(f"Uploaded: {succeeded}; failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SyncError, OSError, csv.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
