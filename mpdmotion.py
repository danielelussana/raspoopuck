#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
mpdmotion_presence.py

Controllo MPD con rilevazione presenza selezionabile da file JSON e integrazione Puck.js:
  - pir      : solo PIR su GPIO4
  - rcwl     : solo RCWL-0516 su GPIO17
  - tof      : solo sensore ToF VL53L1X su I2C
  - tof_pir  : avvio con doppio check ToF + PIR; mantenimento con ToF
  - auto     : sceglie automaticamente in questo ordine: ToF+PIR, ToF, PIR, RCWL

Integrazione Puck.js (comandi via manufacturer data BLE, non GATT/UART):
  - click singolo : PRIVACY_ON, boost volume target 100%
  - doppio click  : QUIET_TOGGLE, quiet mode volume target 25%
  - triplo click  : PREVIOUS_PLAYLIST, prossima traccia (mpc next)

Numerazione GPIO: BCM.
Collegamenti attesi:
  PIR OUT       -> GPIO4  / pin fisico 7
  RCWL OUT      -> GPIO17 / pin fisico 11
  TOF VL53L1X   -> I2C standard: SDA GPIO2 pin 3, SCL GPIO3 pin 5
"""

import argparse
import copy
import json
import os
import signal
import sys
import time
from subprocess import DEVNULL, PIPE, Popen, TimeoutExpired, run
from typing import Any, Optional

from gpiozero import DigitalInputDevice


DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "auto",
    "sleep_secs": 0.2,
    "quiet": False,
    "pins": {
        "pir": 4,
        "rcwl": 17,
    },
    "mpd": {
        "volume_step": 1,
        "volume_min": 0,
        "volume_max": 90,
        "pause_at_zero": True,
        "idle_volume": 0,
        "idle_volume_safety_margin": 20,
    },
    "tof": {
        "min_cm": 25.0,
        "max_cm": 170.0,
        "distance_mode": 1,       # 1=short, 2=long
        "timing_budget_ms": 100,
        "enter_count": 3,
        "exit_count": 150,
    },
    "pir": {
        "enter_count": 8,
        "absence_seconds": 60,
        "pull_up": False,
    },
    "rcwl": {
        "enter_count": 5,
        "absence_seconds": 30,
        "pull_up": False,
    },
    "tof_pir": {
        "pir_enter_count": 5,
        "tof_enter_count": 3,
        "tof_absence_seconds": 60,
    },
    "puck": {
        "enabled": True,
        "bridge_script": "puckberry_bridge.py",
        "state_file": "/tmp/mpdmotion_puck_state.json",
        "device_name": "poopuck",
        "device_address": None,
        "boost_volume": 100,
        "quiet_volume": 25,
        "boost_clear_after_absence_seconds": 90,
        "quiet_clear_after_absence_seconds": 300,
        "manual_volume_step": 5,
    },
    "counter_max": 500,
}


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        cfg = deep_merge(cfg, user_cfg)
    return cfg


def count_from_seconds(seconds: float, sleep_secs: float) -> int:
    return max(1, int(round(seconds / sleep_secs)))


# -----------------------------------------------------------------------------
# Utility
# -----------------------------------------------------------------------------
def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def mpc(*args: str) -> None:
    run(["mpc", *args], check=False, stdout=DEVNULL, stderr=DEVNULL)


def mpd_is_playing() -> bool:
    proc = run(["mpc", "status"], check=False, stdout=PIPE, stderr=DEVNULL, text=True)
    return "[playing]" in (proc.stdout or "")


def yesno(value: bool) -> str:
    return "SI" if value else "no"


def atomic_write_json(path: str, data: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


def safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"Non riesco a rimuovere {path}: {exc}", flush=True)


def read_puck_state(path: str) -> Optional[dict[str, Any]]:
    """
    Legge lo stato temporaneo scritto dal bridge Puck.

    Scelta prudenziale: se il file esiste ma è corrotto, non è JSON,
    non è un oggetto, oppure contiene una modalità non riconosciuta,
    viene eliminato. Così un file sporco in /tmp non lascia lo script
    in uno stato ambiguo.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            print(f"Stato Puck ignorato: contenuto non oggetto JSON ({path}). Lo elimino.", flush=True)
            safe_unlink(path)
            return None

        mode = str(data.get("mode", "")).lower()
        if mode not in {"boost", "quiet"}:
            print(f"Stato Puck ignorato: mode non valida ({mode!r}) in {path}. Lo elimino.", flush=True)
            safe_unlink(path)
            return None

        return data

    except FileNotFoundError:
        return None
    except Exception as exc:
        print(f"Stato Puck non leggibile ({path}): {exc}. Lo elimino.", flush=True)
        safe_unlink(path)
        return None


