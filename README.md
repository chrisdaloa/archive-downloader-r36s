# Archive Downloader for R36S / dArkOS

Downloads single files from archive.org and installs them into the ROM folder of the right system.
Works on the console (with Wi-Fi) and on a PC with the SD card inserted. Python 3 standard library only.

**Disclaimer:** use it only for content you have the rights to (backups of games you own, public domain, homebrew). You are solely responsible for what you download. The tool shows a notice at startup that you must accept with **B** (**A** to quit).

## Installing on the SD card

The console's ROM partition (the big exFAT one, usually labelled `EASYROMS`) has a `tools` folder: it is what EmulationStation shows under **Tools**. It is `/opt/system/Tools/` on the console.

1. Power off the console and put its SD card in the PC (or use a card reader).
2. Download this repository (green **Code** button → **Download ZIP**, or `git clone`).
3. Copy these files from `opt/system/Tools/` into the `tools` folder of the ROM partition:
   - `Archive Downloader.sh`
   - `archive_dl.py`
   - `archive_sources.example.conf` (optional but handy: rename a copy to `archive_sources.conf` to edit the source list)
4. Eject the card, put it back, boot the console. **Tools → Archive Downloader** appears (if not, restart EmulationStation).

Once installed, **Update tool...** keeps the files current from this repository, so you only need to copy them by hand the first time. The console needs Python 3 (already included in dArkOS) and Wi-Fi.

## On the console (R36S)

1. Turn Wi-Fi on.
2. In EmulationStation: **Tools → Archive Downloader**, then accept the disclaimer with **B**.
3. Pick a source from the list, **Search archive.org...** to look for an item by title, or **Other URL...** for your own link. **Update tool...** downloads the latest version from this repository.
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
| Start | open the `.zip` under the cursor and pick single files inside it |
| A | back |
| `/` (keyboard) | filter by name |
| `o` (keyboard) | same as Start |

A row too long for the screen scrolls by itself while the cursor rests on it, so you can read the whole name and the size.

The top line shows `[files chosen, size chosen / total size of the list]`.

### Single games inside a zip

Some items hold one big `.zip` with the whole collection (e.g. `GBC.zip`, hundreds of MB). Put the cursor on it and press **Start**: archive.org provides the index, you tick only the games you want and just those are downloaded, without fetching the whole zip. When an item is essentially one huge `.zip` (1 GiB or more, 90% of the item, e.g. the 19 GB FBNeo romset) the index opens by itself, for any system; going back returns to the source list. If the item also has a `.dat` file (ClrMamePro/Logiqx, like FBNeo's), the cryptic romset names (`1941.zip`) are shown with the full title, year and maker (`1941: Counter Attack (World) (1990, Capcom)`) and the `/` filter searches them too. The `.dat` is downloaded once and cached in `cache/` next to the script. Only `.zip` is supported (not `.7z`/`.rar`). Single files from a zip have no checksum: the size is checked, and an interrupted download restarts from zero.

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
python3 archive_dl.py --update
python3 archive_dl.py --gamelist --sd /media/SDCARD
python3 archive_dl.py https://archive.org/details/gbc_20240311 --zip GBC.zip --match "^GBC/1942 " --system gbc --sd /media/$USER/<SD>
python3 archive_dl.py --source gba --sd /media/$USER/<SD> --match "regex"
python3 archive_dl.py <URL> --system gba --sd /media/$USER/<SD> --all
```

`--sd` is the mount point of the SD card; the ROM folder is read from `es_systems.cfg` (`--es-cfg` to point to another one).

## archive.org login (restricted items)

A few items (e.g. the full PortMaster pack `portmaster_ports_202405`) can only be downloaded by logged-in users: without a login archive.org answers 401/403 and the tool says "Login required". Public items work with or without a login.

**Where to put the keys:** in `archive_auth.conf`, next to `archive_dl.py`:

- on the console: `/opt/system/Tools/archive_auth.conf`
- on a PC: `opt/system/Tools/archive_auth.conf` in this repository

The file is listed in `.gitignore`, so git never tracks it and your keys are not pushed. Only the template `archive_auth.example.conf` is versioned, and the updater never touches your `archive_auth.conf`.

1. Create a free account on archive.org and get your keys at <https://archive.org/account/s3.php>.
2. Copy `archive_auth.example.conf` to `archive_auth.conf` and fill it in:

   ```ini
   [archive.org]
   access = YOUR_ACCESS_KEY
   secret = YOUR_SECRET_KEY
   ```

   Or from a PC, straight onto the console:

   ```
   ssh ark@<console-ip> "printf '[archive.org]\naccess = XXX\nsecret = YYY\n' > /opt/system/Tools/archive_auth.conf"
   ```

The keys are only sent to `archive.org` hosts. They are stored in clear text, and the ROM partition ignores file permissions, so anyone with the console or the SD card can read them: never commit or share the file. If they leak, regenerate them on the same archive.org page.

## Updating

**Port covers.** After installing a PortMaster port, the tool reads each port's `gameinfo.xml` and adds its cover, description, developer and genre to `/roms/ports/gamelist.xml` (existing fields are never overwritten, the previous file is kept as `gamelist.xml.bak`). Scrapers usually find nothing for ports. Run `--gamelist` to do it for ports already installed; restart EmulationStation afterwards.

**Update tool...** in the menu (or `--update` on a PC) compares `archive_dl.py`, `Archive Downloader.sh`, `archive_sources.example.conf` and `archive_auth.example.conf` with the `main` branch of this repository and replaces the ones that differ. Your `archive_sources.conf` and `archive_auth.conf` are never touched, the previous script is kept as `archive_dl.py.bak`, and nothing is written if the downloaded script does not compile. Relaunch the tool afterwards. The installed version is shown in the title of the main menu (`VERSION` in `archive_dl.py`, also `--version`); the update screen shows `vX -> vY`. Bump `VERSION` whenever a change should reach users. It needs Wi-Fi; it runs whatever is on `main`, so only use it if you trust the repository.

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
