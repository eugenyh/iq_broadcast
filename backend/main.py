"""
FastAPI-бэкенд библиотеки IQ-сигналов для трансляции через HackRF One.

Библиотека хранит файлы СРАЗУ в формате HackRF (int8, нужная частота
дискретизации) — конвертация происходит один раз при добавлении сигнала
(tools/wav_to_iq_library.py), а не при каждом воспроизведении. Поэтому
здесь никакого кэша/рендера IQ в момент play — сразу старт.

Спектр для веб-интерфейса по возможности берётся из готового кэша
(см. spectrum_cache.py) — конвертеры считают его один раз при добавлении
сигнала. Если кэша нет — считается вживую тем же самым кодом.

Эндпоинты:
  GET  /signals              — список сигналов из library/library.json
  GET  /status                — что сейчас играет / последняя ошибка
  POST /play/{signal_id}     — начать передачу выбранного сигнала (останавливает предыдущую)
  POST /stop                 — остановить текущую передачу
  WS   /ws/spectrum          — поток FFT-спектра активной трансляции
  WS   /ws/preview/{id}      — предпросмотр спектра сигнала БЕЗ передачи в HackRF
  POST /import               — импорт бандла сигналов (.tar с manifest.json + signals/)

Запуск (из папки backend/):
  uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""
import asyncio
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, Set

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from hackrf_tx import HackRFTransmitter
from spectrum_cache import (CHUNK_SAMPLES, freq_crop_indices, iter_raw_frames,
                             load_cache_if_valid, build_cache_for_signal)
from library_utils import load_library, register_signal
from signal_spec import SPEC_DEFAULTS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIBRARY_DIR = PROJECT_ROOT / "library"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
IS_WINDOWS = sys.platform == "win32"

AVG_ALPHA = 0.25

app = FastAPI(title="IQ broadcast library (HackRF)")


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    """
    Подстраховка: любое необработанное исключение (в т.ч. из самого
    FastAPI/Starlette — например, при разборе очень большого multipart-
    запроса, до того как код эндпоинта вообще успевает выполниться)
    всё равно возвращается в ожидаемом фронтендом виде {"error": "..."},
    а не в служебном формате FastAPI (тот же {"detail": ...}), на который
    фронтенд не рассчитан и падает с непонятной JS-ошибкой вместо
    внятного сообщения.
    """
    print(f"[main] необработанное исключение на {request.url.path}: {exc}")
    return JSONResponse(status_code=200, content={"error": f"{type(exc).__name__}: {exc}"})


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Отдельно от общего обработчика выше: FastAPI регистрирует свой
    обработчик именно для RequestValidationError с более высоким
    приоритетом, так что общий @app.exception_handler(Exception) его не
    перехватывает — нужен свой. Именно такая ошибка возникает, если при
    отправке очень большого файла multipart-тело запроса обрывается/
    повреждается на середине: python-multipart не может вычленить поле
    'file', и FastAPI сообщает "поле отсутствует", хотя на самом деле
    проблема в оборвавшейся передаче.
    """
    print(f"[main] ошибка валидации запроса на {request.url.path}: {exc}")
    return JSONResponse(status_code=200, content={
        "error": "Некорректный запрос — возможно, загрузка файла прервалась "
                 f"на середине (особенно вероятно для больших файлов). Детали: {exc}"
    })


async def _safe_send(ws: WebSocket, payload: str):
    try:
        await ws.send_text(payload)
    except Exception:
        pass


class PlaybackState:
    def __init__(self):
        self.thread: Optional[threading.Thread] = None
        self.spectrum_thread: Optional[threading.Thread] = None
        self.stop_flag = threading.Event()
        self.subscribers: Set[WebSocket] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.current_signal_id: Optional[str] = None
        self.current_freq_hz: Optional[float] = None
        self.current_tx_vga_gain: Optional[int] = None
        self.current_amp_enable: Optional[bool] = None
        self.current_loop_enabled: Optional[bool] = None
        self.current_duration_sec: Optional[float] = None
        self.playback_start_time: Optional[float] = None
        self.cycle_start_time: Optional[float] = None
        self.cycle_count: Optional[int] = None
        self.phase: Optional[str] = None  # "starting" | "playing" | None
        self.last_error: Optional[str] = None

    def broadcast(self, freqs_khz, db):
        if not self.subscribers or self.loop is None:
            return
        payload = json.dumps({"freqs": freqs_khz, "db": db})
        for ws in list(self.subscribers):
            asyncio.run_coroutine_threadsafe(_safe_send(ws, payload), self.loop)


