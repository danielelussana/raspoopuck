# raspoopuck

*[English](#english) | [Italiano](#italiano)*

---

## English

Automatic MPD music control driven by presence detection, with dedicated
handling for spaces that need discretion (e.g. a shared bathroom): music
starts when someone walks in, fades and stops when they leave, and a BLE
remote (Puck.js) lets you force a quick volume boost or a quiet mode
on the fly without touching a phone.

### How it works

- `mpdmotion.py` is the main process: it reads the configuration, selects
  the presence detector, drives MPD volume/play/pause via `mpc`, and
  starts/manages the Puck.js bridge as a subprocess.
- `puckberry_bridge.py` listens for the Puck.js BLE advertising packets
  (no GATT connection: the Puck broadcasts commands as manufacturer data)
  and writes the requested state (`boost`/`quiet`) to a temporary file
  that `mpdmotion.py` reads on every cycle.
- `mpdmotion_presence.json` holds the configuration (GPIO pins,
  thresholds, MPD parameters, Puck parameters).

### Hardware requirements

- Raspberry Pi (or compatible SBC) with accessible GPIO and Bluetooth LE
  (onboard or USB dongle).
- One or more presence sensors, pick any:
  - **PIR** on GPIO4 (physical pin 7);
  - **RCWL-0516** on GPIO17 (physical pin 11);
  - **ToF VL53L1X** on standard I2C (SDA GPIO2 pin 3, SCL GPIO3 pin 5).
- **Puck.js** (optional but recommended) flashed with firmware that
  transmits commands via non-connectable BLE advertising, manufacturer ID
  `0x0590`, payload `[0x50, 0x4D, seq, cmd]` where `cmd` is:
  - `1` = volume boost (single click)
  - `2` = quiet mode (double click)
  - `3` = next track (triple click)

### Software requirements

- Python 3.11+
- [`gpiozero`](https://pypi.org/project/gpiozero/) — reads the PIR/RCWL digital sensors
- [`bleak`](https://pypi.org/project/bleak/) — BLE scanning for the Puck bridge
- `board` + `adafruit-circuitpython-vl53l1x` — only if using the ToF sensor
- A running, reachable MPD instance, with the `mpc` client installed
- `bluetoothd` (BlueZ) running for BLE scanning

### Configuration

All parameters (pins, enter/exit thresholds, volumes, timeouts, the
Puck's BLE name) are set in `mpdmotion_presence.json`. The default file
used is `mpdmotion_presence.json` in the working directory, or the one
given via `--config` or the `MPDMOTION_CONFIG` environment variable.

### Running it

```
python3 mpdmotion.py --config mpdmotion_presence.json
```

Meant to run as a long-lived systemd service (see `mpdmotion.service` for
an example, not included in this repo).

---

## Italiano

Musica che si adatta alla presenza e sparisce quando serve discrezione:
controllo MPD via sensori di presenza (PIR/ToF/RCWL) e un telecomando BLE
Puck.js, pensato per ambienti che richiedono più privacy (es. bagno) o
meno rumore. La musica parte quando qualcuno entra, si abbassa e si
interrompe quando esce, e il telecomando permette di forzare al volo un
boost di volume o una modalità silenziosa senza dover toccare lo
smartphone.

### Come funziona

- `mpdmotion.py` è il processo principale: legge la configurazione,
  seleziona il rilevatore di presenza, pilota il volume/play/pause di MPD
  via `mpc`, e avvia/gestisce il bridge Puck.js come sottoprocesso.
- `puckberry_bridge.py` ascolta i pacchetti di advertising BLE del Puck.js
  (nessuna connessione GATT: il Puck trasmette i comandi come manufacturer
  data in broadcast) e scrive lo stato richiesto (`boost`/`quiet`) in un
  file temporaneo che `mpdmotion.py` legge ad ogni ciclo.
- `mpdmotion_presence.json` è la configurazione (pin GPIO, soglie,
  parametri MPD, parametri Puck).

### Requisiti hardware

- Raspberry Pi (o SBC compatibile) con GPIO accessibile e Bluetooth LE
  (onboard o dongle USB).
- Uno o più sensori di presenza, a scelta:
  - **PIR** su GPIO4 (pin fisico 7);
  - **RCWL-0516** su GPIO17 (pin fisico 11);
  - **ToF VL53L1X** su I2C standard (SDA GPIO2 pin 3, SCL GPIO3 pin 5).
- **Puck.js** (opzionale ma consigliato) flashato con un firmware che
  trasmette comandi via advertising BLE non connettibile, manufacturer ID
  `0x0590`, payload `[0x50, 0x4D, seq, cmd]` dove `cmd` è:
  - `1` = boost volume (click singolo)
  - `2` = modalità quiet (doppio click)
  - `3` = traccia successiva (triplo click)

### Requisiti software

- Python 3.11+
- [`gpiozero`](https://pypi.org/project/gpiozero/) — lettura sensori digitali PIR/RCWL
- [`bleak`](https://pypi.org/project/bleak/) — scansione BLE per il bridge Puck
- `board` + `adafruit-circuitpython-vl53l1x` — solo se si usa il sensore ToF
- MPD in esecuzione e raggiungibile, con il client `mpc` installato
- `bluetoothd` attivo (BlueZ) per lo scan BLE

### Configurazione

Tutti i parametri (pin, soglie di ingresso/uscita, volumi, timeout, nome
BLE del Puck) si impostano in `mpdmotion_presence.json`. Il file di default
usato è `mpdmotion_presence.json` nella working directory, oppure quello
indicato con `--config` o dalla variabile d'ambiente `MPDMOTION_CONFIG`.

### Avvio

```
python3 mpdmotion.py --config mpdmotion_presence.json
```

È pensato per girare come servizio systemd di lunga durata (vedi
`mpdmotion.service` per un esempio, non incluso in questo repo).
