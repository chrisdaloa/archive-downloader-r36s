# Archive Downloader - Guida rapida

Scarica singoli file da archive.org e li installa nella cartella ROM del sistema giusto.
Funziona dalla console (con Wi-Fi) e dal PC con la SD inserita.

**Disclaimer:** usalo solo per contenuti di cui possiedi i diritti (backup di giochi che possiedi, pubblico dominio, homebrew). Sei l'unico responsabile di ciò che scarichi. All'avvio il tool mostra un avviso da accettare con **B** (**A** per uscire).

## Dalla console (R36S)

1. Attiva il Wi-Fi.
2. In EmulationStation: **Tools → Archive Downloader** e accetta il disclaimer con **B**.
3. Scegli una sorgente dalla lista (es. `[gba] Romset italiano`), oppure **Altro URL...** per un link tuo.
4. Spunta i file da scaricare e premi **X**.
5. Controlla il riepilogo (numero di file, dimensione totale, spazio libero) e premi **B** per scaricare, **A** per annullare.
6. A fine download esci con **A**: EmulationStation si riavvia e i giochi compaiono.

### Tasti

| Tasto | Azione |
|---|---|
| Su / Giù | muove il cursore |
| Sinistra / Destra | pagina precedente / successiva |
| B | spunta il file (nei menu: conferma) |
| X | scarica i file spuntati |
| Y | spunta / deseleziona tutti |
| A | indietro |
| `/` (tastiera) | filtra per nome |

La riga in alto mostra `[file scelti, dimensione scelta / totale della sezione]`.

### Cosa succede ai file

- Se il sistema accetta il formato (es. `.zip`, `.chd`), il file viene copiato così com'è.
- Altrimenti l'archivio viene estratto; se contiene più file, in una cartella col suo nome.
- ScummVM: viene creato anche il file `<cartella>.scummvm`.
- Il download riprende da dove si è interrotto e viene verificato con l'MD5. I file già presenti vengono saltati.

## Dal PC

```
python3 archive_dl.py --tui --sd /media/$USER/<SD>
python3 archive_dl.py --list-sources
python3 archive_dl.py --source gba --sd /media/$USER/<SD> --match "regex"
python3 archive_dl.py <URL> --system gba --sd /media/$USER/<SD> --all
```

Serve solo Python 3. `--sd` è il punto di mount della SD; la cartella ROM è letta da `es_systems.cfg` (`--es-cfg` per indicarne un altro).

## Aggiungere una sorgente

Modifica `archive_sources.conf` (sulla console: `/opt/system/Tools/`):

```ini
[nes]
Nome visibile = https://archive.org/details/identificativo-item
```

- La sezione è il nome del sistema in `es_systems.cfg` (`gba`, `psx`, `megadrive`, `ports`, `scummvm`...).
- Accettati `/details/<id>`, `/download/<id>` e `/download/<id>/<file>` (file singolo).
- Le righe con `#` sono commenti.
- Verifica con `python3 archive_dl.py --list-sources`.

## Problemi comuni

- **"python3 non è installato"**: il tool richiede Python 3 sulla console.
- **Errore di rete**: controlla il Wi-Fi; riavviando il download riparte dal `.part`.
- **MD5 non corrispondente**: il file viene scartato, riprova.
- **Gioco non visibile**: riavvia EmulationStation o controlla di aver scaricato nel sistema giusto.
- Alcuni item sono enormi (decine di GB): guarda la dimensione nel riepilogo prima di confermare.

## Licenza

MIT, vedi il file `LICENSE`.
