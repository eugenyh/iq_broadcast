#!/usr/bin/env python3
"""
spectrum_cache.py

Расчёт и кэширование спектра (FFT) для сигналов библиотеки.

Используется в двух ролях:
1. Конвертеры (wav_to_iq_library.py, generate_sine.py) вызывают
   build_cache_for_signal() сразу после генерации IQ-файла — кэш
   всегда создаётся вместе с сигналом, отдельным шагом руками
   готовить не нужно.
2. backend/main.py читает готовый кэш при воспроизведении вместо
   расчёта FFT в реальном времени (load_cache_if_valid); если кэша
   нет или он не подходит — использует iter_raw_frames() напрямую.
   Таким образом live-режим и кэш считаются ОДНИМ и тем же кодом —
   не могут разойтись друг с другом.

Можно запускать и как отдельный скрипт:
    python3 spectrum_cache.py               # пересчитать кэш для ВСЕХ сигналов библиотеки
    python3 spectrum_cache.py stanag-4285    # пересчитать кэш только для одного сигнала
"""
import argparse
import json
from pathlib import Path

import numpy as np
from scipy.signal.windows import blackmanharris

CHUNK_SAMPLES = 65536   # окно БПФ — совпадает с тем, что использовалось при живом расчёте
BYTES_PER_SAMPLE = 2    # int8 I + int8 Q

CACHE_SUBDIR = "spectrum_cache"


def freq_crop_indices(sample_rate, freq_min_khz, freq_max_khz, chunk_samples=CHUNK_SAMPLES):
    """Возвращает (freqs_khz_cropped, lo_idx, hi_idx)."""
    freqs_khz_full = np.fft.fftshift(np.fft.fftfreq(chunk_samples, d=1 / sample_rate)) / 1e3
    lo_idx = int(np.searchsorted(freqs_khz_full, freq_min_khz))
    hi_idx = int(np.searchsorted(freqs_khz_full, freq_max_khz))
    return freqs_khz_full[lo_idx:hi_idx].round(2), lo_idx, hi_idx


def iter_raw_frames(iq_file_path, chunk_samples=CHUNK_SAMPLES):
    """
    Один проход по файлу от начала до конца (без зацикливания — это
    забота вызывающего кода). На каждой порции отдаёт ПОЛНЫЙ
    (необрезанный по частоте) спектр в дБ. Последний неполный блок в
    конце файла отбрасывается — так же, как и раньше при живом расчёте.
    """
    window = blackmanharris(chunk_samples)
    win_norm = np.sum(window)
    bytes_to_read = chunk_samples * BYTES_PER_SAMPLE

    with open(iq_file_path, "rb") as f:
        while True:
            raw = f.read(bytes_to_read)
            if len(raw) < bytes_to_read:
                return
            raw_i8 = np.frombuffer(raw, dtype=np.int8)
            iq = (raw_i8[0::2] / 127.0) + 1j * (raw_i8[1::2] / 127.0)
            windowed = iq * window
            fft = np.fft.fftshift(np.fft.fft(windowed)) / win_norm
            db = 20 * np.log10(np.abs(fft) + 1e-12)
            yield db


def _cache_path(library_dir: Path, signal_id: str) -> Path:
    return library_dir / CACHE_SUBDIR / f"{signal_id}.npz"


def build_cache_for_signal(signal_meta: dict, library_dir: Path) -> Path:
    """
    Считает спектр по всему файлу сигнала и сохраняет в
    library/spectrum_cache/<id>.npz. Конвертеры вызывают это
    автоматически — руками дёргать нужно только для пересчёта без
    переконвертации IQ (например, поменяли CHUNK_SAMPLES).
    """
    file_path = library_dir / signal_meta["file"]
    sample_rate = signal_meta["sample_rate"]
    freq_min_khz = signal_meta.get("spectrum_freq_min_khz", -5.0)
    freq_max_khz = signal_meta.get("spectrum_freq_max_khz", 5.0)

    freqs_khz, lo_idx, hi_idx = freq_crop_indices(sample_rate, freq_min_khz, freq_max_khz)

    frames = [db[lo_idx:hi_idx].astype(np.float32) for db in iter_raw_frames(file_path)]

    if not frames:
        raise RuntimeError(
            f"Файл {file_path} короче одного окна БПФ ({CHUNK_SAMPLES} отсчётов) — кэш не создан."
        )

    frames = np.stack(frames)

    cache_dir = library_dir / CACHE_SUBDIR
    cache_dir.mkdir(exist_ok=True)
    out_path = _cache_path(library_dir, signal_meta["id"])
    np.savez_compressed(
        out_path,
        freqs_khz=freqs_khz.astype(np.float32),
        frames=frames,
        sample_rate=sample_rate,
        chunk_samples=CHUNK_SAMPLES,
        freq_min_khz=freq_min_khz,
        freq_max_khz=freq_max_khz,
    )
    print(f"[spectrum_cache] {signal_meta['id']}: {frames.shape[0]} кадров x {frames.shape[1]} точек "
          f"-> {out_path.name} ({out_path.stat().st_size/1e6:.2f} МБ)")
    return out_path


def load_cache_if_valid(signal_meta: dict, library_dir: Path):
    """
    Возвращает (freqs_khz: list, frames: np.ndarray), если кэш есть и
    соответствует текущим метаданным сигнала, иначе None — без
    исключений, вызывающий код в этом случае просто считает вживую.
    """
    cache_path = _cache_path(library_dir, signal_meta["id"])
    if not cache_path.exists():
        return None

    iq_path = library_dir / signal_meta["file"]
    if iq_path.exists() and cache_path.stat().st_mtime < iq_path.stat().st_mtime:
        print(f"[spectrum_cache] кэш для '{signal_meta['id']}' старше IQ-файла — считаю вживую")
        return None

    try:
        data = np.load(cache_path)
        expected = {
            "sample_rate": signal_meta["sample_rate"],
            "chunk_samples": CHUNK_SAMPLES,
            "freq_min_khz": signal_meta.get("spectrum_freq_min_khz", -5.0),
            "freq_max_khz": signal_meta.get("spectrum_freq_max_khz", 5.0),
        }
        for key, want in expected.items():
            got = data[key].item()
            if abs(float(got) - float(want)) > 1e-6:
                print(f"[spectrum_cache] кэш для '{signal_meta['id']}' не совпадает по '{key}' — считаю вживую")
                return None
        return data["freqs_khz"].tolist(), data["frames"]
    except Exception as e:
        print(f"[spectrum_cache] не удалось прочитать кэш для '{signal_meta['id']}' ({e}) — считаю вживую")
        return None


def _load_library(library_dir: Path):
    with open(library_dir / "library.json", "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("signal_id", nargs="?", default=None,
                     help="ID сигнала для пересчёта. Без аргумента — пересчитать все сигналы библиотеки.")
    ap.add_argument("--library", default="../library", help="Путь к папке библиотеки")
    args = ap.parse_args()

    library_dir = Path(args.library).resolve()
    signals = _load_library(library_dir)

    if args.signal_id:
        signals = [s for s in signals if s["id"] == args.signal_id]
        if not signals:
            print(f"Сигнал '{args.signal_id}' не найден в библиотеке.")
            return

    for meta in signals:
        build_cache_for_signal(meta, library_dir)


if __name__ == "__main__":
    main()
