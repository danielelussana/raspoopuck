# raspoopuck

Controllo automatico della musica MPD in base alla presenza rilevata, con
gestione dedicata per ambienti che richiedono discrezione (es. un bagno
condiviso): la musica parte quando qualcuno entra, si abbassa e si
interrompe quando esce, e un telecomando BLE (Puck.js) permette di forzare
al volo un boost di volume o una modalità silenziosa senza dover toccare
lo smartphone.

## Come funziona

- `mpdmotion.py` è il processo principale: legge la configurazione,
  seleziona il rilevatore di presenza, pilota il volume/play/pause di MPD
  via `mpc`, e avvia/gestisce il bridge Puck.js come sottoprocesso.
- `puckberry_bridge.py` ascolta i pacchetti di advertising BLE del Puck.js
  (nessuna connessione GATT: il Puck trasmette i comandi come manufacturer
  data in broadcast) e scrive lo stato richiesto (`boost`/`quiet`) in un
  file temporaneo che `mpdmotion.py` legge ad ogni ciclo.
- `mpdmotion_presence.json` è la configurazione (pin GPIO, soglie,
  parametri MPD, parametri Puck).

## Requisiti hardware

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

## Requisiti software

- Python 3.11+
- [`gpiozero`](https://pypi.org/project/gpiozero/) — lettura sensori digitali PIR/RCWL
- [`bleak`](https://pypi.org/project/bleak/) — scansione BLE per il bridge Puck
- `board` + `adafruit-circuitpython-vl53l1x` — solo se si usa il sensore ToF
- MPD in esecuzione e raggiungibile, con il client `mpc` installato
- `bluetoothd` attivo (BlueZ) per lo scan BLE

## Configurazione

Tutti i parametri (pin, soglie di ingresso/uscita, volumi, timeout, nome
BLE del Puck) si impostano in `mpdmotion_presence.json`. Il file di default
usato è `mpdmotion_presence.json` nella working directory, oppure quello
indicato con `--config` o dalla variabile d'ambiente `MPDMOTION_CONFIG`.

## Avvio

```
python3 mpdmotion.py --config mpdmotion_presence.json
```

È pensato per girare come servizio systemd di lunga durata (vedi
`mpdmotion.service` per un esempio, non incluso in questo repo).
