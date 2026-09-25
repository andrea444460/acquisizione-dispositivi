# Acquisizione dati da dispositivi GESTUS via USB

Script interattivo che guida il collegamento **uno alla volta** di 9 dispositivi Gestus, copia i file (solo le estensioni indicate) e li organizza così:

```
dati/
  2026-08-13_11-53-53/
    piede_destro/
    piede_sinistro/
    gamba_destra/
    …
```

La cartella `giorno_ora` **non** usa l’orologio del PC: viene letta dal nome file, ad esempio

`B24354_rec_20260813115353148` → **13 agosto 2026, 11:53:53.148** → cartella `2026-08-13_11-53-53`.

I 9 dispositivi finiscono nella **stessa** sessione (data/ora del primo dispositivo copiato).

## Avvio

Serve Python 3. Doppio clic su `acquisisci.bat`, oppure da terminale:

```bat
py -3 acquisisci.py
```


## Configurazione (`config.json`)

Modifica questo file e riavvia: non serve toccare lo script.

| Campo | Significato |
| --- | --- |
| `extensions` | Estensioni da copiare, es. `[ ".dat"]`. Lista vuota = nessun file copiato. |
| `filename_pattern` | Regex sul nome (senza estensione). Il primo gruppo deve essere il timestamp. Default: `_rec_(\d{17})`. |
| `devices` | Elenco in ordine di acquisizione: `label` a schermo, `folder` nome cartella. |
| `output_dir` | Dove salvare (relativo al progetto o assoluto). |
| `source_subdir` | Sottocartella sul disco USB; vuoto = radice. |
| `wait_timeout_sec` | Secondi di attesa per una nuova unità USB. |
| `gestus_split` | Script da eseguire dopo ogni copia sui file `.dat` (default: `C:\Users\Andrea\Desktop\gestus_split.py`). |
| `rtklib_dir` | Cartella RTKLIB con `convbin.exe` (default: `C:\Program Files (x86)\RTKLIB_EX_2.5.0\RTKLIB_EX_2.5.0`). |
| `convbin_format` | Formato binario GNSS per convbin (default: `unicore`). |
| `rinex_version` | Versione RINEX in uscita (default: `3.04`). |

Dopo la copia di un `.dat`:

1. `gestus_split.py` genera `_header.txt`, `_gnss.bin` e `_imu.bin`
2. `convbin -r unicore` converte `_gnss.bin` in RINEX (`.obs` e `.nav`) nella stessa cartella

### Estensioni

```json
"extensions": [".csv", ".bin", ".dat"]
```

### Nome file e data/ora

Formato previsto: `PREFISSO_rec_YYYYMMDDHHMMSSmmm`.

Esempio: `B24354_rec_20260813115353148`

- 17 cifre = data, ora, millisecondi
- la cartella sessione usa data e ora **al secondo** (senza ms)

Se su una USB ci sono più registrazioni (timestamp diversi), lo script le elenca e chiede quali copiare (`1` oppure `1,2`). Con più scelte, tutti i file finiscono nella cartella sessione del timestamp **più vecchio**.

## Uso

1. Avvia lo script.
2. Collega il dispositivo indicato (es. *Gamba destra (4/9)*), premi Invio.
3. Se ci sono più registrazioni, scegli i numeri (`1` oppure `1,2`).
4. Controlla il riepilogo della copia, scollega, Invio.
5. Ripeti per gli altri dispositivi. `s` salta, `q` esce.

I file sul dispositivo **non** vengono cancellati.