def puck_state_age_seconds(path: str, state: dict[str, Any]) -> float:
    # Per rispettare la logica richiesta uso soprattutto mtime del file.
    # created_at rimane nel JSON come fallback/diagnostica.
    now = time.time()
    try:
        ts = os.path.getmtime(path)
    except FileNotFoundError:
        return 0.0
    except Exception:
        ts = float(state.get("created_at", now) or now)
    return max(0.0, now - ts)


def update_puck_state_for_presence(puck_cfg: dict[str, Any], presence: bool) -> tuple[Optional[str], float]:
    """
    Ritorna (mode, age_seconds). Se la modalità è scaduta e non c'è presenza,
    elimina il file temporaneo e ritorna (None, age).
    """
    state_file = str(puck_cfg.get("state_file", "/tmp/mpdmotion_puck_state.json"))
    state = read_puck_state(state_file)
    if state is None:
        return None, 0.0

    mode = str(state.get("mode", "")).lower()
    age = puck_state_age_seconds(state_file, state)

    if mode == "boost":
        clear_after = float(puck_cfg.get("boost_clear_after_absence_seconds", 90))
    elif mode == "quiet":
        clear_after = float(puck_cfg.get("quiet_clear_after_absence_seconds", 300))
    else:
        safe_unlink(state_file)
        return None, age

    # Condizione richiesta: cancella lo stato solo quando NON c'è più presenza
    # e il file ha almeno l'età minima prevista.
    if not presence and age >= clear_after:
        safe_unlink(state_file)
        return None, age

    return mode, age


def resolve_bridge_script_path(config_path: str, bridge_script: str) -> str:
    if os.path.isabs(bridge_script):
        return bridge_script

    candidates = []
    if config_path:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(config_path)), bridge_script))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), bridge_script))
    candidates.append(bridge_script)

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def start_puck_bridge(cfg: dict[str, Any], config_path: str, quiet: bool) -> Optional[Popen]:
    puck_cfg = cfg.get("puck", {})
    if not bool(puck_cfg.get("enabled", False)):
        return None

    bridge_script = resolve_bridge_script_path(
        config_path,
        str(puck_cfg.get("bridge_script", "puckberry_bridge.py")),
    )

    if not os.path.exists(bridge_script):
        print(f"Bridge Puck non trovato: {bridge_script}", flush=True)
        return None

    cmd = [
        sys.executable,
        bridge_script,
        "--config",
        config_path,
        "--state-file",
        str(puck_cfg.get("state_file", "/tmp/mpdmotion_puck_state.json")),
    ]

    stdout = DEVNULL if quiet else None
    stderr = DEVNULL if quiet else None

    try:
        proc = Popen(cmd, stdout=stdout, stderr=stderr)
        print(f"Bridge Puck avviato: pid={proc.pid} script={bridge_script}", flush=True)
        return proc
    except Exception as exc:
        print(f"Bridge Puck non avviato: {exc}", flush=True)
        return None


def stop_puck_bridge(proc: Optional[Popen]) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except TimeoutExpired:
        proc.kill()


def step_volume_towards(current: int, target: int, step: int) -> int:
    step = max(1, int(step))
    if current < target:
        return min(target, current + step)
    if current > target:
        return max(target, current - step)
    return current


