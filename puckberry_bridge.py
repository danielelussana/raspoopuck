#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
puckberry_bridge.py

Bridge BLE tra Puck.js/Poopuck e mpdmotion.py.

Il Puck NON usa una connessione GATT/UART per i comandi: trasmette invece
pacchetti di advertising BLE non connettibili con manufacturer data
(manufacturer ID di default 0x0590, Espruino) nel formato:

    [MAGIC_A, MAGIC_B, seq, cmd]

dove cmd è:
  1 = PRIVACY_ON        -> scrive stato boost nel file temporaneo
  2 = QUIET_TOGGLE       -> scrive stato quiet nel file temporaneo
  3 = PREVIOUS_PLAYLIST  -> mpc next

Il bridge fa quindi solo scansione BLE continua (nessuna connessione), e
deduplica i comandi tramite il numero di sequenza (lo stesso comando viene
ripetuto per ~2s ad ogni pressione del pulsante).

Il bridge NON governa il volume direttamente: scrive solo lo stato.
Il volume resta gestito da mpdmotion.py, che conosce la presenza.
"""

import argparse
import asyncio
import fcntl
import json
import os
import signal
import sys
import time
from subprocess import DEVNULL, run
from typing import Any, Optional

from bleak import BleakScanner

DEFAULT_CONFIG: dict[str, Any] = {
    "puck": {
        "enabled": True,
        "state_file": "/tmp/mpdmotion_puck_state.json",
        "device_name": "poopuck",
        "device_address": None,
        "manufacturer_id": 0x0590,
        "magic_bytes": [0x50, 0x4D],
        "reconnect_delay": 3.0,
        "lock_file": "/tmp/mpdmotion_puck_bridge.lock",
    }
}


def load_config(path: str) -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        if isinstance(user_cfg, dict):
            puck = user_cfg.get("puck", {})
            if isinstance(puck, dict):
                cfg["puck"].update(puck)
    return cfg


def atomic_write_json(path: str, data: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


def write_mode(state_file: str, mode: str, command: str) -> None:
    now = time.time()
    atomic_write_json(
        state_file,
        {
            "mode": mode,
            "command": command,
            "source": "poopuck",
            "created_at": now,
            "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
        },
    )
    print(f"Puck command {command}: mode={mode} scritto in {state_file}", flush=True)


def mpc(*args: str) -> None:
    run(["mpc", *args], check=False, stdout=DEVNULL, stderr=DEVNULL)


def handle_puck_cmd(cmd: int, seq: int, state_file: str) -> None:
    if cmd == 1:
        write_mode(state_file, "boost", f"PRIVACY_ON seq={seq}")
    elif cmd == 2:
        write_mode(state_file, "quiet", f"QUIET_TOGGLE seq={seq}")
    elif cmd == 3:
        print(f"Puck command PREVIOUS_PLAYLIST seq={seq}: mpc next", flush=True)
        mpc("next")
    else:
        print(f"Comando Puck ignorato: cmd={cmd} seq={seq}", flush=True)


def acquire_lock(lock_file: str):
    os.makedirs(os.path.dirname(lock_file) or ".", exist_ok=True)
    fh = open(lock_file, "w", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"Un altro puckberry_bridge è già in esecuzione: {lock_file}", flush=True)
        sys.exit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


async def run_bridge(cfg: dict[str, Any], state_file: str) -> None:
    puck_cfg = cfg.get("puck", {})
    device_name = str(puck_cfg.get("device_name") or "poopuck")
    device_address = puck_cfg.get("device_address")
    device_address = str(device_address).upper() if device_address else None
    manufacturer_id = int(puck_cfg.get("manufacturer_id", 0x0590))
    magic_bytes = bytes(puck_cfg.get("magic_bytes", [0x50, 0x4D]))
    reconnect_delay = float(puck_cfg.get("reconnect_delay", 3.0))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    last_seq: Optional[int] = None

    def detection_callback(device, advertisement_data) -> None:
        nonlocal last_seq

        if device_address:
            if device.address.upper() != device_address:
                return
        else:
            name = device.name or advertisement_data.local_name
            if name != device_name:
                return

        data = advertisement_data.manufacturer_data.get(manufacturer_id)
        if not data or len(data) < 4 or data[0:2] != magic_bytes:
            return

        seq = data[2]
        cmd = data[3]
        if seq == last_seq:
            return
        last_seq = seq

        handle_puck_cmd(cmd, seq, state_file)

    print(f"In ascolto pacchetti advertising Puck (manufacturer_id=0x{manufacturer_id:04x})...", flush=True)

    while not stop_event.is_set():
        try:
            scanner = BleakScanner(detection_callback=detection_callback)
            await scanner.start()
            try:
                await stop_event.wait()
            finally:
                await scanner.stop()
        except Exception as exc:
            print(f"Scanner Puck interrotto: {exc}; riprovo tra {reconnect_delay}s", flush=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=reconnect_delay)
            except asyncio.TimeoutError:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge BLE Puck.js -> mpdmotion.")
    parser.add_argument("--config", default=os.environ.get("MPDMOTION_CONFIG", "mpdmotion_presence.json"))
    parser.add_argument("--state-file", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    puck_cfg = cfg.get("puck", {})
    state_file = args.state_file or str(puck_cfg.get("state_file", "/tmp/mpdmotion_puck_state.json"))
    lock_file = str(puck_cfg.get("lock_file", "/tmp/mpdmotion_puck_bridge.lock"))

    lock_handle = acquire_lock(lock_file)
    print(f"Avvio puckberry_bridge; state_file={state_file}", flush=True)
    try:
        asyncio.run(run_bridge(cfg, state_file))
    finally:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
            lock_handle.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
