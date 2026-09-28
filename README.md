# SubSync

A small desktop GUI to **batch-synchronise `.srt` subtitles to the audio of their video files**. It is a front end for [ffsubsync](https://github.com/smacke/ffsubsync).

Use it on a media library (Jellyfin, Plex, Emby, Kodi, …) when subtitles are a few seconds early or late, or when they drift over the course of an episode because they were timed for a release with a different framerate.

## Download

**[⬇ Download subsync.pyw (latest release)](https://github.com/Kudery/SubSync/releases/latest/download/subsync.pyw)**

All versions and release notes are on the [Releases page](https://github.com/Kudery/SubSync/releases). See [Requirements](#requirements) and [Installation](#installation-windows) below before the first start.

## Features

- Point it at a folder, for example a whole library, a show or a single season. It scans all subfolders for `.srt` files and pairs each one with its video (`Movie.srt` or `Movie.en.srt` with `Movie.mkv`).
- **Sync new** processes only subtitles that haven't been synced yet, so new additions are one click away.
- It tries two strategies for each file and keeps the better one:
  - a constant offset only;
  - offset plus framerate correction (`--gss`). This one is used only if it scores at least 10% better.
- It leaves a file untouched when the offset is below 0.3 s.
- It always syncs against the **audio** track (`--reference-stream a:0`) and never against embedded subtitles, which can be wrong themselves.
- Originals are kept as `name.srt.bak`. Media servers ignore that extension, and every re-run starts from the backup, so a re-run is always safe. **Restore original** reverts the selected files.
- It processes several files in parallel.
- It remembers the last folder and settings.

## Requirements

- Python 3.10+ with Tkinter. Tkinter is included in the python.org Windows installers.
- [ffmpeg](https://ffmpeg.org/) on your `PATH`.
- [ffsubsync](https://github.com/smacke/ffsubsync).

> **Python 3.14+ note:** ffsubsync depends on `webrtcvad-wheels`. When this was written, that package had no prebuilt wheels for Python 3.14, so pip tries to compile it and fails without the MSVC Build Tools. The simplest fix is to install ffsubsync into a separate Python 3.12 virtual environment, at the location SubSync looks for it. The app itself can keep running on 3.14.

## Installation (Windows)

```powershell
# ffmpeg
winget install Gyan.FFmpeg

# ffsubsync in a dedicated Python 3.12 venv (recommended)
py install 3.12            # or: winget install Python.Python.3.12
py -V:3.12 -m venv "$env:LOCALAPPDATA\SubSync\venv"
& "$env:LOCALAPPDATA\SubSync\venv\Scripts\python.exe" -m pip install ffsubsync
```

[Download `subsync.pyw`](https://github.com/Kudery/SubSync/releases/latest/download/subsync.pyw) and start it with a double-click.

If you are on Python ≤ 3.13, you can skip the venv. The app offers to install ffsubsync into its own interpreter on first start.

### Smart App Control / "file blocked"

Windows flags downloaded files with the "Mark of the Web", and Smart App Control may block them. Unblock the file and start it through `pythonw.exe` with a shortcut:

```powershell
Unblock-File "C:\path\to\subsync.pyw"
$py = (Get-Command pythonw.exe).Source
$s = (New-Object -ComObject WScript.Shell).CreateShortcut("$env:USERPROFILE\Desktop\SubSync.lnk")
$s.TargetPath = $py
$s.Arguments  = '"C:\path\to\subsync.pyw"'
$s.Save()
```

Replace `C:\path\to` with the folder where you saved `subsync.pyw`.

## Installation (Linux / macOS)

```bash
# ffmpeg and Tkinter via your package manager, e.g. Debian/Ubuntu:
sudo apt install ffmpeg python3-tk python3.12-venv
python3.12 -m venv ~/.local/share/SubSync/venv
~/.local/share/SubSync/venv/bin/python -m pip install ffsubsync
curl -LO https://github.com/Kudery/SubSync/releases/latest/download/subsync.pyw
python3 subsync.pyw
```

## Usage

1. Click **Browse…** and pick a folder. Mapped network drives and UNC paths work.
2. **Sync new** syncs every subtitle that has no `.srt.bak` yet.
3. Check the result in your player. If a file got worse, select it and click **Restore original**.
4. **Sync selected** or **Sync all** re-sync from the original backups.

Status colours:

| Status | Meaning |
|---|---|
| `OK` | Subtitle was shifted and/or rescaled. |
| `IN SYNC` | Offset below 0.3 s; the file is left unchanged. |
| `FAIL` | ffsubsync failed; the original is kept. |
| `SKIP` | No matching video file was found. |

After syncing, refresh the metadata in your media server so it picks up the changed files.

### Tips

- **Speed.** Each file takes roughly 20–60 s because it runs two full audio analyses. If the files are on a NAS, reading over the network can become the bottleneck before the CPU does.
- **Results are not always perfect.** A global sync cannot fix subtitles whose individual lines are timed inconsistently. If a file still drifts, look for a subtitle made for your exact release.
- **Framerate drift.** The typical symptom is subtitles that are late at the start, correct in the middle and early at the end. `--gss` handles this.

## Where things are stored

| What | Windows | Linux/macOS |
|---|---|---|
| Settings | `%APPDATA%\SubSync\config.json` | `~/.config/SubSync/config.json` |
| ffsubsync venv (optional) | `%LOCALAPPDATA%\SubSync\venv` | `~/.local/share/SubSync/venv` |

## License

MIT — see [LICENSE](LICENSE). ffsubsync and ffmpeg are separate projects with their own licenses.