class PresenceDetector:
    name = "base"

    def update(self) -> tuple[bool, str]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class DigitalCounterDetector(PresenceDetector):
    """Presenza da un singolo sensore digitale, con ingresso e uscita filtrati."""

    def __init__(
        self,
        *,
        name: str,
        pin: int,
        enter_count: int,
        exit_count: int,
        pull_up: Optional[bool],
        counter_max: int,
    ):
        self.name = name
        self.pin = pin
        self.enter_count = int(enter_count)
        self.exit_count = int(exit_count)
        self.counter_max = int(counter_max)
        self.device = DigitalInputDevice(pin, pull_up=pull_up)
        self.hit_count = 0
        self.miss_count = 0
        self.occupied = False

    def update(self) -> tuple[bool, str]:
        active = bool(self.device.is_active)

        if not self.occupied:
            # Fase di ingresso: servono N letture positive consecutive.
            if active:
                self.hit_count = clamp(self.hit_count + 1, 0, self.enter_count)
            else:
                self.hit_count = 0

            self.miss_count = 0

            if self.hit_count >= self.enter_count:
                self.occupied = True
                self.hit_count = self.enter_count
                self.miss_count = 0

        else:
            # Fase già occupata.
            # Non accumuliamo più hit infiniti: restiamo cappati alla soglia.
            if active:
                self.hit_count = self.enter_count
                self.miss_count = 0
            else:
                # Appena il PIR torna basso, l'assenza parte subito.
                self.hit_count = 0
                self.miss_count = clamp(self.miss_count + 1, 0, self.exit_count)

                if self.miss_count >= self.exit_count:
                    self.occupied = False
                    self.hit_count = 0
                    self.miss_count = 0

        raw = int(self.device.value)
        status = (
            f"{self.name}: GPIO{self.pin} "
            f"raw={raw} active={'SI' if active else 'no'} "
            f"hit={self.hit_count:03d}/{self.enter_count:03d} "
            f"miss={self.miss_count:03d}/{self.exit_count:03d} "
            f"presence={'SI' if self.occupied else 'no'}"
        )
        return self.occupied, status

    def close(self) -> None:
        self.device.close()


class ToFDevice:
    """Wrapper minimale per VL53L1X. Importa le librerie solo quando il ToF serve."""

    def __init__(self, cfg: dict[str, Any]):
        import board
        import adafruit_vl53l1x

        self.i2c = board.I2C()
        self.tof = adafruit_vl53l1x.VL53L1X(self.i2c)
        self.tof.distance_mode = int(cfg["distance_mode"])
        self.tof.timing_budget = int(cfg["timing_budget_ms"])
        self.tof.start_ranging()

    def read_cm(self) -> Optional[float]:
        if not self.tof.data_ready:
            return None
        distance = self.tof.distance
        self.tof.clear_interrupt()
        if distance is None:
            return None
        return float(distance)

    def close(self) -> None:
        try:
            self.tof.stop_ranging()
        except Exception:
            pass


class ToFCounterDetector(PresenceDetector):
    """Presenza basata su distanza ToF dentro una finestra min/max."""

    name = "tof"

    def __init__(self, *, cfg: dict[str, Any], counter_max: int):
        self.cfg = cfg
        self.min_cm = float(cfg["min_cm"])
        self.max_cm = float(cfg["max_cm"])
        self.enter_count = int(cfg["enter_count"])
        self.exit_count = int(cfg["exit_count"])
        self.counter_max = int(counter_max)
        self.device = ToFDevice(cfg)
        self.hit_count = 0
        self.miss_count = 0
        self.occupied = False
        self.last_distance: Optional[float] = None

    def update(self) -> tuple[bool, str]:
        distance = self.device.read_cm()
        if distance is not None:
            self.last_distance = distance

        in_range = distance is not None and self.min_cm <= distance <= self.max_cm

        if in_range:
            self.hit_count = clamp(self.hit_count + 1, 0, self.counter_max)
            self.miss_count = 0
        else:
            self.hit_count = clamp(self.hit_count - 1, 0, self.counter_max)
            if self.occupied:
                self.miss_count = clamp(self.miss_count + 1, 0, self.counter_max)
            else:
                self.miss_count = 0

        if not self.occupied and self.hit_count >= self.enter_count:
            self.occupied = True
            self.miss_count = 0

        if self.occupied and self.miss_count >= self.exit_count:
            self.occupied = False
            self.hit_count = 0
            self.miss_count = 0

        dist_text = "----" if distance is None else f"{distance:5.1f}"
        detail = (
            f"TOF: dist={dist_text}cm range={yesno(in_range)} "
            f"range_cfg={self.min_cm:.0f}-{self.max_cm:.0f}cm "
            f"hit={self.hit_count:03d}/{self.enter_count:03d} "
            f"miss={self.miss_count:03d}/{self.exit_count:03d} "
            f"presence={yesno(self.occupied)}"
        )
        return self.occupied, detail

    def close(self) -> None:
        self.device.close()


