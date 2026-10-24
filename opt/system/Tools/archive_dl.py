#!/usr/bin/env python3
"""Download single files from an archive.org item and install them into the ROM folder of the system.

Usage on a PC (SD card mounted):
    archive_dl.py --list-sources
    archive_dl.py --search "advance wars"
    archive_dl.py --update
    archive_dl.py https://archive.org/details/gbc_20240311 --zip GBC.zip --match "1942" --system gbc --sd /media/user/EASYROMS
    archive_dl.py --tui --sd /media/user/EASYROMS
    archive_dl.py --source gba --match "advance wars" --sd /media/user/EASYROMS
    archive_dl.py https://archive.org/download/gba-italian-romset_202201 --system gba --sd /media/user/EASYROMS

Usage on the console: launched by "Archive Downloader.sh" with the subcommands
sources / files / get (dialog-friendly output).
"""
import argparse
import configparser
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONF = os.path.join(HERE, "archive_sources.conf")
AUTH_CONF = os.path.join(HERE, "archive_auth.conf")
DEFAULT_ES_CFG = "/etc/emulationstation/es_systems.cfg"
VERSION = "1.4.0"  # bump on every change that should reach users through "Update tool..."
USER_AGENT = "archive_dl/%s (dArkOS R36S)" % VERSION
CHUNK = 1024 * 256
DL_CHUNK = 1024 * 32  # network read size: small, so the TUI can react to a cancel quickly

EXT_TO_SYSTEM = {
    ".gba": "gba", ".gb": "gb", ".gbc": "gbc", ".nes": "nes", ".fds": "fds",
    ".sfc": "snes", ".smc": "snes", ".md": "megadrive", ".gen": "megadrive",
    ".smd": "megadrive", ".sms": "mastersystem", ".gg": "gamegear",
    ".pce": "pcengine", ".nds": "nds", ".n64": "n64", ".z64": "n64", ".v64": "n64",
}
ARCHIVE_EXT = (".zip", ".7z")
SKIP_SUFFIXES = ("_meta.xml", "_files.xml", "_meta.sqlite", "_reviews.xml", "__ia_thumb.jpg", ".torrent")


class Error(Exception):
    pass


class Cancelled(Exception):
    """Download cancelled by the user; the .part file is kept for resuming."""


# --------------------------------------------------------------------------
# archive.org
# --------------------------------------------------------------------------
def auth_header(url):
    """archive.org S3 keys from archive_auth.conf, only for archive.org hosts (never sent elsewhere)."""
    host = urllib.parse.urlparse(url).hostname or ""
    if host != "archive.org" and not host.endswith(".archive.org"):
        return {}
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(AUTH_CONF, encoding="utf-8")
        return {"Authorization": "LOW %s:%s" % (cp["archive.org"]["access"].strip(),
                                                cp["archive.org"]["secret"].strip())}
    except (KeyError, configparser.Error):
        return {}


