#!/usr/bin/env python3
"""Acquisizione interattiva dei dati da dispositivi USB (uno alla volta)."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
UNKNOWN_TS = "__nessun_timestamp__"
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5


@dataclass
class Device:
    id: str
    label: str
    folder: str


@dataclass
class Config:
    output_dir: Path
    source_subdir: str
    extensions: set[str]
    filename_pattern: re.Pattern[str]
    wait_timeout_sec: float
    poll_interval_sec: float
    gestus_split: Path | None
    convbin: Path | None
    convbin_format: str
    rinex_version: str
    devices: list[Device]


@dataclass
class RecStamp:
    digits: str
    dt: datetime
    ms: int

    def folder_name(self) -> str:
        return self.dt.strftime("%Y-%m-%d_%H-%M-%S")

    def display(self) -> str:
        extra = f".{self.ms:03d}" if self.ms else ""
        return self.dt.strftime("%d/%m/%Y %H:%M:%S") + extra


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    extensions = {normalize_ext(x) for x in raw.get("extensions", [])}
    pattern = raw.get("filename_pattern") or r"_rec_(\d{17})"
    output = Path(raw.get("output_dir") or "dati")
    if not output.is_absolute():
        output = SCRIPT_DIR / output
    devices = [
        Device(id=d["id"], label=d["label"], folder=d["folder"])
        for d in raw.get("devices", [])
    ]
    if not devices:
        raise SystemExit("config.json: manca la lista 'devices'.")
    splitter_raw = str(raw.get("gestus_split") or "").strip()
    if splitter_raw:
        splitter = Path(splitter_raw)
        if not splitter.is_absolute():
            splitter = SCRIPT_DIR / splitter
    else:
        splitter = Path(r"C:\Users\Andrea\Desktop\gestus_split.py")
        if not splitter.is_file():
            splitter = SCRIPT_DIR / "gestus_split.py"
    if not splitter.is_file():
        print(f"Attenzione: gestus_split non trovato ({splitter}). Lo split non verra eseguito.")
        splitter = None
    convbin = resolve_convbin(raw)
    convbin_format = str(raw.get("convbin_format") or "unicore").strip().lower()
    rinex_version = str(raw.get("rinex_version") or "3.04").strip()
    return Config(
        output_dir=output,
        source_subdir=str(raw.get("source_subdir") or "").strip().strip("/\\"),
        extensions=extensions,
        filename_pattern=re.compile(pattern),
        wait_timeout_sec=float(raw.get("wait_timeout_sec") or 180),
        poll_interval_sec=float(raw.get("poll_interval_sec") or 1),
        gestus_split=splitter,
        convbin=convbin,
        convbin_format=convbin_format,
        rinex_version=rinex_version,
        devices=devices,
    )


def normalize_ext(value: str) -> str:
    value = str(value).strip().lower()
    if not value:
        return ""
    return value if value.startswith(".") else f".{value}"


def resolve_convbin(raw: dict) -> Path | None:
    convbin_raw = str(raw.get("convbin") or "").strip()
    if convbin_raw:
        path = Path(convbin_raw)
        if not path.is_absolute():
            path = SCRIPT_DIR / path
        if path.is_file():
            return path
        print(f"Attenzione: convbin non trovato ({path}).")
        return None

    rtklib_dir = str(raw.get("rtklib_dir") or "").strip()
    if not rtklib_dir:
        rtklib_dir = r"C:\Program Files (x86)\RTKLIB_EX_2.5.0\RTKLIB_EX_2.5.0"
    path = Path(rtklib_dir) / "convbin.exe"
    if path.is_file():
        return path
    print(f"Attenzione: convbin non trovato in {path.parent}. La conversione RINEX non verra eseguita.")
    return None


def _kernel32():
    import ctypes

    return ctypes.windll.kernel32


def system_root() -> str:
    drive = os.environ.get("SystemDrive", "C:")
    if not drive.endswith("\\"):
        drive += "\\"
    return drive.upper()


def list_local_drives() -> dict[str, int]:
    """Lettere di unita locali (rimovibili o fisse), escluso il disco di sistema."""
    if sys.platform != "win32":
        return {}
    kernel32 = _kernel32()
    bitmask = kernel32.GetLogicalDrives()
    skip = system_root()
    found: dict[str, int] = {}
    for i in range(26):
        if not bitmask & (1 << i):
            continue
        root = f"{chr(ord('A') + i)}:\\"
        if root.upper() == skip:
            continue
        dtype = int(kernel32.GetDriveTypeW(root))
        if dtype in {DRIVE_REMOTE, DRIVE_CDROM}:
            continue
        found[root] = dtype
    return found


def drive_label(root: str) -> str:
    if sys.platform != "win32":
        return ""
    import ctypes

    buf = ctypes.create_unicode_buffer(261)
    ok = _kernel32().GetVolumeInformationW(root, buf, 261, None, None, None, None, 0)
    return buf.value if ok else ""


def format_drive(root: str, dtype: int | None = None) -> str:
    kinds = {
        DRIVE_REMOVABLE: "rimovibile",
        DRIVE_FIXED: "fisso/USB",
        DRIVE_UNKNOWN: "sconosciuto",
    }
    kind = kinds.get(dtype or 0, "disco")
    name = drive_label(root)
    extra = f" '{name}'" if name else ""
    return f"{root} ({kind}{extra})"


def drive_ready(path: Path) -> bool:
    try:
        next(path.iterdir(), None)
        return True
    except OSError:
        return False


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nInterrotto.")
        raise SystemExit(1)


def parse_stamp(stem: str, pattern: re.Pattern[str]) -> RecStamp | None:
    match = pattern.search(stem)
    if not match:
        return None
    digits = match.group(1)
    if len(digits) < 14:
        return None
    try:
        dt = datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    ms = int(digits[14:17]) if len(digits) >= 17 and digits[14:17].isdigit() else 0
    return RecStamp(digits=digits, dt=dt, ms=ms)


def iter_matching_files(root: Path, extensions: set[str]) -> Iterable[Path]:
    if not extensions:
        return
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in extensions:
            yield path


def group_files(
    files: list[Path], pattern: re.Pattern[str]
) -> dict[str, list[tuple[Path, RecStamp | None]]]:
    groups: dict[str, list[tuple[Path, RecStamp | None]]] = defaultdict(list)
    for path in files:
        stamp = parse_stamp(path.stem, pattern)
        key = stamp.digits if stamp else UNKNOWN_TS
        groups[key].append((path, stamp))
    return dict(groups)


def choose_group(
    groups: dict[str, list[tuple[Path, RecStamp | None]]],
) -> list[tuple[Path, RecStamp | None]] | None:
    keys = sorted(groups.keys(), key=lambda k: (k == UNKNOWN_TS, k))
    if len(keys) == 1:
        key = keys[0]
        items = groups[key]
        stamp = items[0][1]
        label = stamp.display() if stamp else "senza data/ora nel nome"
        print(f"Trovata 1 registrazione: {label} ({len(items)} file).")
        return items

    print(f"Trovate {len(keys)} registrazioni:")
    for i, key in enumerate(keys, start=1):
        items = groups[key]
        stamp = items[0][1]
        label = stamp.display() if stamp else "senza data/ora nel nome"
        print(f"  [{i}] {label}  ({len(items)} file)")
        for path, _ in items[:5]:
            print(f"      {path.name}")
        if len(items) > 5:
            print(f"      ... e altri {len(items) - 5}")
    while True:
        choice = ask("Scegli il numero della registrazione da copiare (s = salta): ")
        if choice.lower() in {"s", "skip"}:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(keys):
            return groups[keys[int(choice) - 1]]
        print("Scelta non valida.")


def ask_session_datetime() -> datetime:
    while True:
        raw = ask("Inserisci data e ora della sessione (YYYY-MM-DD HH:MM:SS): ")
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            print("Formato non valido. Esempio: 2026-08-13 11:53:53")


def pick_drive(roots: list[str], types: dict[str, int]) -> Path:
    if len(roots) == 1:
        root = roots[0]
        print(f"Rilevata unita: {format_drive(root, types.get(root))}")
        return Path(root)
    print("Piu unita nuove rilevate:")
    for i, root in enumerate(roots, start=1):
        print(f"  [{i}] {format_drive(root, types.get(root))}")
    while True:
        choice = ask(f"Quale usare? [1-{len(roots)}]: ")
        if choice.isdigit() and 1 <= int(choice) <= len(roots):
            return Path(roots[int(choice) - 1])
        upper = choice.upper().rstrip("\\") + "\\"
        if upper in {r.upper() for r in roots}:
            return Path(upper)
        print("Scelta non valida.")


def wait_for_new_drive(
    before: dict[str, int],
    timeout_sec: float,
    interval_sec: float,
) -> Path | None:
    print("Controllo le unita disco (anche USB visti come dischi fissi)...")
    deadline = time.monotonic() + timeout_sec
    last_print = 0.0
    chosen: Path | None = None
    interval = max(0.3, interval_sec)
    while True:
        now = list_local_drives()
        new = sorted(set(now) - set(before))
        if new and chosen is None:
            chosen = pick_drive(new, now)
        if chosen is not None and drive_ready(chosen):
            print(f"Unita pronta: {chosen}")
            return chosen
        if time.monotonic() >= deadline:
            break
        if time.monotonic() - last_print > 5:
            present = ", ".join(sorted(now)) or "(nessuna oltre C:)"
            print(f"  ancora nessuna unita nuova. Ora presenti: {present}")
            last_print = time.monotonic()
        time.sleep(interval)
    print("Nessuna nuova lettera di unita rispetto a prima del collegamento.")
    now = list_local_drives()
    leftovers = sorted(now)
    if leftovers:
        print("Unita locali disponibili:")
        for root in leftovers:
            mark = " (gia presente)" if root in before else " (nuova)"
            print(f"  - {format_drive(root, now.get(root))}{mark}")
        pick = ask("Lettera da usare (es. E) oppure Invio per annullare: ").strip()
        if pick:
            letter = pick.upper().rstrip(":\\")
            root = letter + ":\\"
            return Path(root)
    return None


def wait_until_unplugged(drive: Path, timeout_sec: float, interval_sec: float) -> None:
    root = str(drive)
    if not root.endswith("\\"):
        root += "\\"
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        present = list_local_drives()
        if root not in present or not drive.exists():
            print("Dispositivo scollegato.")
            return
        time.sleep(interval_sec)
    print("Continuo comunque (il disco risulta ancora presente).")


def copy_files(
    items: list[tuple[Path, RecStamp | None]],
    source_root: Path,
    dest_root: Path,
) -> tuple[int, int, list[Path]]:
    copied = 0
    bytes_total = 0
    dests: list[Path] = []
    overwrite_all = False
    skip_all = False
    for src, _ in items:
        rel = src.relative_to(source_root)
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if skip_all:
                print(f"  saltato (già presente): {rel}")
                continue
            if not overwrite_all:
                ans = ask(
                    f"Esiste già {rel}. Sovrascrivere? [s/N/a=tutti/x=nessuno]: "
                ).lower()
                if ans in {"a", "all"}:
                    overwrite_all = True
                elif ans in {"x"}:
                    skip_all = True
                    print(f"  saltato: {rel}")
                    continue
                elif ans not in {"s", "y", "si", "sì"}:
                    print(f"  saltato: {rel}")
                    continue
        shutil.copy2(src, dest)
        size = dest.stat().st_size
        copied += 1
        bytes_total += size
        dests.append(dest)
        print(f"  copiato: {rel} ({size} byte)")
    return copied, bytes_total, dests


def gnss_bin_for_dat(dat: Path) -> Path:
    return Path(str(dat.with_suffix("")) + "_gnss.bin")


def run_gestus_split(splitter: Path | None, dest_files: list[Path]) -> list[Path]:
    gnss_files: list[Path] = []
    if splitter is None:
        return gnss_files
    dat_files = [p for p in dest_files if p.suffix.lower() == ".dat"]
    if not dat_files:
        return gnss_files
    print(f"Eseguo gestus_split su {len(dat_files)} file .dat...")
    for dat in dat_files:
        print(f"--- {dat.name} ---")
        result = subprocess.run(
            [sys.executable, str(splitter), str(dat)],
            check=False,
        )
        if result.returncode != 0:
            print(f"gestus_split ha restituito codice {result.returncode} per {dat.name}")
            continue
        gnss = gnss_bin_for_dat(dat)
        if gnss.is_file():
            gnss_files.append(gnss)
        else:
            print(f"Attenzione: non trovato {gnss.name} dopo lo split.")
    return gnss_files


def run_convbin(cfg: Config, gnss_files: list[Path]) -> None:
    if cfg.convbin is None or not gnss_files:
        return
    print(f"Eseguo convbin ({cfg.convbin_format}) su {len(gnss_files)} file GNSS...")
    for gnss in gnss_files:
        obs = Path(str(gnss.with_suffix("")) + ".obs")
        nav = Path(str(gnss.with_suffix("")) + ".nav")
        print(f"--- {gnss.name} -> RINEX ---")
        cmd = [
            str(cfg.convbin),
            "-r",
            cfg.convbin_format,
            "-v",
            cfg.rinex_version,
            "-o",
            str(obs),
            "-n",
            str(nav),
            str(gnss),
        ]
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            print(f"convbin ha restituito codice {result.returncode} per {gnss.name}")
            continue
        if obs.is_file():
            print(f"  RINEX OBS: {obs.name}")
        if nav.is_file():
            print(f"  RINEX NAV: {nav.name}")


def run_post_copy_processing(cfg: Config, dest_files: list[Path]) -> None:
    gnss_files = run_gestus_split(cfg.gestus_split, dest_files)
    run_convbin(cfg, gnss_files)


def ensure_session(
    session_dir: Path | None,
    stamp: RecStamp | None,
    devices: list[Device],
    output_dir: Path,
) -> Path:
    if session_dir is not None:
        return session_dir
    if stamp is None:
        dt = ask_session_datetime()
        name = dt.strftime("%Y-%m-%d_%H-%M-%S")
        print(f"Nessun timestamp nel nome file. Cartella sessione: {name}")
    else:
        name = stamp.folder_name()
        print(f"Sessione da nome file: {stamp.display()} -> {name}")
    session_dir = output_dir / name
    session_dir.mkdir(parents=True, exist_ok=True)
    for device in devices:
        (session_dir / device.folder).mkdir(parents=True, exist_ok=True)
    return session_dir


def source_root_for(drive: Path, subdir: str) -> Path:
    root = drive / subdir if subdir else drive
    if not root.exists():
        raise FileNotFoundError(f"Percorso sorgente non trovato: {root}")
    return root


def process_device(
    device: Device,
    index: int,
    total: int,
    cfg: Config,
    session_dir: Path | None,
    from_dir: Path | None,
) -> tuple[Path | None, str, int, int]:
    print()
    print("=" * 60)
    print(f"Collega: {device.label} ({index}/{total})")
    print("=" * 60)

    while True:
        if from_dir is not None:
            action = ask(
                f"Usa la cartella di prova {from_dir} per '{device.label}'? "
                "[Invio = sì, s = salta, q = esci]: "
            ).lower()
            if action in {"q", "quit"}:
                raise SystemExit(0)
            if action in {"s", "skip"}:
                return session_dir, "saltato", 0, 0
            drive = from_dir
        else:
            before = list_local_drives()
            if before:
                print("Unita gia presenti (non collegare queste):")
                for root, dtype in sorted(before.items()):
                    print(f"  - {format_drive(root, dtype)}")
            else:
                print("Nessun altro disco locale oltre a C:.")
            print("Collega ORA il dispositivo USB (aspetta che Windows lo monti).")
            action = ask(
                "Poi premi Invio (s = salta, q = esci): "
            ).lower()
            if action in {"q", "quit"}:
                raise SystemExit(0)
            if action in {"s", "skip"}:
                return session_dir, "saltato", 0, 0
            drive = wait_for_new_drive(before, cfg.wait_timeout_sec, cfg.poll_interval_sec)
            if drive is None:
                retry = ask("[r] riprova, [s] salta, [q] esci: ").lower()
                if retry in {"s", "skip"}:
                    return session_dir, "saltato", 0, 0
                if retry in {"q", "quit"}:
                    raise SystemExit(0)
                continue

        files = []
        try:
            root = source_root_for(drive, cfg.source_subdir)
        except FileNotFoundError as exc:
            print(exc)
            retry = ask("[r] riprova, [s] salta: ").lower()
            if retry in {"s", "skip"}:
                return session_dir, "saltato", 0, 0
            continue

        files = sorted(iter_matching_files(root, cfg.extensions))
        if not files:
            exts = ", ".join(sorted(cfg.extensions)) or "(nessuna)"
            print(f"Nessun file con estensioni {exts} in {root}")
            retry = ask("[r] riprova, [s] salta: ").lower()
            if retry in {"s", "skip"}:
                return session_dir, "saltato", 0, 0
            continue

        groups = group_files(files, cfg.filename_pattern)
        chosen = choose_group(groups)
        if chosen is None:
            return session_dir, "saltato", 0, 0

        stamps = [s for _, s in chosen if s]
        if not stamps and session_dir is None:
            print("I file scelti non hanno data/ora nel nome.")
            if ask("Continuare comunque? [s/N]: ").lower() not in {"s", "y", "si", "sì"}:
                return session_dir, "saltato", 0, 0
        elif not stamps:
            print("Avviso: file senza data/ora nel nome; uso la cartella sessione già aperta.")
            if ask("Copiare comunque? [s/N]: ").lower() not in {"s", "y", "si", "sì"}:
                return session_dir, "saltato", 0, 0

        stamp = stamps[0] if stamps else None
        session_dir = ensure_session(session_dir, stamp, cfg.devices, cfg.output_dir)
        dest_root = session_dir / device.folder
        copied, nbytes, dests = copy_files(chosen, root, dest_root)
        print(f"Copiati {copied} file ({nbytes} byte) in {dest_root}")
        run_post_copy_processing(cfg, dests)

        if from_dir is None:
            ask("Scollega il dispositivo e premi Invio per continuare.")
            wait_until_unplugged(drive, min(30, cfg.wait_timeout_sec), cfg.poll_interval_sec)
        return session_dir, "ok" if copied else "errore", copied, nbytes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copia i dati da 9 dispositivi USB, uno alla volta."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=SCRIPT_DIR / "config.json",
        help="Percorso del file di configurazione",
    )
    parser.add_argument(
        "--from",
        dest="from_dir",
        type=Path,
        default=None,
        help="Cartella locale al posto dell'USB (solo per prova)",
    )
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    args = parse_args()
    cfg = load_config(args.config)
    from_dir = args.from_dir.resolve() if args.from_dir else None
    if from_dir and not from_dir.is_dir():
        raise SystemExit(f"Cartella --from non trovata: {from_dir}")
    if not cfg.extensions:
        print("Attenzione: 'extensions' è vuoto, non verrà copiato nessun file.")

    print("Acquisizione dati da dispositivi USB")
    print(f"Estensioni: {', '.join(sorted(cfg.extensions)) or '(nessuna)'}")
    print(f"Pattern nome file: {cfg.filename_pattern.pattern}")
    print(f"Destinazione: {cfg.output_dir}")
    if cfg.gestus_split:
        print(f"Dopo la copia: {cfg.gestus_split}")
    if cfg.convbin:
        print(f"Poi convbin ({cfg.convbin_format}): {cfg.convbin}")
    if from_dir:
        print(f"Modalità prova: {from_dir}")

    session_dir: Path | None = None
    results: list[tuple[str, str, int]] = []
    total = len(cfg.devices)
    for i, device in enumerate(cfg.devices, start=1):
        session_dir, status, copied, _nbytes = process_device(
            device, i, total, cfg, session_dir, from_dir
        )
        results.append((device.label, status, copied))

    print()
    print("=" * 60)
    print("Riepilogo")
    print("=" * 60)
    if session_dir:
        print(f"Cartella sessione: {session_dir}")
    else:
        print("Nessuna cartella sessione creata (nessuna copia).")
    for label, status, copied in results:
        extra = f", {copied} file" if status == "ok" else ""
        print(f"  - {label}: {status}{extra}")


if __name__ == "__main__":
    main()
