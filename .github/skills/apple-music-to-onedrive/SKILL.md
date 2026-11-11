---
name: apple-music-to-onedrive
description: "Scan the Apple Music library for new music and upload it to OneDrive, tracking confirmed uploads in Unified Music List.csv. Use when asked to sync or back up Apple Music to OneDrive."
---

# Apple Music to OneDrive

The source is `C:\Users\jebbb\Music\Apple Music` and the target is the
authenticated OneDrive account's `Music` folder. This is a local-file scan,
not an Apple purchase-history query. Subscription downloads may be protected
and copying them does not remove DRM.

1. Run a read-only preview:
   `python .github\skills\apple-music-to-onedrive\sync.py --preview`
2. Obtain a delegated Microsoft Graph access token for the intended OneDrive
   account with `Files.ReadWrite` permission. Supply it privately in the
   `ONEDRIVE_ACCESS_TOKEN` environment variable; do not print it or save it
   in the repository. The token must remain valid throughout the upload.
3. Run `python .github\skills\apple-music-to-onedrive\sync.py --upload`.
   It preserves relative folder paths, does not overwrite existing OneDrive
   files, and records each confirmed upload immediately in the existing CSV.
4. Report counts and failures. Re-run to retry failures. Existing destination
   conflicts require manual investigation, not automatic overwriting.

See [README.md](./README.md) for authentication, tracking and limitations.