class ToFPlusPIRDetector(PresenceDetector):
    """
    Modalità bagno consigliata:
      - avvio: PIR confermato + ToF in range;
      - mantenimento: resta presente finché il ToF rimane in range;
      - spegnimento: solo dopo assenza ToF stabile.
    """

    name = "tof_pir"

    def __init__(self, *, cfg: dict[str, Any]):
        pins = cfg["pins"]
        tof_cfg = cfg["tof"]
        pir_cfg = cfg["pir"]
        combo_cfg = cfg["tof_pir"]
        sleep_secs = float(cfg["sleep_secs"])

        self.pir_pin = int(pins["pir"])
        self.pir = DigitalInputDevice(self.pir_pin, pull_up=pir_cfg.get("pull_up", False))
        self.tof = ToFDevice(tof_cfg)

        self.tof_min_cm = float(tof_cfg["min_cm"])
        self.tof_max_cm = float(tof_cfg["max_cm"])
        self.pir_enter_count = int(combo_cfg["pir_enter_count"])
        self.tof_enter_count = int(combo_cfg["tof_enter_count"])
        self.tof_exit_count = count_from_seconds(float(combo_cfg["tof_absence_seconds"]), sleep_secs)
        self.counter_max = int(cfg["counter_max"])

        self.pir_hit_count = 0
        self.tof_hit_count = 0
        self.tof_miss_count = 0
        self.occupied = False

    def update(self) -> tuple[bool, str]:
        pir_active = bool(self.pir.is_active)
        distance = self.tof.read_cm()
        tof_in_range = distance is not None and self.tof_min_cm <= distance <= self.tof_max_cm

        if pir_active:
            self.pir_hit_count = clamp(self.pir_hit_count + 1, 0, self.counter_max)
        else:
            self.pir_hit_count = clamp(self.pir_hit_count - 1, 0, self.counter_max)

        if tof_in_range:
            self.tof_hit_count = clamp(self.tof_hit_count + 1, 0, self.counter_max)
            self.tof_miss_count = 0
        else:
            self.tof_hit_count = clamp(self.tof_hit_count - 1, 0, self.counter_max)
            if self.occupied:
                self.tof_miss_count = clamp(self.tof_miss_count + 1, 0, self.counter_max)
            else:
                self.tof_miss_count = 0

        pir_confirmed = self.pir_hit_count >= self.pir_enter_count
        tof_confirmed = self.tof_hit_count >= self.tof_enter_count

        if not self.occupied and pir_confirmed and tof_confirmed:
            self.occupied = True
            self.tof_miss_count = 0

        # Una volta acceso, il PIR non spegne la presenza.
        # Spegniamo solo quando il ToF non vede più presenza per abbastanza tempo.
        if self.occupied and self.tof_miss_count >= self.tof_exit_count:
            self.occupied = False
            self.pir_hit_count = 0
            self.tof_hit_count = 0
            self.tof_miss_count = 0

        dist_text = "----" if distance is None else f"{distance:5.1f}"
        detail = (
            f"TOF+PIR: pir_gpio=GPIO{self.pir_pin} pir_raw={int(pir_active)} "
            f"pir_hit={self.pir_hit_count:03d}/{self.pir_enter_count:03d} "
            f"tof_dist={dist_text}cm tof_range={yesno(tof_in_range)} "
            f"tof_hit={self.tof_hit_count:03d}/{self.tof_enter_count:03d} "
            f"tof_miss={self.tof_miss_count:03d}/{self.tof_exit_count:03d} "
            f"presence={yesno(self.occupied)}"
        )
        return self.occupied, detail

    def close(self) -> None:
        self.pir.close()
        self.tof.close()


# -----------------------------------------------------------------------------
# Autodetect / build detector
# -----------------------------------------------------------------------------
def can_init_tof(cfg: dict[str, Any]) -> bool:
    dev = None
    try:
        dev = ToFDevice(cfg["tof"])
        return True
    except Exception as exc:
        print(f"TOF non disponibile: {exc}", flush=True)
        return False
    finally:
        if dev is not None:
            dev.close()


