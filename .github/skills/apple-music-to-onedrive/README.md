# Apple Music to OneDrive

Run from the repository root using Python 3:

```powershell
python .github\skills\apple-music-to-onedrive\sync.py --preview
$secureToken = Read-Host "Microsoft Graph access token" -AsSecureString
$env:ONEDRIVE_ACCESS_TOKEN = [System.Net.NetworkCredential]::new('', $secureToken).Password
python .github\skills\apple-music-to-onedrive\sync.py --upload
Remove-Item Env:\ONEDRIVE_ACCESS_TOKEN
Remove-Variable secureToken
```

Provide a delegated Microsoft Graph token for the **intended** OneDrive
account, with `Files.ReadWrite` permission and audience
`https://graph.microsoft.com`. For example, sign in to
[Microsoft Graph Explorer](https://developer.microsoft.com/graph/graph-explorer)
with the intended account, use the profile's **Consent to permissions** menu
to consent to `Files.ReadWrite`, and copy the token from the **Access token**
tab. An Azure CLI account may be a different account and may not have
OneDrive. Tokens expire and are not refreshed by this tool; if one expires,
obtain a fresh token and re-run. Do not paste a token into chat or commit it.
The script does not display or save the token.

The preview never modifies files or calls OneDrive. Uploading requires network
access and uses the Graph upload session API. The relative path under Apple
Music is preserved below OneDrive `Music`. Existing remote files are not
overwritten: conflicts are reported for manual reconciliation. Upload status is
stored in the `OneDrive Uploads` column of the existing unified CSV as a JSON
object keyed by each individual Apple file path, because a song row can contain
multiple Apple files. Existing library columns and rows are preserved; newly
discovered files get a new row. Only a completed upload is recorded, and the
CSV is atomically updated after each success. Close Excel before running.

The script skips files already recorded as uploaded, continues after individual
failures, exits nonzero if any fail, and refuses to overwrite a concurrently
modified CSV. A remote upload that completes but is not recorded (for example
if the CSV is locked) will conflict on retry and needs manual reconciliation.
The tracker records a confirmed Graph upload response, not playback rights:
protected Apple Music downloads may not play on other devices.

Customize paths with `--source`, `--csv`, and `--target`. `--target` is a
folder path within the current OneDrive, not a local Windows path.
