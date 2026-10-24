#!/usr/bin/env python3
"""Scarica singoli file da un item di archive.org e li installa nella cartella ROM del sistema.

Uso su PC (SD montata):
    archive_dl.py --list-sources
    archive_dl.py --tui --sd /media/utente/EASYROMS
    archive_dl.py --source gba --match "advance wars" --sd /media/utente/EASYROMS
    archive_dl.py https://archive.org/download/gba-italian-romset_202201 --system gba --sd /media/utente/EASYROMS

Uso sulla console: lanciato da "Archive Downloader.sh" con i sottocomandi
sources / files / get (output adatto a dialog).
"""
import argparse
import configparser
import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONF = os.path.join(HERE, "archive_sources.conf")
DEFAULT_ES_CFG = "/etc/emulationstation/es_systems.cfg"
USER_AGENT = "archive_dl/1.0 (dArkOS R36S)"
CHUNK = 1024 * 256

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


# --------------------------------------------------------------------------
# archive.org
# --------------------------------------------------------------------------
def http_open(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(req, timeout=30)


def parse_url(url):
    """Ritorna (identifier, file_singolo_o_None)."""
    u = urllib.parse.urlparse(url)
    if "archive.org" not in u.netloc:
        raise Error("L'URL non è di archive.org: %s" % url)
    parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
    if len(parts) < 2 or parts[0] not in ("download", "details"):
        raise Error("URL non riconosciuto (atteso /download/<id> o /details/<id>)")
    single = "/".join(parts[2:]) if parts[0] == "download" and len(parts) > 2 else None
    return parts[1], single


def list_files(identifier):
    import json
    try:
        with http_open("https://archive.org/metadata/" + urllib.parse.quote(identifier)) as r:
            data = json.load(r)
    except (urllib.error.URLError, OSError) as e:
        raise Error("Impossibile leggere i metadata: %s" % e)
    if not data.get("files"):
        raise Error("Item vuoto o inesistente: %s" % identifier)
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
    """Scarica in dest (via dest.part) con resume e verifica MD5."""
    url = "https://archive.org/download/%s/%s" % (
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
            if e.code == 416 and have:  # già completo
                break
            raise Error("HTTP %d scaricando %s" % (e.code, entry["name"]))
        except (urllib.error.URLError, OSError) as e:
            raise Error("Errore di rete: %s" % e)
        with r:
            if have and r.status != 206:  # il server ignora Range: riparto da zero
                have = 0
            with open(part, "ab" if have else "wb") as f:
                done = have
                while True:
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
        if total and os.path.getsize(part) < total:
            continue  # connessione interrotta: un secondo tentativo riprende dal .part
        break
    if entry["md5"] and md5_of(part) != entry["md5"]:
        os.remove(part)
        raise Error("MD5 non corrispondente per %s" % entry["name"])
    return part


# --------------------------------------------------------------------------
# Setup / percorsi
# --------------------------------------------------------------------------
def load_sources(conf_path):
    cp = configparser.ConfigParser(delimiters=("=",), interpolation=None,
                                   comment_prefixes=("#", ";"), inline_comment_prefixes=None)
    cp.optionxform = str
    if conf_path == DEFAULT_CONF and not os.path.exists(conf_path):
        conf_path = os.path.join(HERE, "archive_sources.example.conf")  # nessun setup personale
    if not cp.read(conf_path, encoding="utf-8"):
        raise Error("File di setup non trovato: %s" % conf_path)
    return [(sec, name.strip(), url.strip())
            for sec in cp.sections() for name, url in cp.items(sec) if url.strip()]


def load_systems(es_cfg):
    """{nome: (path, [estensioni])} da es_systems.cfg."""
    if not os.path.exists(es_cfg):
        return {}
    systems = {}
    for s in ET.parse(es_cfg).getroot().iter("system"):
        name, path, ext = (s.findtext(t, "").strip() for t in ("name", "path", "extension"))
        if name and path:
            systems[name] = (path.rstrip("/"), [e.lower() for e in ext.split()])
    return systems


def resolve_rom_dir(system, systems, sd=None):
    """Ritorna (cartella_destinazione, estensioni_accettate)."""
    path, exts = systems.get(system, (None, []))
    folder = os.path.basename(path) if path else system
    if sd:
        for cand in (os.path.join(sd, "roms", folder), os.path.join(sd, folder)):
            if os.path.isdir(cand):
                return cand, exts
        base = os.path.join(sd, "roms") if os.path.isdir(os.path.join(sd, "roms")) else sd
        return os.path.join(base, folder), exts
    if not path:
        raise Error("Sistema '%s' non trovato in es_systems.cfg (usa --sd su PC)" % system)
    return path, exts


def install(part, name, dest_dir, exts):
    """Sposta/estrae il file scaricato. Ritorna una descrizione."""
    base = os.path.basename(name)
    ext = os.path.splitext(base)[1].lower()
    final = os.path.join(dest_dir, base)
    if not exts or ext in exts or ext not in ARCHIVE_EXT:
        os.replace(part, final)
        return base
    # archivio non accettato dall'emulatore: estrazione in una cartella temporanea
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
                raise Error("%s non accettato dall'emulatore e 7z non disponibile: lasciato archiviato" % base)
            subprocess.run([tool, "x", "-y", "-o" + tmp, part],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        entries = os.listdir(tmp)
        # più voci in radice (es. giochi ScummVM): cartella col nome dell'archivio
        target = dest_dir if len(entries) == 1 else os.path.join(dest_dir, os.path.splitext(base)[0])
        os.makedirs(target, exist_ok=True)
        for name in entries if len(entries) == 1 else [os.path.join(tmp, n) for n in entries]:
            src = os.path.join(tmp, name) if len(entries) == 1 else name
            dst = os.path.join(target, os.path.basename(src))
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                os.replace(src, dst)
    except (zipfile.BadZipFile, subprocess.CalledProcessError, OSError) as e:
        raise Error("Estrazione fallita per %s: %s" % (base, e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    os.remove(part)
    # ScummVM: ES mostra solo i .scummvm, che contengono il nome della cartella del gioco
    if ".scummvm" in exts:
        games = [os.path.basename(target)] if len(entries) > 1 else \
                [n for n in entries if os.path.isdir(os.path.join(dest_dir, n))]
        for g in games:
            shortcut = os.path.join(dest_dir, g + ".scummvm")
            if not os.path.exists(shortcut):
                with open(shortcut, "w", encoding="utf-8") as fh:
                    fh.write(g + "\n")
    return base + (" (estratto in cartella)" if len(entries) > 1 else " (estratto)")


def install_entries(identifier, entries, system, systems, sd, gauge=False, out=print, hook=None):
    dest_dir, exts = resolve_rom_dir(system, systems, sd)
    os.makedirs(dest_dir, exist_ok=True)
    errors = []
    for i, e in enumerate(entries):
        base = os.path.basename(e["name"])
        final = os.path.join(dest_dir, base)
        label = "[%d/%d] %s" % (i + 1, len(entries), base)
        if e["md5"] and os.path.exists(final) and md5_of(final) == e["md5"]:
            if hook:
                hook((i + 1) * 100 // len(entries), label + " (già presente)", e["size"], e["size"])
            else:
                out("%s: già presente" % label)
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
                print(("\n" if not gauge else "") + "ERRORE: %s" % err, file=sys.stderr, flush=True)
    return dest_dir, errors


# --------------------------------------------------------------------------
# Selezione interattiva (PC)
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
        ans = input("\nNumeri (es. 1-3,7), /testo per filtrare, a=tutti, r=reset, q=esci: ").strip()
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
                print("Selezione non valida: %s" % e)


# --------------------------------------------------------------------------
# TUI (curses): solo frecce/PgUp/PgDn, Invio ed Esc, quindi usabile col gamepad
# --------------------------------------------------------------------------
OSK = "/opt/inttools/osk.py"
EXIT_INSTALLED = 20  # codice di uscita della TUI: qualcosa è stato installato


def _put(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w - 1:
        try:
            win.addstr(y, x, text[:w - 1 - x], attr)
        except Exception:
            pass


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


def tui_menu(win, title, labels, footer="Su/Giù scegli  Invio ok  Esc indietro"):
    import curses
    cur = top = 0
    while True:
        _frame(win, title, footer)
        h, _w = win.getmaxyx()
        rows = h - 2
        if cur < top:
            top = cur
        elif cur >= top + rows:
            top = cur - rows + 1
        for r in range(rows):
            i = top + r
            if i >= len(labels):
                break
            _put(win, 1 + r, 0, ("> " if i == cur else "  ") + labels[i],
                 curses.A_REVERSE if i == cur else 0)
        win.refresh()
        k = win.getch()
        if k in (10, 13, curses.KEY_ENTER):
            return cur
        if _is_back(k):
            return None
        cur = _move(k, cur, len(labels), rows)


def tui_message(win, title, lines, wait=True):
    _frame(win, title, "Premi un tasto" if wait else "")
    h, _w = win.getmaxyx()
    for i, line in enumerate(lines[:h - 3]):
        _put(win, 2 + i, 1, line)
    win.refresh()
    if wait:
        win.getch()


def tui_text(win, prompt):
    """Input testuale da tastiera. Ritorna None se annullato."""
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


def tui_ask_url(win):
    import curses
    if os.path.exists(OSK):  # tastiera su schermo (gamepad)
        curses.def_prog_mode()
        curses.endwin()
        res = subprocess.run([sys.executable, OSK, "URL archive.org", "URL"],
                             stderr=subprocess.PIPE, text=True)
        curses.reset_prog_mode()
        win.clear()
        win.refresh()
        return res.stderr.strip() if res.returncode == 0 else None
    return tui_text(win, "URL: ")


def tui_checklist(win, title, files, chosen=None):
    """Ritorna la lista dei file scelti, oppure None per tornare indietro.
    Invio/Spazio = spunta, d (X sul gamepad) = scarica, a (Y) = tutti."""
    import curses
    chosen = set() if chosen is None else chosen
    flt = ""
    cur = top = 0
    note = ""
    while True:
        view = [f for f in files if flt.lower() in f["name"].lower()]
        n = max(len(view), 1)
        cur = min(cur, n - 1)
        total = sum(f["size"] for f in files if f["name"] in chosen)
        _, w = win.getmaxyx()
        head = "%s%s  [%d sel, %s / tot %s]" % (title, " /" + flt if flt else "", len(chosen), human(total),
                                              human(sum(f["size"] for f in files)))
        _frame(win, head, note or "B=spunta  X=scarica  Y=tutti  A=indietro")
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
            label = "[%s] %s (%s)" % ("x" if f["name"] in chosen else " ", f["name"], human(f["size"]))
            _put(win, 1 + r, 0, ("> " if i == cur else "  ") + label,
                 curses.A_REVERSE if i == cur else 0)
        win.refresh()
        k = win.getch()
        if k in (10, 13, curses.KEY_ENTER, ord(" ")):
            if view:
                chosen ^= {view[cur]["name"]}
        elif k in (ord("d"), ord("D")):
            if chosen:
                return [f for f in files if f["name"] in chosen]
            note = "Nessun file spuntato"
        elif k in (ord("a"), ord("A")):
            names = {f["name"] for f in view}
            chosen = chosen - names if names <= chosen else chosen | names
        elif k == ord("/"):
            new = tui_text(win, "Filtro: ")
            if new is not None:
                flt, cur, top = new, 0, 0
        elif _is_back(k):
            if flt:
                flt, cur, top = "", 0, 0
            else:
                return None
        else:
            cur = _move(k, cur, n, rows)


def tui_confirm(win, entries, system, systems, sd):
    """Riepilogo prima del download. True = procedi, False = annulla."""
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
    lines = ["File: %d" % len(entries), "Dimensione totale: %s" % human(total),
             "Spazio libero: %s" % free, "Sistema: %s" % system, "Cartella: %s" % dest]
    _frame(win, "Conferma download", "B=scarica  A=annulla")
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

    def hook(pct, label, done=0, total=0):
        h, w = win.getmaxyx()
        _frame(win, "Download", "")
        _put(win, 2, 1, label)
        _put(win, 3, 1, "File: %s / %s   Totale: %s" % (human(done), human(total), human(grand)))
        bar = max(10, w - 10)
        fill = bar * pct // 100
        _put(win, 4, 1, "[" + "#" * fill + "-" * (bar - fill) + "] %3d%%" % pct)
        win.refresh()

    try:
        hook(0, "Avvio...")
        dest, errors = install_entries(identifier, entries, system, systems, sd, hook=hook)
    except Error as e:
        tui_message(win, "Errore", [str(e)])
        return False
    lines = ["Cartella: " + dest] + ["ERRORE: " + e for e in errors[:6]]
    if not errors:
        lines.insert(0, "Completato (%d file)." % len(entries))
    tui_message(win, "Risultato", lines)
    return len(errors) < len(entries)


DISCLAIMER = [
    "ATTENZIONE",
    "",
    "Questo strumento scarica file da archive.org.",
    "Usalo solo per contenuti di cui possiedi i diritti",
    "(es. copie di backup di giochi che possiedi) o che",
    "sono di pubblico dominio / homebrew.",
    "",
    "Sei l'unico responsabile di cio' che scarichi.",
]


def tui_disclaimer(win):
    """Avviso iniziale. True = accettato, False = rifiutato."""
    import curses
    _frame(win, "Disclaimer", "B=accetto  A=esci")
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
        sources = load_sources(args.conf)
    except Error:
        sources = []
    systems = load_systems(args.es_cfg)
    installed = False
    while True:
        labels = ["[%s] %s" % (s, n) for s, n, _u in sources] + ["Altro URL..."]
        i = tui_menu(win, "Archive Downloader - sorgente", labels, "Su/Giù  Invio=scegli  Esc=esci")
        if i is None:
            return EXIT_INSTALLED if installed else 0
        if i < len(sources):
            system, url = sources[i][0], sources[i][2]
        else:
            url = tui_ask_url(win)
            if not url:
                continue
            names = sorted(systems)
            j = tui_menu(win, "Sistema di destinazione", ["(deduci dall'estensione)"] + names)
            if j is None:
                continue
            system = names[j - 1] if j else None
        tui_message(win, "Archive Downloader", ["Lettura elenco file..."], wait=False)
        try:
            identifier, files = fetch_files(url)
        except Error as e:
            tui_message(win, "Errore", [str(e)])
            continue
        chosen = set()
        while True:
            entries = tui_checklist(win, "File (%s)" % (system or "auto"), files, chosen)
            if not entries:
                break
            try:
                target = system or deduce_system(entries)
            except Error as e:
                tui_message(win, "Errore", [str(e)])
                break
            if tui_confirm(win, entries, target, systems, args.sd):
                installed |= tui_download(win, identifier, entries, target, systems, args.sd)
                break


def run_tui(args):
    import curses
    import locale
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    return curses.wrapper(_tui_main, args)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def resolve_url(args):
    if args.source:
        sources = [s for s in load_sources(args.conf) if s[0] == args.source]
        if not sources:
            raise Error("Nessuna sorgente per il sistema '%s' in %s" % (args.source, args.conf))
        if len(sources) == 1:
            return sources[0][2], args.source
        print("Sorgenti per %s:" % args.source)
        for i, (_s, name, url) in enumerate(sources, 1):
            print("%3d) %s  %s" % (i, name, url))
        return sources[int(input("Scegli: ")) - 1][2], args.source
    if not args.url:
        raise Error("Specifica un URL o --source")
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
        raise Error("Impossibile dedurre il sistema: usa --system o --source")
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
    p.add_argument("cmd_or_url", nargs="?", help="URL archive.org, oppure sources/files/get (uso console)")
    p.add_argument("url2", nargs="?", help=argparse.SUPPRESS)
    p.add_argument("--source", help="sistema definito in archive_sources.conf")
    p.add_argument("--list-sources", action="store_true")
    p.add_argument("--system", help="sistema di destinazione (nome in es_systems.cfg)")
    p.add_argument("--sd", help="mountpoint della SD (uso su PC)")
    p.add_argument("--es-cfg", default=DEFAULT_ES_CFG)
    p.add_argument("--conf", default=DEFAULT_CONF)
    p.add_argument("--match", help="regex sul nome file")
    p.add_argument("--all", action="store_true")
    p.add_argument("--files-from", help="file con un nome per riga (selezione già fatta)")
    p.add_argument("--tui", action="store_true", help="interfaccia a schermo intero (curses)")
    p.add_argument("--gauge", action="store_true", help="output per dialog --gauge")
    args = p.parse_args(argv)

    cmd = args.cmd_or_url if args.cmd_or_url in ("sources", "files", "get") else None
    args.url = args.url2 if cmd else args.cmd_or_url

    try:
        if args.tui:
            return run_tui(args)
        if args.list_sources or cmd == "sources":
            for sec, name, url in load_sources(args.conf):
                print("%s\t%s\t%s" % (sec, name, url))
            return 0
        url, system = resolve_url(args)
        identifier, files = fetch_files(url)
        if cmd == "files":
            for f in files:
                print("%s\t%d\t%s" % (f["name"], f["size"], f["md5"]))
            return 0

        entries = pick_entries(files, args)
        if not entries:
            print("Nessun file selezionato.")
            return 0
        if not system:
            system = deduce_system(entries)
        systems = load_systems(args.es_cfg)
        dest, errors = install_entries(identifier, entries, system, systems, args.sd, args.gauge)
        if not args.gauge:
            print("Cartella: %s" % dest)
        return 1 if errors else 0
    except Error as e:
        print("ERRORE: %s" % e, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nInterrotto (i download parziali restano come .part e riprendono al rilancio).")
        return 130


if __name__ == "__main__":
    sys.exit(main())