def can_init_gpio(pin: int, label: str, pull_up: Optional[bool]) -> bool:
    dev = None
    try:
        dev = DigitalInputDevice(pin, pull_up=pull_up)
        return True
    except Exception as exc:
        print(f"{label} su GPIO{pin} non disponibile: {exc}", flush=True)
        return False
    finally:
        if dev is not None:
            dev.close()


def choose_auto_mode(cfg: dict[str, Any]) -> str:
    pins = cfg["pins"]
    tof_ok = can_init_tof(cfg)
    pir_ok = can_init_gpio(int(pins["pir"]), "PIR", cfg["pir"].get("pull_up", False))
    rcwl_ok = can_init_gpio(int(pins["rcwl"]), "RCWL", cfg["rcwl"].get("pull_up", False))

    if tof_ok and pir_ok:
        return "tof_pir"
    if tof_ok:
        return "tof"
    if pir_ok:
        return "pir"
    if rcwl_ok:
        return "rcwl"
    raise RuntimeError("Nessun sensore inizializzabile: né ToF, né PIR, né RCWL.")


def build_detector(mode: str, cfg: dict[str, Any]) -> PresenceDetector:
    pins = cfg["pins"]
    sleep_secs = float(cfg["sleep_secs"])
    counter_max = int(cfg["counter_max"])

    if mode == "auto":
        selected = choose_auto_mode(cfg)
        print(f"Modalità auto: selezionata {selected}", flush=True)
        mode = selected

    if mode == "pir":
        exit_count = count_from_seconds(float(cfg["pir"]["absence_seconds"]), sleep_secs)
        return DigitalCounterDetector(
            name="PIR",
            pin=int(pins["pir"]),
            enter_count=int(cfg["pir"]["enter_count"]),
            exit_count=exit_count,
            pull_up=cfg["pir"].get("pull_up", False),
            counter_max=max(counter_max, exit_count),
        )

    if mode == "rcwl":
        exit_count = count_from_seconds(float(cfg["rcwl"]["absence_seconds"]), sleep_secs)
        return DigitalCounterDetector(
            name="RCWL",
            pin=int(pins["rcwl"]),
            enter_count=int(cfg["rcwl"]["enter_count"]),
            exit_count=exit_count,
            pull_up=cfg["rcwl"].get("pull_up", False),
            counter_max=max(counter_max, exit_count),
        )

    if mode == "tof":
        return ToFCounterDetector(cfg=cfg["tof"], counter_max=counter_max)

    if mode == "tof_pir":
        return ToFPlusPIRDetector(cfg=cfg)

    raise ValueError(f"Modalità sconosciuta: {mode}")


# -----------------------------------------------------------------------------
# CLI / Main
# -----------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Avvia MPD/fade volume in base alla presenza rilevata da PIR, RCWL, ToF o ToF+PIR."
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("MPDMOTION_CONFIG", "mpdmotion_presence.json"),
        help="File JSON di configurazione. Default: mpdmotion_presence.json oppure variabile MPDMOTION_CONFIG.",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "pir", "rcwl", "tof", "tof_pir"],
        default=None,
        help="Override della modalità sensore definita nel JSON.",
    )
    parser.add_argument("--quiet", action="store_true", help="Override: riduce l'output diagnostico.")
    parser.add_argument("--tof-min", type=float, default=None, help="Override: distanza minima ToF in cm.")
    parser.add_argument("--tof-max", type=float, default=None, help="Override: distanza massima ToF in cm.")
    parser.add_argument("--pir-enter-count", type=int, default=None, help="Override: letture PIR positive per presenza.")
    parser.add_argument("--pir-absence-seconds", type=float, default=None, help="Override: secondi PIR assente prima di spegnere.")
    return parser.parse_args()