state = PlaybackState()


def stream_spectrum(signal_meta: dict, emit_fn, stop_event: threading.Event):
    """
    Общий цикл проигрывания спектра в реальном темпе — используется и
    для реальной трансляции (spectrum_worker, шлёт всем подписчикам
    активной сессии), и для предпросмотра (ws_preview, шлёт в одно
    конкретное соединение). Единственная разница между вызовами —
    сам emit_fn(freqs_khz, db) и то, чей stop_event их останавливает.

    Сначала пробует готовый кэш (load_cache_if_valid) — тогда это
    просто "проигрывание" уже посчитанных кадров по кругу. Если кэша
    нет/не подходит — считает вживую через iter_raw_frames() из
    spectrum_cache — тем же кодом, что строит кэш, поэтому поведение
    гарантированно не расходится между режимами.
    """
    file_path = LIBRARY_DIR / signal_meta["file"]
    sample_rate = signal_meta["sample_rate"]

    cached = load_cache_if_valid(signal_meta, LIBRARY_DIR)
    block_duration = CHUNK_SAMPLES / sample_rate

    if cached is not None:
        freqs_khz, frames = cached
        avg_db = np.full(frames.shape[1], -100.0)
        idx = 0
        n_frames = frames.shape[0]
        while not stop_event.is_set():
            t0 = time.monotonic()
            avg_db[:] = AVG_ALPHA * frames[idx] + (1 - AVG_ALPHA) * avg_db
            emit_fn(freqs_khz, avg_db.round(1).tolist())
            idx = (idx + 1) % n_frames

            elapsed = time.monotonic() - t0
            sleep_left = block_duration - elapsed
            if sleep_left > 0:
                time.sleep(sleep_left)
        return

    # --- живой расчёт (нет валидного кэша) ---
    freq_min_khz = signal_meta.get("spectrum_freq_min_khz", -5.0)
    freq_max_khz = signal_meta.get("spectrum_freq_max_khz", 5.0)
    freqs_khz, lo_idx, hi_idx = freq_crop_indices(sample_rate, freq_min_khz, freq_max_khz)
    freqs_khz = freqs_khz.tolist()
    avg_db = np.full(hi_idx - lo_idx, -100.0)

    while not stop_event.is_set():
        for db in iter_raw_frames(file_path):
            if stop_event.is_set():
                return
            t0 = time.monotonic()
            avg_db[:] = AVG_ALPHA * db[lo_idx:hi_idx] + (1 - AVG_ALPHA) * avg_db
            emit_fn(freqs_khz, avg_db.round(1).tolist())

            elapsed = time.monotonic() - t0
            sleep_left = block_duration - elapsed
            if sleep_left > 0:
                time.sleep(sleep_left)


def spectrum_worker(signal_meta: dict):
    """Спектр активной трансляции — шлёт всем подписчикам /ws/spectrum."""
    stream_spectrum(signal_meta, state.broadcast, state.stop_flag)


