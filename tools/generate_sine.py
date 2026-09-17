#!/usr/bin/env python3
"""
generate_sine.py

Генерирует чистый одночастотный тестовый сигнал (несущая или тон со
смещением) напрямую в комплексном IQ — без WAV на входе, без ФНЧ и
преобразования Гильберта. Удобно для калибровки тракта, проверки
приёмника/анализатора спектра, отладки самой библиотеки.

Смещение от центра (offset) — по умолчанию 1 кГц, не 0: у многих SDR-
передатчиков (в т.ч. HackRF, прямое преобразование) на самой несущей
бывает паразитный DC-спайк от утечки гетеродина. Тон чуть в стороне от
центра эту наводку не путает с полезным сигналом на анализаторе.

Длительность автоматически подгоняется под целое число периодов тона —
тогда зацикливание файла (-R у hackrf_transfer) проходит без скачка
фазы/щелчка на стыке. При offset=0 (чистая несущая, без вращения фазы)
подгонка не нужна — там жёстко постоянный I/Q, шов не может быть виден.

Использование:
    python3 generate_sine.py \
        --id test-tone-1khz \
        --name "Тестовый тон +1 кГц" \
        --freq 14000000 \
        --library ../library
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from spectrum_cache import build_cache_for_signal
from library_utils import register_signal
from signal_spec import add_spec_arguments, spec_entry_from_args


def generate(output_path, sample_rate, offset_hz, amplitude, duration_hint):
    if offset_hz == 0:
        # чистая несущая: постоянный вектор, фаза не крутится — шва при
        # зацикливании в принципе быть не может
        n = int(round(duration_hint * sample_rate))
        iq = np.full(n, amplitude, dtype=np.complex128)
    else:
        # подгоняем длительность под целое число периодов тона
        cycles = max(1, round(offset_hz * duration_hint))
        duration = cycles / offset_hz
        n = int(round(duration * sample_rate))
        t = np.arange(n) / sample_rate
        iq = amplitude * np.exp(2j * np.pi * offset_hz * t)

    i8 = np.clip(np.round(iq.real * 127), -128, 127).astype(np.int8)
    q8 = np.clip(np.round(iq.imag * 127), -128, 127).astype(np.int8)
    interleaved = np.empty(i8.size * 2, dtype=np.int8)
    interleaved[0::2] = i8
    interleaved[1::2] = q8
    interleaved.tofile(output_path)

    duration = n / sample_rate
    print(f"Готово: {n} комплексных отсчётов, {duration:.4f} с при {sample_rate} Гц.")
    print(f"Размер файла: {interleaved.nbytes} байт (~{interleaved.nbytes/1e6:.2f} МБ).")
    return duration



def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", required=True, help="Уникальный ID сигнала в библиотеке")
    ap.add_argument("--name", default="Тестовый тон", help="Отображаемое имя сигнала")
    ap.add_argument("--freq", type=float, required=True,
                     help="Центральная (настраиваемая) частота, Гц")
    ap.add_argument("--offset-khz", type=float, default=1.0,
                     help="Смещение тона от центра, кГц (по умолчанию 1.0; "
                          "0 = чистая несущая точно на центральной частоте)")
    ap.add_argument("--amplitude", type=float, default=0.8,
                     help="Амплитуда 0..1 (по умолчанию 0.8)")
    ap.add_argument("--duration", type=float, default=2.0,
                     help="Ориентировочная длительность, с (по умолчанию 2.0 — "
                          "реальная будет чуть скорректирована под целое число периодов)")
    ap.add_argument("--sample-rate", type=int, default=2_000_000,
                     help="Частота дискретизации для HackRF, Гц (по умолчанию 2000000)")
    ap.add_argument("--library", default="../library", help="Путь к папке библиотеки")
    ap.add_argument("--tx-vga-gain", type=int, default=20,
                     help="TX VGA gain для hackrf_transfer, 0-47 дБ")
    ap.add_argument("--amp-enable", action="store_true",
                     help="Включить встроенный RF-усилитель HackRF (+14 дБ). Осторожно!")
    ap.add_argument("--description", default="",
                     help="Краткое описание (по умолчанию сгенерируется автоматически)")
    ap.add_argument("--spectrum-freq-min-khz", type=float, default=None,
                     help="Нижняя граница окна спектра, кГц (по умолчанию подбирается сама)")
    ap.add_argument("--spectrum-freq-max-khz", type=float, default=None,
                     help="Верхняя граница окна спектра, кГц (по умолчанию подбирается сама)")
    ap.add_argument("--spectrum-db-min", type=float, default=-100.0)
    ap.add_argument("--spectrum-db-max", type=float, default=0.0)
    add_spec_arguments(ap)
    args = ap.parse_args()

    library_dir = Path(args.library).resolve()
    output_iq = library_dir / f"{args.id}.cs8"

    duration = generate(output_iq, args.sample_rate, args.offset_khz * 1000,
                         args.amplitude, args.duration)

    margin = max(2.0, abs(args.offset_khz) * 0.5)
    freq_min = args.spectrum_freq_min_khz
    freq_max = args.spectrum_freq_max_khz
    if freq_min is None:
        freq_min = min(-2.0, args.offset_khz - margin)
    if freq_max is None:
        freq_max = max(2.0, args.offset_khz + margin)

    description = args.description or (
        "Чистая несущая, без модуляции" if args.offset_khz == 0
        else f"Тестовый тон, смещение {args.offset_khz:+.2f} кГц от центральной частоты"
    )

    entry = {
        "id": args.id,
        "name": args.name,
        "file": output_iq.name,
        "sample_rate": args.sample_rate,
        "recommended_freq_hz": args.freq,
        "tx_vga_gain": args.tx_vga_gain,
        "amp_enable": args.amp_enable,
        "sideband": "n/a",
        "gain": args.amplitude,
        "loop": True,
        "spectrum_freq_min_khz": freq_min,
        "spectrum_freq_max_khz": freq_max,
        "spectrum_db_min": args.spectrum_db_min,
        "spectrum_db_max": args.spectrum_db_max,
        "description": description,
        "duration_sec": round(duration, 4),
        **spec_entry_from_args(args),
    }
    # разумное значение по умолчанию для модуляции — можно переопределить через --modulation
    if entry["modulation"] is None:
        entry["modulation"] = "Несущая без модуляции (CW)" if args.offset_khz == 0 else "Немодулированный тон"
    if entry["tone_count"] is None:
        entry["tone_count"] = 1
    if entry["fec"] is None:
        entry["fec"] = "нет"
    if entry["interleaving"] is None:
        entry["interleaving"] = "нет"

    register_signal(library_dir, entry)
    build_cache_for_signal(entry, library_dir)


if __name__ == "__main__":
    main()