def apply_overrides(cfg: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    cfg = copy.deepcopy(cfg)
    if args.mode is not None:
        cfg["mode"] = args.mode
    if args.quiet:
        cfg["quiet"] = True
    if args.tof_min is not None:
        cfg["tof"]["min_cm"] = args.tof_min
    if args.tof_max is not None:
        cfg["tof"]["max_cm"] = args.tof_max
    if args.pir_enter_count is not None:
        cfg["pir"]["enter_count"] = args.pir_enter_count
    if args.pir_absence_seconds is not None:
        cfg["pir"]["absence_seconds"] = args.pir_absence_seconds
    return cfg


def main() -> None:
    args = parse_args()
    cfg = apply_overrides(load_config(args.config), args)

    mode = str(cfg["mode"])
    detector = build_detector(mode, cfg)

    mpd_cfg = cfg["mpd"]
    volume_step = int(mpd_cfg["volume_step"])
    volume_min = int(mpd_cfg["volume_min"])
    volume_max = int(mpd_cfg["volume_max"])
    pause_at_zero = bool(mpd_cfg.get("pause_at_zero", True))
    idle_volume_raw = int(mpd_cfg.get("idle_volume", 0))
    idle_volume_safety_margin = int(mpd_cfg.get("idle_volume_safety_margin", 20))
    if idle_volume_raw > volume_max:
        idle_volume = clamp(volume_max - idle_volume_safety_margin, volume_min, volume_max)
    else:
        idle_volume = clamp(idle_volume_raw, volume_min, volume_max)
    sleep_secs = float(cfg["sleep_secs"])
    quiet = bool(cfg.get("quiet", False))
    puck_cfg = cfg.get("puck", {})
    boost_volume = clamp(int(puck_cfg.get("boost_volume", 100)), volume_min, 100)
    quiet_volume = clamp(int(puck_cfg.get("quiet_volume", 25)), volume_min, 100)
    manual_volume_step = int(puck_cfg.get("manual_volume_step", max(volume_step, 5)))

    volume = volume_min
    last_presence: Optional[bool] = None
    last_puck_mode: Optional[str] = None
    puck_bridge_proc: Optional[Popen] = None

    print("Avvio mpdmotion presence configurable", flush=True)
    print(f"Config: {args.config}", flush=True)
    print(f"Modalità richiesta: {mode}; modalità effettiva: {detector.name}", flush=True)
    print(
        f"PIR GPIO: {cfg['pins']['pir']}; RCWL GPIO: {cfg['pins']['rcwl']}; "
        f"ToF range: {float(cfg['tof']['min_cm']):.1f}-{float(cfg['tof']['max_cm']):.1f} cm",
        flush=True,
    )
    print("CTRL+C per uscire", flush=True)

    puck_bridge_proc = start_puck_bridge(cfg, args.config, quiet)

    mpc("pause")
    mpc("volume", str(volume_min))

    def handle_sigterm(signum, frame):
        mpc("pause")
        try:
            stop_puck_bridge(puck_bridge_proc)
            detector.close()
        finally:
            sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    try:
        while True:
            presence, detail = detector.update()

            puck_mode, puck_age = update_puck_state_for_presence(puck_cfg, presence)

            if presence:
                if not mpd_is_playing():
                    mpc("play")

                if puck_mode == "boost":
                    target_volume = boost_volume
                    step = manual_volume_step
                elif puck_mode == "quiet":
                    target_volume = quiet_volume
                    step = manual_volume_step
                else:
                    target_volume = volume_max
                    step = volume_step
            else:
                target_volume = idle_volume if idle_volume > 0 else volume_min
                step = volume_step

            new_volume = step_volume_towards(volume, target_volume, step)
            if new_volume != volume:
                volume = clamp(new_volume, volume_min, 100)
                mpc("volume", str(volume))

            if not presence and idle_volume == 0 and pause_at_zero and volume == volume_min:
                mpc("pause")

            puck_text = "-" if puck_mode is None else f"{puck_mode}:{puck_age:0.0f}s"
            if not quiet:
                print(
                    f"presence={yesno(presence):2s} volume={volume:3d} target={target_volume:3d} "
                    f"puck={puck_text} | {detail}",
                    flush=True,
                )
            elif presence != last_presence or puck_mode != last_puck_mode:
                print(
                    f"presence={yesno(presence):2s} volume={volume:3d} target={target_volume:3d} "
                    f"puck={puck_text} | {detail}",
                    flush=True,
                )

            last_presence = presence
            last_puck_mode = puck_mode

            time.sleep(sleep_secs)

    except KeyboardInterrupt:
        print("\nUscita richiesta dall'utente.", flush=True)
    finally:
        mpc("pause")
        stop_puck_bridge(puck_bridge_proc)
        detector.close()


if __name__ == "__main__":
    main()