def tx_worker(signal_meta: dict):
    file_path = LIBRARY_DIR / signal_meta["file"]
    sample_rate = signal_meta["sample_rate"]
    freq = signal_meta["recommended_freq_hz"]
    tx_vga_gain = signal_meta.get("tx_vga_gain", 20)
    amp_enable = signal_meta.get("amp_enable", False)
    loop_enabled = signal_meta.get("loop", True)

    tx = HackRFTransmitter(frequency_hz=freq, sample_rate_hz=sample_rate,
                            tx_vga_gain=tx_vga_gain, amp_enable=amp_enable, loop=loop_enabled)
    state.phase = "starting"
    try:
        tx.start(file_path)
    except Exception as e:
        state.last_error = str(e)
        print("[main] ошибка запуска HackRF:", e)
        state.phase = None
        state.current_signal_id = None
        return

    state.phase = "playing"
    state.current_freq_hz = freq
    state.current_tx_vga_gain = tx_vga_gain
    state.current_amp_enable = amp_enable
    state.current_loop_enabled = loop_enabled
    state.current_duration_sec = signal_meta.get("duration_sec")
    state.playback_start_time = time.monotonic()
    state.spectrum_thread = threading.Thread(target=spectrum_worker, args=(signal_meta,), daemon=True)
    state.spectrum_thread.start()

    try:
        while not state.stop_flag.is_set():
            if not tx.check_alive():
                break  # файл доигран до конца без зацикливания — это не ошибка
            if tx.cycle_start_time is not None:
                state.cycle_start_time = tx.cycle_start_time
                state.cycle_count = tx.cycle_count
            time.sleep(1.0)
    except Exception as e:
        state.last_error = str(e)
        print("[main] ошибка во время передачи:", e)
    finally:
        state.stop_flag.set()
        tx.close()
        if state.spectrum_thread is not None:
            state.spectrum_thread.join(timeout=3)
        state.phase = None
        state.current_signal_id = None
        state.current_freq_hz = None
        state.current_tx_vga_gain = None
        state.current_amp_enable = None
        state.current_loop_enabled = None
        state.current_duration_sec = None
        state.playback_start_time = None
        state.cycle_start_time = None
        state.cycle_count = None


@app.on_event("startup")
async def on_startup():
    state.loop = asyncio.get_event_loop()
    # подчищаем мусор от возможной прошлой аварийно прерванной распаковки
    stale_tmp = LIBRARY_DIR / "_import_tmp"
    if stale_tmp.exists():
        shutil.rmtree(stale_tmp, ignore_errors=True)


@app.get("/signals")
def get_signals():
    return load_library(LIBRARY_DIR)


@app.get("/status")
def get_status():
    elapsed_sec = None
    cycle_position_sec = None
    cycle_number = None
    if state.phase == "playing":
        if state.playback_start_time is not None:
            elapsed_sec = round(time.monotonic() - state.playback_start_time, 1)
        if state.cycle_start_time is not None:
            # достоверная позиция — подтверждена реальным "Rewind" из
            # вывода hackrf_transfer, а не просто вычислена делением
            cycle_position_sec = round(time.monotonic() - state.cycle_start_time, 1)
            cycle_number = state.cycle_count
    return {
        "playing": state.current_signal_id,
        "phase": state.phase,
        "freq_hz": state.current_freq_hz,
        "tx_vga_gain": state.current_tx_vga_gain,
        "amp_enable": state.current_amp_enable,
        "loop": state.current_loop_enabled,
        "duration_sec": state.current_duration_sec,
        "elapsed_sec": elapsed_sec,
        "cycle_position_sec": cycle_position_sec,
        "cycle_number": cycle_number,
        "last_error": state.last_error,
    }


@app.post("/play/{signal_id}")
def play(signal_id: str, freq_hz: Optional[float] = None,
          tx_vga_gain: Optional[int] = None, amp_enable: Optional[bool] = None,
          loop: Optional[bool] = None):
    library = load_library(LIBRARY_DIR)
    meta = next((s for s in library if s["id"] == signal_id), None)
    if meta is None:
        return {"error": "signal not found"}

    meta = dict(meta)  # копия — не трогаем сохранённые в library.json данные
    if freq_hz:
        meta["recommended_freq_hz"] = freq_hz
    if tx_vga_gain is not None:
        meta["tx_vga_gain"] = max(0, min(47, tx_vga_gain))
    if amp_enable is not None:
        meta["amp_enable"] = amp_enable
    if loop is not None:
        meta["loop"] = loop

    _stop_current()

    state.stop_flag.clear()
    state.last_error = None
    state.current_signal_id = signal_id
    state.thread = threading.Thread(target=tx_worker, args=(meta,), daemon=True)
    state.thread.start()
    return {
        "status": "starting",
        "signal_id": signal_id,
        "freq_hz": meta["recommended_freq_hz"],
        "tx_vga_gain": meta["tx_vga_gain"],
        "amp_enable": meta["amp_enable"],
        "loop": meta["loop"],
    }


