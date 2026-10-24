# Archive Downloader for R36S / dArkOS

Downloads single files from archive.org and installs them into the ROM folder of the right system.
Works on the console (with Wi-Fi) and on a PC with the SD card inserted. Python 3 standard library only.

**Disclaimer:** use it only for content you have the rights to (backups of games you own, public domain, homebrew). You are solely responsible for what you download. The tool shows a notice at startup that you must accept with **B** (**A** to quit).

## On the console (R36S)

1. Turn Wi-Fi on.
2. In EmulationStation: **Tools → Archive Downloader**, then accept the disclaimer with **B**.
3. Pick a source from the list, **Search archive.org...** to look for an item by title, or **Other URL...** for your own link.
4. Tick the files to download and press **X**.
5. Check the summary (number of files, total size, free space) and press **B** to download or **A** to cancel.
6. When done, quit with **A**: EmulationStation restarts and the games show up.

### Buttons

| Button | Action |
|---|---|
| Up / Down | move the cursor |
| Left / Right | previous / next page |
| B | tick the file (in menus: confirm) |
| X | download the ticked files |
| Y | tick / untick all |
| A | back |
| `/` (keyboard) | filter by name |

The top line shows `[files chosen, size chosen / total size of the list]`.

### What happens to the files

- If the system accepts the format (e.g. `.zip`, `.chd`), the file is copied as is.
- Otherwise the archive is extracted; if it holds several files, into a folder named after it.
- ScummVM: the `<folder>.scummvm` file is created too.
- Downloads resume where they stopped and are verified with MD5. Files already present are skipped.

## On a PC

```
python3 archive_dl.py --tui --sd /media/$USER/<SD>
python3 archive_dl.py --list-sources
python3 archive_dl.py --search "advance wars"
python3 archive_dl.py --source gba --sd /media/$USER/<SD> --match "regex"
python3 archive_dl.py <URL> --system gba --sd /media/$USER/<SD> --all
```

`--sd` is the mount point of the SD card; the ROM folder is read from `es_systems.cfg` (`--es-cfg` to point to another one).

## Adding a source

The repo only ships `archive_sources.example.conf`. Copy it to `archive_sources.conf` (same folder; it is not versioned) and edit it: if it exists, the tool uses that. On the console the folder is `/opt/system/Tools/`:

```ini
[nes]
Display name = https://archive.org/details/item-identifier
```

- The section is the system name in `es_systems.cfg` (`gba`, `psx`, `megadrive`, `ports`, `scummvm`...).
- Accepted: `/details/<id>`, `/download/<id>` and `/download/<id>/<file>` (single file).
- Lines starting with `#` are comments.
- Check with `python3 archive_dl.py --list-sources`.

## Troubleshooting

- **"python3 is not installed"**: the tool needs Python 3 on the console.
- **Network error**: check Wi-Fi; relaunching the download resumes from the `.part` file.
- **MD5 mismatch**: the file is discarded, try again.
- **Game not showing**: restart EmulationStation, or check you downloaded into the right system.
- Some items are huge (tens of GB): check the size in the summary before confirming.

## License

MIT, see the `LICENSE` file.