def http_open(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **auth_header(url), **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def parse_url(url):
    """Return (identifier, single_file_or_None)."""
    u = urllib.parse.urlparse(url)
    if "archive.org" not in u.netloc:
        raise Error("Not an archive.org URL: %s" % url)
    parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
    if len(parts) < 2 or parts[0] not in ("download", "details"):
        raise Error("Unrecognized URL (expected /download/<id> or /details/<id>)")
    single = "/".join(parts[2:]) if parts[0] == "download" and len(parts) > 2 else None
    return parts[1], single


def list_files(identifier):
    import json
    try:
        with http_open("https://archive.org/metadata/" + urllib.parse.quote(identifier)) as r:
            data = json.load(r)
    except (urllib.error.URLError, OSError) as e:
        raise Error("Cannot read metadata: %s" % e)
    if not data.get("files"):
        raise Error("Empty or nonexistent item: %s" % identifier)
    out = []
    for f in data["files"]:
        name = f.get("name", "")
        if not name or name.endswith(SKIP_SUFFIXES) or name.startswith("history/"):
            continue
        if f.get("source") == "derivative" or f.get("format") in ("Metadata", "Item Tile"):
            continue
        out.append({"name": name, "size": int(f.get("size") or 0), "md5": f.get("md5", "")})
    out.sort(key=lambda x: x["name"].lower())
    return out


ZIP_ROW = re.compile(r'<tr><td><a href="([^"]+)">([^<]+)</a><td><td>[^<]*<td id="size">(\d+)</tr>')


def list_zip_members(identifier, zipname):
    """List the files inside a .zip of an item without downloading it (archive.org builds the index).
    Entries carry their own "url": archive.org serves a single member from it."""
    import html
    url = "https://archive.org/download/%s/%s/" % (urllib.parse.quote(identifier), urllib.parse.quote(zipname))
    try:
        with http_open(url, timeout=120) as r:
            page = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise Error("Login required for %s: add your archive.org keys to archive_auth.conf" % zipname)
        raise Error("Cannot read the zip index: %s" % e)
    except (urllib.error.URLError, OSError) as e:
        raise Error("Cannot read the zip index: %s" % e)
    out = [{"name": html.unescape(name), "size": int(size), "md5": "",
            "url": "https:" + href if href.startswith("//") else href}
           for href, name, size in ZIP_ROW.findall(page)]
    if not out:
        raise Error("No index available for %s" % zipname)
    out.sort(key=lambda x: x["name"].lower())
    return out


DAT_MAX = 64 << 20
CACHE_DIR = os.path.join(HERE, "cache")


def find_dat(files):
    """The ClrMamePro/Logiqx .dat shipped with an item (it maps romset names to full titles), or None."""
    dats = [f for f in files if f["name"].lower().endswith(".dat") and 0 < f["size"] <= DAT_MAX]
    return max(dats, key=lambda f: f["size"], default=None)


def parse_dat(fh):
    """{zip name (lowercase): "Full title (year, manufacturer)"} from a Logiqx XML .dat, in constant memory."""
    titles = {}
    for _ev, el in ET.iterparse(fh, events=("end",)):
        if el.tag in ("game", "machine"):
            name = el.get("name")
            desc = (el.findtext("description") or "").strip()
            if name and desc:
                extra = ", ".join(x for x in ((el.findtext("year") or "").strip(),
                                              (el.findtext("manufacturer") or "").strip()) if x and x != "<unknown>")
                titles[name.lower()] = desc + (" (%s)" % extra if extra else "")
            el.clear()
    return titles


def load_titles(identifier, files):
    """Full game names for the romsets of an item, from its .dat (downloaded once, then cached). {} if none."""
    dat = find_dat(files)
    if not dat:
        return {}
    cache = os.path.join(CACHE_DIR, "%s_%s.tsv" % (re.sub(r"\W+", "_", identifier), dat["md5"] or dat["size"]))
    try:
        with open(cache, encoding="utf-8") as fh:
            return dict(line.rstrip("\n").split("\t", 1) for line in fh if "\t" in line)
    except OSError:
        pass
    url = "https://archive.org/download/%s/%s" % (urllib.parse.quote(identifier), urllib.parse.quote(dat["name"]))
    try:
        with http_open(url, timeout=120) as r:
            titles = parse_dat(r)
    except (urllib.error.URLError, OSError, ET.ParseError):
        return {}
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            for k, v in titles.items():
                fh.write("%s\t%s\n" % (k, v.replace("\t", " ").replace("\n", " ")))
    except OSError:
        pass
    return titles


def search_items(query, rows=50):
    """Search archive.org software items. Return [{"id", "title", "size"}], most downloaded first."""
    import json
    words = query.replace('"', " ").split()
    if not words:
        raise Error("Empty search")
    query = " AND ".join(words)
    params = urllib.parse.urlencode({
        "q": "title:(%s) AND mediatype:software" % query,
        "fl[]": ["identifier", "title", "item_size"],
        "sort[]": "downloads desc",
        "rows": rows,
        "output": "json",
    }, doseq=True)
    try:
        with http_open("https://archive.org/advancedsearch.php?" + params) as r:
            data = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Error("Search failed: %s" % e)
    return [{"id": d["identifier"], "title": d.get("title") or d["identifier"],
             "size": int(d.get("item_size") or 0)}
            for d in data.get("response", {}).get("docs", []) if d.get("identifier")]


UPDATE_BASE = "https://raw.githubusercontent.com/chrisdaloa/archive-downloader-r36s/main/opt/system/Tools/"
UPDATE_FILES = ("archive_dl.py", "Archive Downloader.sh", "archive_sources.example.conf",
                "archive_auth.example.conf")  # never archive_sources.conf / archive_auth.conf


def check_update():
    """Compare the installed files with the repository. Return [(name, new_content)] for those that differ."""
    changed = []
    for name in UPDATE_FILES:
        try:
            with http_open(UPDATE_BASE + urllib.parse.quote(name)) as r:
                data = r.read()
        except (urllib.error.URLError, OSError) as e:
            raise Error("Cannot reach the repository: %s" % e)
        path = os.path.join(HERE, name)
        old = None
        if os.path.exists(path):
            with open(path, "rb") as fh:
                old = fh.read()
        if data != old:
            changed.append((name, data))
    return changed


def version_of(code):
    """VERSION declared in the source of archive_dl.py (bytes or str), or None."""
    if isinstance(code, bytes):
        code = code.decode("utf-8", "replace")
    m = re.search(r'^VERSION = "([^"]+)"', code, re.M)
    return m.group(1) if m else None


def apply_update(changed):
    """Replace the files (the old script is kept as .bak). Nothing is written if the new code does not compile."""
    for name, data in changed:
        if name.endswith(".py"):
            try:
                compile(data, name, "exec")
            except (SyntaxError, ValueError) as e:
                raise Error("Downloaded %s is not valid Python (%s): update aborted" % (name, e))
    try:
        for name, data in changed:
            path = os.path.join(HERE, name)
            mode = os.stat(path).st_mode if os.path.exists(path) else 0o644
            if os.path.exists(path):
                shutil.copy2(path, path + ".bak")
            tmp = path + ".new"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, mode)
            os.replace(tmp, path)  # new inode: a running copy of the .sh keeps reading the old one
    except OSError as e:
        raise Error("Cannot write the update: %s" % e)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def download(identifier, entry, dest, on_progress=None):
    """Download to dest (via dest.part) with resume and MD5 check."""
    url = entry.get("url") or "https://archive.org/download/%s/%s" % (
        urllib.parse.quote(identifier), urllib.parse.quote(entry["name"]))
    part = dest + ".part"
    total = entry["size"]
    for _attempt in range(2):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if total and have > total:
            os.remove(part)
            have = 0
        headers = {"Range": "bytes=%d-" % have} if have else {}
        try:
            r = http_open(url, headers)
        except urllib.error.HTTPError as e:
            if e.code == 416 and have:  # already complete
                break
            if e.code in (401, 403):
                raise Error("Login required for %s: add your archive.org keys to archive_auth.conf" % entry["name"])
            raise Error("HTTP %d while downloading %s" % (e.code, entry["name"]))
        except (urllib.error.URLError, OSError) as e:
            raise Error("Network error: %s" % e)
        with r:
            if have and r.status != 206:  # server ignores Range: restart from zero
                have = 0
            with open(part, "ab" if have else "wb") as f:
                done = have
                while True:
                    chunk = r.read(DL_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
        if total and os.path.getsize(part) < total:
            continue  # connection dropped: a second attempt resumes from the .part
        break
    if entry["md5"] and md5_of(part) != entry["md5"]:
        os.remove(part)
        raise Error("MD5 mismatch for %s" % entry["name"])
    if not entry["md5"] and total and os.path.getsize(part) != total:  # zip member: no checksum, size only
        os.remove(part)
        raise Error("Incomplete download for %s" % entry["name"])
    return part


# --------------------------------------------------------------------------
# Setup / paths
# --------------------------------------------------------------------------
def load_sources(conf_path):
    cp = configparser.ConfigParser(delimiters=("=",), interpolation=None,
                                   comment_prefixes=("#", ";"), inline_comment_prefixes=None)
    cp.optionxform = str
    if conf_path == DEFAULT_CONF and not os.path.exists(conf_path):
        conf_path = os.path.join(HERE, "archive_sources.example.conf")  # no personal setup
    if not cp.read(conf_path, encoding="utf-8"):
        raise Error("Setup file not found: %s" % conf_path)
    return [(sec, name.strip(), url.strip())
            for sec in cp.sections() for name, url in cp.items(sec) if url.strip()]


def load_systems(es_cfg):
    """{name: (path, [extensions])} from es_systems.cfg."""
    if not os.path.exists(es_cfg):
        return {}
    systems = {}
    for s in ET.parse(es_cfg).getroot().iter("system"):
        name, path, ext = (s.findtext(t, "").strip() for t in ("name", "path", "extension"))
        if name and path:
            systems[name] = (path.rstrip("/"), [e.lower() for e in ext.split()])
    return systems


def resolve_rom_dir(system, systems, sd=None):
    """Return (destination_folder, accepted_extensions)."""
    path, exts = systems.get(system, (None, []))
    folder = os.path.basename(path) if path else system
    if sd:
        for cand in (os.path.join(sd, "roms", folder), os.path.join(sd, folder)):
            if os.path.isdir(cand):
                return cand, exts
        base = os.path.join(sd, "roms") if os.path.isdir(os.path.join(sd, "roms")) else sd
        return os.path.join(base, folder), exts
    if not path:
        raise Error("System '%s' not found in es_systems.cfg (use --sd on a PC)" % system)
    return path, exts


def install(part, name, dest_dir, exts):
    """Move/extract the downloaded file. Return a description."""
    base = os.path.basename(name)
    ext = os.path.splitext(base)[1].lower()
    final = os.path.join(dest_dir, base)
    if not exts or ext in exts or ext not in ARCHIVE_EXT:
        os.replace(part, final)
        return base
    # archive not accepted by the emulator: extract into a temporary folder
    tmp = os.path.join(dest_dir, ".extract_" + os.path.splitext(base)[0][:40])
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        if ext == ".zip":
            with zipfile.ZipFile(part) as z:
                z.extractall(tmp)
        else:
            tool = next((t for t in ("7z", "7za", "7zr") if shutil.which(t)), None)
            if not tool:
                os.replace(part, final)
                raise Error("%s not accepted by the emulator and 7z is not available: left archived" % base)
            subprocess.run([tool, "x", "-y", "-o" + tmp, part],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        entries = os.listdir(tmp)
        # several top-level entries (e.g. ScummVM games): folder named after the archive,
        # unless one of them is a file the system launches itself (e.g. a port: "Game.sh" + "game/")
        launcher = any(os.path.isfile(os.path.join(tmp, n)) and os.path.splitext(n)[1].lower() in exts
                       for n in entries)
        wrap = len(entries) > 1 and not launcher
        target = os.path.join(dest_dir, os.path.splitext(base)[0]) if wrap else dest_dir
        os.makedirs(target, exist_ok=True)
        for name in entries:
            src = os.path.join(tmp, name)
            dst = os.path.join(target, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                os.replace(src, dst)
    except (zipfile.BadZipFile, subprocess.CalledProcessError, OSError) as e:
        raise Error("Extraction failed for %s: %s" % (base, e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    os.remove(part)
    # ScummVM: ES only lists .scummvm files, which contain the game folder name
    if ".scummvm" in exts:
        games = [os.path.basename(target)] if wrap else \
                [n for n in entries if os.path.isdir(os.path.join(dest_dir, n))]
        for g in games:
            shortcut = os.path.join(dest_dir, g + ".scummvm")
            if not os.path.exists(shortcut):
                with open(shortcut, "w", encoding="utf-8") as fh:
                    fh.write(g + "\n")
    return base + (" (extracted to folder)" if wrap else " (extracted)")


def update_gamelist(dest_dir):
    """Add to gamelist.xml the entries of the ports that ship a gameinfo.xml (cover, description...).
    Existing fields are never overwritten. Return the number of games touched."""
    gl = os.path.join(dest_dir, "gamelist.xml")
    try:
        tree = ET.parse(gl) if os.path.isfile(gl) else ET.ElementTree(ET.Element("gameList"))
    except ET.ParseError:
        return 0
    root = tree.getroot()
    games = {os.path.normpath(g.findtext("path") or ""): g for g in root.findall("game")}
    touched = 0
    for folder in sorted(os.listdir(dest_dir)):
        info = os.path.join(dest_dir, folder, "gameinfo.xml")
        if not os.path.isfile(info):
            continue
        try:
            src_games = ET.parse(info).getroot().findall("game")
        except ET.ParseError:
            continue
        for sg in src_games:
            key = os.path.normpath(sg.findtext("path") or "")
            if not os.path.isfile(os.path.join(dest_dir, key)):
                continue
            g = games.get(key)
            if g is None:
                g = ET.SubElement(root, "game")
                games[key] = g
            changed = False
            for field in list(sg):
                if not (field.text or "").strip():
                    continue
                if field.tag == "image" and not os.path.isfile(os.path.join(dest_dir, field.text.strip())):
                    continue
                cur = g.find(field.tag)
                if cur is None:
                    cur = ET.SubElement(g, field.tag)
                if not (cur.text or "").strip():
                    cur.text = field.text.strip()
                    changed = True
            img = g.findtext("image")
            if img and g.find("thumbnail") is None:
                ET.SubElement(g, "thumbnail").text = img
                changed = True
            touched += changed
    if touched:
        if os.path.isfile(gl):
            shutil.copy2(gl, gl + ".bak")
        ET.indent(tree, space="\t")
        tree.write(gl, encoding="utf-8", xml_declaration=True)
    return touched


def install_entries(identifier, entries, system, systems, sd, gauge=False, out=print, hook=None):
    dest_dir, exts = resolve_rom_dir(system, systems, sd)
    os.makedirs(dest_dir, exist_ok=True)
    errors = []
    for i, e in enumerate(entries):
        base = os.path.basename(e["name"])
        final = os.path.join(dest_dir, base)
        label = "[%d/%d] %s" % (i + 1, len(entries), base)
        present = os.path.exists(final) and (md5_of(final) == e["md5"] if e["md5"]
                                             else bool(e["size"]) and os.path.getsize(final) == e["size"])
        if present:
            if hook:
                hook((i + 1) * 100 // len(entries), label + " (already present)", e["size"], e["size"])
            else:
                out("%s: already present" % label)
            continue

        def progress(done, total, i=i, label=label):
            pct = (i * 100 + (done * 100 // total if total else 0)) // len(entries)
            if hook:
                hook(pct, label, done, total)
            elif gauge:
                print("XXX\n%d\n%s\nXXX" % (pct, label), flush=True)
            else:
                sys.stdout.write("\r%s %3d%%" % (label[:60], done * 100 // total if total else 0))
                sys.stdout.flush()
        try:
            part = download(identifier, e, os.path.join(dest_dir, base), progress)
            desc = install(part, e["name"], dest_dir, exts)
            if not gauge and not hook:
                print("\r%s: OK -> %s" % (label[:60], desc))
        except Error as err:
            errors.append(str(err))
            if not hook:
                print(("\n" if not gauge else "") + "ERROR: %s" % err, file=sys.stderr, flush=True)
    if ".sh" in exts:
        update_gamelist(dest_dir)
    return dest_dir, errors


# --------------------------------------------------------------------------
# Interactive selection (PC)
# --------------------------------------------------------------------------
def parse_selection(text, n):
    chosen = set()
    for tok in re.split(r"[,\s]+", text.strip()):
        if not tok:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", tok)
        if not m:
            raise ValueError(tok)
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if not 1 <= a <= b <= n:
            raise ValueError(tok)
        chosen.update(range(a, b + 1))
    return sorted(chosen)


def select_interactive(files):
    view = files
    while True:
        for i, f in enumerate(view, 1):
            print("%4d) %s  (%s)" % (i, f["name"], human(f["size"])))
        ans = input("\nNumbers (e.g. 1-3,7), /text to filter, a=all, r=reset, q=quit: ").strip()
        if ans == "q":
            return []
        if ans == "a":
            return view
        if ans == "r":
            view = files
        elif ans.startswith("/"):
            needle = ans[1:].lower()
            view = [f for f in files if needle in f["name"].lower()] or files
        else:
            try:
                return [view[i - 1] for i in parse_selection(ans, len(view))]
            except ValueError as e:
                print("Invalid selection: %s" % e)


# --------------------------------------------------------------------------
# TUI (curses): arrows/PgUp/PgDn, Enter and Esc only, so it is usable with a gamepad
# --------------------------------------------------------------------------
OSK = "/opt/inttools/osk.py"
GAMEPAD = False  # set by --pad (console launcher): footers show the gamepad buttons instead of the keyboard keys
EXIT_INSTALLED = 20  # TUI exit code: something was installed


def _put(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w - 1:
        try:
            win.addstr(y, x, text[:w - 1 - x], attr)
        except Exception:
            pass


def marquee(text, width, tick):
    """The part of a too-long text to show at this tick: rests at the start, scrolls to the end, rests, restarts."""
    over = len(text) - width
    if over <= 0:
        return text
    pause = 4
    off = tick % (over + 2 * pause)
    return text[min(max(off - pause, 0), over):][:width]


def _getch(win, scrolling):
    """getch(); with scrolling=True it gives up after 300 ms and returns -1 so the caller can animate."""
    win.timeout(300 if scrolling else -1)
    k = win.getch()
    win.timeout(-1)
    return k


def _frame(win, title, footer):
    import curses
    win.erase()
    h, w = win.getmaxyx()
    _put(win, 0, 0, (" " + title).ljust(w - 1), curses.A_REVERSE)
    _put(win, h - 1, 0, footer, curses.A_DIM)


def _is_back(k):
    import curses
    return k in (27, ord("q"), 8, 127, curses.KEY_BACKSPACE)


def _move(k, cur, n, page):
    import curses
    if k in (curses.KEY_UP, ord("k")):
        return max(0, cur - 1)
    if k in (curses.KEY_DOWN, ord("j")):
        return min(n - 1, cur + 1)
    if k in (curses.KEY_PPAGE, curses.KEY_LEFT):
        return max(0, cur - page)
    if k in (curses.KEY_NPAGE, curses.KEY_RIGHT):
        return min(n - 1, cur + page)
    if k == curses.KEY_HOME:
        return 0
    if k == curses.KEY_END:
        return n - 1
    return cur


def tui_menu(win, title, labels, footer=None):
    import curses
    if footer is None:
        footer = "Up/Down=move  B=ok  A=back" if GAMEPAD else "Up/Down choose  Enter ok  Esc back"
    cur = top = tick = 0
    while True:
        _frame(win, title, footer)
        h, w = win.getmaxyx()
        rows = h - 2
        if cur < top:
            top = cur
        elif cur >= top + rows:
            top = cur - rows + 1
        for r in range(rows):
            i = top + r
            if i >= len(labels):
                break
            text = marquee(labels[i], w - 3, tick) if i == cur else labels[i]
            _put(win, 1 + r, 0, ("> " if i == cur else "  ") + text,
                 curses.A_REVERSE if i == cur else 0)
        win.refresh()
        k = _getch(win, cur < len(labels) and len(labels[cur]) > w - 3)
        if k == -1:
            tick += 1
            continue
        tick = 0
        if k in (10, 13, curses.KEY_ENTER):
            return cur
        if _is_back(k):
            return None
        cur = _move(k, cur, len(labels), rows)


def tui_message(win, title, lines, wait=True):
    _frame(win, title, "Press any key" if wait else "")
    h, _w = win.getmaxyx()
    for i, line in enumerate(lines[:h - 3]):
        _put(win, 2 + i, 1, line)
    win.refresh()
    if wait:
        win.getch()


def tui_text(win, prompt):
    """Keyboard text input. Return None if cancelled."""
    import curses
    buf = ""
    curses.curs_set(1)
    try:
        while True:
            h, _w = win.getmaxyx()
            win.move(h - 2, 0)
            win.clrtoeol()
            _put(win, h - 2, 0, prompt + buf)
            win.refresh()
            k = win.getch()
            if k in (10, 13, curses.KEY_ENTER):
                return buf
            if k == 27:
                return None
            if k in (8, 127, curses.KEY_BACKSPACE):
                buf = buf[:-1]
            elif 32 <= k < 127:
                buf += chr(k)
    finally:
        curses.curs_set(0)


def tui_ask(win, title, prompt):
    import curses
    if os.path.exists(OSK):  # on-screen keyboard (gamepad)
        curses.def_prog_mode()
        curses.endwin()
        res = subprocess.run([sys.executable, OSK, "--backtitle", title, "--inputbox", prompt.rstrip(": ")],
                             stderr=subprocess.PIPE, text=True,
                             env={**os.environ, "PYTHONWARNINGS": "ignore"})
        curses.reset_prog_mode()
        win.clear()
        win.refresh()
        lines = res.stderr.strip().splitlines()  # the typed text is the last line; warnings may precede it
        return lines[-1].strip() if res.returncode == 0 and lines else None
    return tui_text(win, prompt)


def tui_ask_url(win):
    return tui_ask(win, "URL archive.org", "URL: ")


def tui_search(win):
    """Ask for a query and pick one result. Return the item URL, or None."""
    query = tui_ask(win, "Search archive.org", "Search: ")
    if not query or not query.strip():
        return None
    tui_message(win, "Archive Downloader", ["Searching..."], wait=False)
    try:
        results = search_items(query)
    except Error as e:
        tui_message(win, "Error", [str(e)])
        return None
    if not results:
        tui_message(win, "Search", ["No results for: %s" % query])
        return None
    labels = ["%s [%s]" % (r["title"], human(r["size"])) for r in results]
    i = tui_menu(win, "Results: %s" % query, labels)
    return None if i is None else "https://archive.org/details/" + results[i]["id"]


def _row_label(f, chosen):
    return "[%s] %s (%s)" % ("x" if f["name"] in chosen else " ", f.get("title") or f["name"], human(f["size"]))


def tui_checklist(win, title, files, chosen=None):
    """Return the list of chosen files, ("open", file) to browse a zip, or None to go back.
    Enter/Space = toggle, d (X on the gamepad) = download, a (Y) = all, o (Start) = open a zip."""
    import curses
    chosen = set() if chosen is None else chosen
    flt = ""
    cur = top = tick = 0
    note = ""
    while True:
        view = [f for f in files if flt.lower() in f["name"].lower() or flt.lower() in f.get("title", "").lower()]
        n = max(len(view), 1)
        cur = min(cur, n - 1)
        total = sum(f["size"] for f in files if f["name"] in chosen)
        _, w = win.getmaxyx()
        head = "%s%s  [%d sel, %s / total %s]" % (title, " /" + flt if flt else "", len(chosen), human(total),
                                              human(sum(f["size"] for f in files)))
        _frame(win, head, note or "B=toggle  X=download  Y=all  Start=open zip  A=back")
        note = ""
        h, _w = win.getmaxyx()
        rows = h - 2
        if cur < top:
            top = cur
        elif cur >= top + rows:
            top = cur - rows + 1
        for r in range(rows):
            i = top + r
            if i >= len(view):
                break
            f = view[i]
            label = _row_label(f, chosen)
            if i == cur:
                label = marquee(label, w - 3, tick)
            _put(win, 1 + r, 0, ("> " if i == cur else "  ") + label,
                 curses.A_REVERSE if i == cur else 0)
        win.refresh()
        k = _getch(win, bool(view) and len(_row_label(view[cur], chosen)) > w - 3)
        if k == -1:
            tick += 1
            continue
        tick = 0
        if k in (10, 13, curses.KEY_ENTER, ord(" ")):
            if view:
                chosen ^= {view[cur]["name"]}
        elif k in (ord("d"), ord("D")):
            if chosen:
                return [f for f in files if f["name"] in chosen]
            note = "No file selected"
        elif k in (ord("o"), ord("O")):
            if view and view[cur]["name"].lower().endswith(".zip") and not view[cur].get("url"):
                return ("open", view[cur])
            note = "Not a zip of this item"
        elif k in (ord("a"), ord("A")):
            names = {f["name"] for f in view}
            chosen = chosen - names if names <= chosen else chosen | names
        elif k == ord("/"):
            new = tui_text(win, "Filter: ")
            if new is not None:
                flt, cur, top = new, 0, 0
        elif _is_back(k):
            if flt:
                flt, cur, top = "", 0, 0
            else:
                return None
        else:
            cur = _move(k, cur, n, rows)


BIG_ZIP = 1 << 30  # a single .zip at least this big (and most of the item) is browsed instead of listed


def dominant_zip(files):
    """The one huge .zip that makes up most of the item (e.g. a 19 GB romset), or None."""
    zips = [f for f in files if f["name"].lower().endswith(".zip") and "/" not in f["name"]]
    total = sum(f["size"] for f in files)
    big = max(zips, key=lambda f: f["size"], default=None)
    if big and big["size"] >= BIG_ZIP and big["size"] >= total * 0.9:
        return big
    return None


def tui_zip_members(win, identifier, zipfile_entry, files=()):
    """Browse the files inside a zip of the item. Return the chosen members, or None to go back.
    Romset names (e.g. 1941.zip) get their full title from the item's .dat, when it has one."""
    tui_message(win, "Archive Downloader", ["Reading zip index..."], wait=False)
    try:
        members = list_zip_members(identifier, zipfile_entry["name"])
    except Error as e:
        tui_message(win, "Error", [str(e)])
        return None
    dat = find_dat(files)
    if dat:
        tui_message(win, "Archive Downloader", ["Reading game names...", "(first time only: %s)" % human(dat["size"])],
                    wait=False)
        titles = load_titles(identifier, files)
        for m in members:
            t = titles.get(os.path.splitext(os.path.basename(m["name"]))[0].lower())
            if t:
                m["title"] = t
    return tui_checklist(win, "Zip: %s" % zipfile_entry["name"], members)


def tui_update(win):
    """Update the tool from the repository. True = updated (restart needed)."""
    tui_message(win, "Update", ["Checking the repository..."], wait=False)
    try:
        changed = check_update()
        if not changed:
            tui_message(win, "Update", ["Already up to date (v%s)." % VERSION])
            return False
        latest = next((version_of(d) for n, d in changed if n == "archive_dl.py"), None) or VERSION
        label = "Update now (%s)" % ", ".join(n for n, _d in changed)
        if tui_menu(win, "Update available: v%s -> v%s" % (VERSION, latest), [label, "Cancel"]) != 0:
            return False
        apply_update(changed)
    except Error as e:
        tui_message(win, "Error", [str(e)])
        return False
    tui_message(win, "Update", ["Updated.", "Quit and relaunch the tool to use the new version."])
    return True


def tui_confirm(win, entries, system, systems, sd):
    """Summary before downloading. True = proceed, False = cancel."""
    import curses
    total = sum(e["size"] for e in entries)
    try:
        dest, _exts = resolve_rom_dir(system, systems, sd)
        probe = dest
        while not os.path.exists(probe) and probe != os.path.dirname(probe):
            probe = os.path.dirname(probe)
        free = human(shutil.disk_usage(probe).free)
    except (Error, OSError):
        dest, free = "?", "?"
    lines = ["File: %d" % len(entries), "Total size: %s" % human(total),
             "Free space: %s" % free, "System: %s" % system, "Folder: %s" % dest]
    _frame(win, "Confirm download", "B=download  A=cancel")
    for i, line in enumerate(lines):
        _put(win, 2 + i, 1, line)
    win.refresh()
    while True:
        k = win.getch()
        if k in (10, 13, curses.KEY_ENTER, ord("d"), ord("D")):
            return True
        if _is_back(k):
            return False


def tui_download(win, identifier, entries, system, systems, sd):
    import curses

    grand = sum(e["size"] for e in entries)
    st = {"label": None, "t0": 0.0, "d0": 0, "draw": 0.0}

    def hook(pct, label, done=0, total=0):
        now = time.time()
        win.nodelay(True)  # poll the keyboard: A / Esc cancels (the .part file is kept)
        try:
            while True:
                k = win.getch()
                if k == -1:
                    break
                if _is_back(k):
                    raise Cancelled()
        finally:
            win.nodelay(False)
        if label != st["label"]:
            st.update(label=label, t0=now, d0=done, draw=0.0)
        if now - st["draw"] < 0.25 and pct < 100:
            return
        st["draw"] = now
        h, w = win.getmaxyx()
        _frame(win, "Download", "A=cancel (resumable)")
        _put(win, 2, 1, label)
        _put(win, 3, 1, "File: %s / %s   Total: %s" % (human(done), human(total), human(grand)))
        speed = (done - st["d0"]) / (now - st["t0"]) if now - st["t0"] >= 1 else 0
        if speed > 0:
            eta = int((total - done) / speed) if total > done else 0
            _put(win, 5, 1, "Speed: %s/s   ETA: %d:%02d:%02d" % (human(speed), eta // 3600, eta % 3600 // 60, eta % 60))
        bar = max(10, w - 10)
        fill = bar * pct // 100
        _put(win, 4, 1, "[" + "#" * fill + "-" * (bar - fill) + "] %3d%%" % pct)
        win.refresh()

    try:
        hook(0, "Starting...")
        dest, errors = install_entries(identifier, entries, system, systems, sd, hook=hook)
    except Cancelled:
        tui_message(win, "Cancelled", ["Download stopped.", "Partial files (.part) are kept: run it again to resume."])
        return False
    except Error as e:
        tui_message(win, "Error", [str(e)])
        return False
    lines = ["Folder: " + dest] + ["ERROR: " + e for e in errors[:6]]
    if not errors:
        lines.insert(0, "Completed (%d files)." % len(entries))
    tui_message(win, "Result", lines)
    return len(errors) < len(entries)


DISCLAIMER = [
    "WARNING",
    "",
    "This tool downloads files from archive.org.",
    "Use it only for content you have the rights to",
    "(e.g. backups of games you own) or that is",
    "public domain / homebrew.",
    "",
    "You are solely responsible for what you download.",
]


def tui_disclaimer(win):
    """Initial notice. True = accepted, False = declined."""
    import curses
    _frame(win, "Disclaimer", "B=accept  A=quit")
    h, _w = win.getmaxyx()
    for i, line in enumerate(DISCLAIMER[:h - 3]):
        _put(win, 2 + i, 1, line, curses.A_BOLD if i == 0 else 0)
    win.refresh()
    while True:
        k = win.getch()
        if k in (10, 13, curses.KEY_ENTER):
            return True
        if _is_back(k):
            return False


def _tui_main(win, args):
    import curses
    curses.curs_set(0)
    win.keypad(True)
    if not tui_disclaimer(win):
        return 0
    try:
        sources = sorted(load_sources(args.conf), key=lambda s: (s[0].lower(), s[1].lower()))
    except Error:
        sources = []
    systems = load_systems(args.es_cfg)
    installed = False
    while True:
        labels = ["[%s] %s" % (s, n) for s, n, _u in sources] + ["Search archive.org...", "Other URL...", "Update tool..."]
        i = tui_menu(win, "Archive Downloader v%s - source" % VERSION, labels, "Up/Down=move  B=choose  A=quit" if GAMEPAD else "Up/Down  Enter=choose  Esc=quit")
        if i is None:
            return EXIT_INSTALLED if installed else 0
        if i < len(sources):
            system, url = sources[i][0], sources[i][2]
        elif i == len(sources) + 2:
            if tui_update(win):
                return EXIT_INSTALLED if installed else 0
            continue
        else:
            url = tui_search(win) if i == len(sources) else tui_ask_url(win)
            if not url:
                continue
            names = sorted(systems)
            j = tui_menu(win, "Destination system", ["(deduce from extension)"] + names)
            if j is None:
                continue
            system = names[j - 1] if j else None
        tui_message(win, "Archive Downloader", ["Reading file list..."], wait=False)
        try:
            identifier, files = fetch_files(url)
        except Error as e:
            tui_message(win, "Error", [str(e)])
            continue
        chosen = set()
        auto = dominant_zip(files)  # one giant romset: its index is the file list
        while True:
            if auto:
                entries = tui_zip_members(win, identifier, auto, files)
                if not entries:
                    break  # back: to the source list, not to the one giant file
            else:
                entries = tui_checklist(win, "File (%s)" % (system or "auto"), files, chosen)
                if not entries:
                    break
                if isinstance(entries, tuple):
                    entries = tui_zip_members(win, identifier, entries[1], files)
                    if not entries:
                        continue
            try:
                target = system or deduce_system(entries)
            except Error as e:
                tui_message(win, "Error", [str(e)])
                break
            if tui_confirm(win, entries, target, systems, args.sd):
                installed |= tui_download(win, identifier, entries, target, systems, args.sd)
                break


def run_tui(args):
    import curses
    import locale
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    global GAMEPAD
    GAMEPAD = args.pad
    return curses.wrapper(_tui_main, args)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def resolve_url(args):
    if args.source:
        sources = [s for s in load_sources(args.conf) if s[0] == args.source]
        if not sources:
            raise Error("No source for system '%s' in %s" % (args.source, args.conf))
        if len(sources) == 1:
            return sources[0][2], args.source
        print("Sources for %s:" % args.source)
        for i, (_s, name, url) in enumerate(sources, 1):
            print("%3d) %s  %s" % (i, name, url))
        return sources[int(input("Choose: ")) - 1][2], args.source
    if not args.url:
        raise Error("Specify a URL or --source")
    return args.url, args.system


def fetch_files(url):
    identifier, single = parse_url(url)
    files = list_files(identifier)
    if single:
        files = [f for f in files if f["name"] == single] or \
                [{"name": single, "size": 0, "md5": ""}]
    return identifier, files


def deduce_system(entries):
    guessed = {EXT_TO_SYSTEM.get(os.path.splitext(f["name"])[1].lower()) for f in entries}
    guessed.discard(None)
    if len(guessed) != 1:
        raise Error("Cannot deduce the system: use --system or --source")
    return guessed.pop()


def pick_entries(files, args):
    if args.files_from:
        with open(args.files_from, encoding="utf-8") as fh:
            wanted = {l.rstrip("\n") for l in fh if l.strip()}
        return [f for f in files if f["name"] in wanted]
    if args.all:
        return files
    if args.match:
        rx = re.compile(args.match, re.I)
        return [f for f in files if rx.search(f["name"])]
    return select_interactive(files)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd_or_url", nargs="?", help="archive.org URL, or sources/files/get (console use)")
    p.add_argument("url2", nargs="?", help=argparse.SUPPRESS)
    p.add_argument("--source", help="system defined in archive_sources.conf")
    p.add_argument("--list-sources", action="store_true")
    p.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    p.add_argument("--update", action="store_true", help="update the tool from the GitHub repository")
    p.add_argument("--search", metavar="QUERY", help="search archive.org software items and print the results")
    p.add_argument("--system", help="destination system (name in es_systems.cfg)")
    p.add_argument("--sd", help="SD card mount point (PC use)")
    p.add_argument("--es-cfg", default=DEFAULT_ES_CFG)
    p.add_argument("--conf", default=DEFAULT_CONF)
    p.add_argument("--gamelist", action="store_true",
                   help="add covers/descriptions of the installed ports to gamelist.xml (--system, default ports)")
    p.add_argument("--zip", metavar="FILE", help="work on the files inside this .zip of the item")
    p.add_argument("--match", help="regex on the file name")
    p.add_argument("--all", action="store_true")
    p.add_argument("--files-from", help="file with one name per line (selection already made)")
    p.add_argument("--tui", action="store_true", help="full-screen interface (curses)")
    p.add_argument("--pad", action="store_true", help="show gamepad buttons in the TUI hints (console launcher)")
    p.add_argument("--gauge", action="store_true", help="output for dialog --gauge")
    args = p.parse_args(argv)

    cmd = args.cmd_or_url if args.cmd_or_url in ("sources", "files", "get") else None
    args.url = args.url2 if cmd else args.cmd_or_url

    try:
        if args.tui:
            return run_tui(args)
        if args.update:
            changed = check_update()
            if not changed:
                print("Already up to date.")
                return 0
            latest = next((version_of(d) for n, d in changed if n == "archive_dl.py"), None)
            apply_update(changed)
            print("Updated%s: %s" % (" to v" + latest if latest else "", ", ".join(n for n, _d in changed)))
            return 0
        if args.gamelist:
            dest, _exts = resolve_rom_dir(args.system or "ports", load_systems(args.es_cfg), args.sd)
            print("Updated %d game(s) in %s" % (update_gamelist(dest), dest))
            return 0
        if args.search:
            for r in search_items(args.search):
                print("%s\t%s\t%s" % (r["id"], human(r["size"]), r["title"]))
            return 0
        if args.list_sources or cmd == "sources":
            for sec, name, url in load_sources(args.conf):
                print("%s\t%s\t%s" % (sec, name, url))
            return 0
        url, system = resolve_url(args)
        identifier, files = fetch_files(url)
        if args.zip:
            files = list_zip_members(identifier, args.zip)
        if cmd == "files":
            for f in files:
                print("%s\t%d\t%s" % (f["name"], f["size"], f["md5"]))
            return 0

        entries = pick_entries(files, args)
        if not entries:
            print("No file selected.")
            return 0
        if not system:
            system = deduce_system(entries)
        systems = load_systems(args.es_cfg)
        dest, errors = install_entries(identifier, entries, system, systems, args.sd, args.gauge)
        if not args.gauge:
            print("Folder: %s" % dest)
        return 1 if errors else 0
    except Error as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nInterrupted (partial downloads stay as .part and resume on relaunch).")
        return 130


if __name__ == "__main__":
    sys.exit(main())