@app.post("/stop")
def stop():
    _stop_current()
    return {"status": "stopped"}


def _stop_current():
    if state.thread is not None and state.thread.is_alive():
        state.stop_flag.set()
        state.thread.join(timeout=10)
    state.current_signal_id = None
    state.phase = None
    state.current_freq_hz = None
    state.current_tx_vga_gain = None
    state.current_amp_enable = None
    state.current_loop_enabled = None
    state.current_duration_sec = None
    state.playback_start_time = None
    state.cycle_start_time = None
    state.cycle_count = None


@app.websocket("/ws/spectrum")
async def ws_spectrum(websocket: WebSocket):
    await websocket.accept()
    state.subscribers.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        state.subscribers.discard(websocket)


@app.websocket("/ws/preview/{signal_id}")
async def ws_preview(websocket: WebSocket, signal_id: str):
    """
    Предпросмотр спектра сигнала БЕЗ передачи в HackRF. Полностью
    независим от PlaybackState — свой поток и свой stop_event на
    каждое соединение, поэтому можно смотреть предпросмотр даже во
    время реальной трансляции другого (или того же) сигнала, они друг
    другу не мешают.
    """
    await websocket.accept()

    library = load_library(LIBRARY_DIR)
    meta = next((s for s in library if s["id"] == signal_id), None)
    if meta is None:
        await websocket.close(code=4404)
        return

    loop = asyncio.get_event_loop()
    stop_event = threading.Event()

    def emit(freqs_khz, db):
        payload = json.dumps({"freqs": freqs_khz, "db": db})
        asyncio.run_coroutine_threadsafe(_safe_send(websocket, payload), loop)

    thread = threading.Thread(target=stream_spectrum, args=(meta, emit, stop_event), daemon=True)
    thread.start()

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        stop_event.set()
        thread.join(timeout=3)


def _safe_extract(tar: tarfile.TarFile, dest: Path):
    """Защита от path traversal и симлинков при распаковке бандла из
    недоверенного источника."""
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        member_path = (dest / member.name).resolve()
        try:
            member_path.relative_to(dest_resolved)
        except ValueError:
            raise ValueError(f"Небезопасный путь в архиве: {member.name}")
        if member.issym() or member.islnk():
            raise ValueError(f"Символические ссылки в архиве не поддерживаются: {member.name}")
    try:
        tar.extractall(dest, filter="data")
    except TypeError:
        tar.extractall(dest)  # старый Python без параметра filter — пути уже проверены выше


def _import_tmp_dir() -> Path:
    """
    Папка для временной распаковки бандлов — намеренно РЯДОМ с самой
    библиотекой (внутри library/), а не системный temp по умолчанию.
    На Windows системный temp почти всегда на диске C:, и если библиотека
    (и место для неё) на другом диске (D:, E:...) — распаковка большого
    бандла может упереться в нехватку места на C:, даже когда на целевом
    диске места полно.
    """
    d = LIBRARY_DIR / "_import_tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


REQUIRED_MANIFEST_FIELDS = ["id", "name", "file", "sample_rate", "recommended_freq_hz"]

MANIFEST_DEFAULTS = {
    "tx_vga_gain": 20,
    "amp_enable": False,
    "sideband": "n/a",
    "gain": 1.0,
    "loop": True,
    "spectrum_freq_min_khz": -5.0,
    "spectrum_freq_max_khz": 5.0,
    "spectrum_db_min": -100.0,
    "spectrum_db_max": 0.0,
    "description": "",
    "duration_sec": 0.0,
    **SPEC_DEFAULTS,
}


class ImportState:
    """
    Состояние текущего импорта — по образцу PlaybackState: один активный
    импорт на сервер, прогресс опрашивается через /import/status,
    аналогично тому, как /status опрашивается для трансляции.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.in_progress = False
        self.step: Optional[str] = None
        self.done = 0
        self.total = 0
        self.result: Optional[dict] = None
        self.error: Optional[str] = None


import_state = ImportState()


def _import_bundle_from_path(tar_path: Path, progress_cb=None) -> dict:
    """
    Общая логика импорта бандла с локального диска сервера. progress_cb,
    если передан, вызывается как progress_cb(step_text, done, total) на
    каждом значимом шаге — на этом строится прогресс-бар в интерфейсе.
    """
    def report(step, done, total):
        if progress_cb:
            progress_cb(step, done, total)

    with tempfile.TemporaryDirectory(dir=str(_import_tmp_dir())) as tmp:
        report("Распаковка архива...", 0, 0)
        extract_dir = Path(tmp) / "extracted"
        extract_dir.mkdir()
        with tarfile.open(tar_path) as tar:
            _safe_extract(tar, extract_dir)

        manifest_path = extract_dir / "manifest.json"
        if not manifest_path.exists():
            return {"error": "В архиве нет manifest.json — это не бандл библиотеки"}

        with open(manifest_path, "r", encoding="utf-8") as f:
            entries = json.load(f)

        existing_ids = {s["id"] for s in load_library(LIBRARY_DIR)}
        imported = []
        total = len(entries)
        report(f"Найдено сигналов: {total}", 0, total)

        for i, raw_entry in enumerate(entries):
            missing = [k for k in REQUIRED_MANIFEST_FIELDS if k not in raw_entry]
            if missing:
                return {"error": f"В записи манифеста не хватает полей {missing}: {raw_entry}"}

            entry = {**MANIFEST_DEFAULTS, **raw_entry}

            report(f"Копирование '{entry['id']}' ({i+1}/{total})...", i, total)
            src_file = extract_dir / "signals" / Path(raw_entry["file"]).name
            if not src_file.exists():
                return {"error": f"Не найден файл сигнала для '{entry['id']}' в архиве"}

            dest_filename = f"{entry['id']}.cs8"
            entry["file"] = dest_filename
            shutil.copy(src_file, LIBRARY_DIR / dest_filename)

            register_signal(LIBRARY_DIR, entry)

            report(f"Построение кэша спектра '{entry['id']}' ({i+1}/{total})...", i, total)
            build_cache_for_signal(entry, LIBRARY_DIR)

            imported.append({
                "id": entry["id"],
                "name": entry["name"],
                "updated": entry["id"] in existing_ids,
            })
            report(f"Готово: {entry['id']} ({i+1}/{total})", i + 1, total)

    return {"status": "ok", "imported": imported}


def _run_import_worker(tar_path: Path):
    def progress_cb(step, done, total):
        import_state.step = step
        import_state.done = done
        import_state.total = total

    try:
        result = _import_bundle_from_path(tar_path, progress_cb=progress_cb)
        if result.get("error"):
            import_state.error = result["error"]
        else:
            import_state.result = result
    except Exception as e:
        import_state.error = str(e)
    finally:
        import_state.in_progress = False
        import_state.step = None


@app.post("/import-local")
def import_local(path: str):
    """
    Импорт бандла напрямую с диска сервера — единственный способ импорта
    (загрузка через браузер убрана: HTTP-multipart для файлов в сотни МБ
    на практике оказался ненадёжным — "There was an error parsing the
    body" на больших файлах). Работает в фоновом потоке, прогресс — через
    /import/status.
    """
    if import_state.in_progress:
        return {"error": "Уже идёт другой импорт — дождитесь его завершения"}

    tar_path = Path(path)
    if not _is_path_allowed(tar_path):
        return {"error": f"Путь вне разрешённых директорий: {path}"}
    if not tar_path.exists():
        return {"error": f"Файл не найден: {path}"}
    if not tar_path.is_file():
        return {"error": f"Это не файл: {path}"}

    import_state.in_progress = True
    import_state.step = "Запуск..."
    import_state.done = 0
    import_state.total = 0
    import_state.result = None
    import_state.error = None

    thread = threading.Thread(target=_run_import_worker, args=(tar_path,), daemon=True)
    thread.start()
    return {"status": "started"}


@app.get("/import/status")
def import_status():
    return {
        "in_progress": import_state.in_progress,
        "step": import_state.step,
        "done": import_state.done,
        "total": import_state.total,
        "result": import_state.result,
        "error": import_state.error,
    }


def _list_windows_drives():
    """
    Определение дисков на Windows через WinAPI GetLogicalDrives() — читает
    таблицу зарегистрированных букв у ОС, не опрашивая сам привод (в
    отличие от Path.exists(), который может упасть на пустом CD-приводе
    без диска и уронить весь список из-за одной проблемной буквы).
    """
    try:
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        return [f"{chr(65 + i)}:\\" for i in range(26) if bitmask & (1 << i)]
    except Exception as e:
        print(f"[main] GetLogicalDrives не сработал ({e}), пробую запасной способ")
        import string
        drives = []
        for d in string.ascii_uppercase:
            try:
                if Path(f"{d}:\\").exists():
                    drives.append(f"{d}:\\")
            except OSError:
                continue
        return drives


def _get_allowed_roots() -> list:
    """
    Список корневых директорий, где разрешено искать бандлы. Задаётся
    переменной окружения IMPORT_ALLOWED_ROOTS (пути через запятую) — так
    можно явно ограничить импорт, например, только конкретной флешкой
    вместо всего диска. Без переменной — разумные умолчания: все диски
    на Windows, стандартные точки автомонтирования флешек на Linux/Pi
    (/media, /mnt, /run/media), а если и их нет — весь корень "/".
    """
    env = os.environ.get("IMPORT_ALLOWED_ROOTS")
    if env:
        roots = []
        for part in env.split(","):
            part = part.strip()
            if part:
                try:
                    roots.append(Path(part).resolve())
                except Exception:
                    continue
        if roots:
            return roots

    if IS_WINDOWS:
        return [Path(d).resolve() for d in _list_windows_drives()]
    else:
        candidates = [Path("/media"), Path("/mnt"), Path("/run/media")]
        existing = [p.resolve() for p in candidates if p.exists()]
        return existing if existing else [Path("/").resolve()]


def _is_path_allowed(p: Path) -> bool:
    try:
        p_resolved = p.resolve()
    except Exception:
        return False
    for root in _get_allowed_roots():
        try:
            p_resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _list_dir(p: Path):
    entries = []
    try:
        children = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except (PermissionError, OSError) as e:
        raise RuntimeError(f"Нет доступа к {p}: {e}")
    for child in children:
        try:
            is_dir = child.is_dir()
            if is_dir or child.suffix.lower() == ".tar":
                entries.append({
                    "name": child.name,
                    "path": str(child),
                    "is_dir": is_dir,
                    "size": None if is_dir else child.stat().st_size,
                })
        except (PermissionError, OSError):
            continue  # пропускаем недоступные элементы, не роняем весь листинг
    return entries


@app.get("/import/browse")
def browse(path: str = ""):
    """
    Листинг директории на сервере для файлового браузера в интерфейсе.
    Без пути — список разрешённых корней (см. _get_allowed_roots).
    Навигация выше этих корней запрещена — доступны только они сами и всё,
    что внутри них.
    """
    try:
        roots = _get_allowed_roots()

        if not path:
            return {"path": "", "parent": None,
                    "entries": [{"name": str(r), "path": str(r), "is_dir": True, "size": None} for r in roots]}

        p = Path(path)
        if not _is_path_allowed(p):
            return {"error": f"Доступ ограничен: '{path}' вне разрешённых директорий"}
        if not p.exists() or not p.is_dir():
            return {"error": f"Директория не найдена: {path}"}

        parent = None
        if p.resolve() not in roots:
            parent_candidate = p.parent
            if _is_path_allowed(parent_candidate):
                parent = str(parent_candidate)

        return {"path": str(p), "parent": parent, "entries": _list_dir(p)}
    except Exception as e:
        return {"error": str(e)}


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
