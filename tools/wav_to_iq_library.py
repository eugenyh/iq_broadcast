#!/usr/bin/env python3
"""
wav_to_iq_library.py

Конвертирует моно WAV-файл с тоновым HF-сигналом (STANAG-4285, ALE,
PACTOR, RTTY, PSK31 и т.п. — как он звучит после демодуляции в SSB)
СРАЗУ в формат, готовый для HackRF (int8 I/Q, нужная частота
дискретизации), и регистрирует его в library.json.

Раньше библиотека хранила компактный "мастер"-файл на низкой частоте,
а передискретизация под HackRF происходила во время воспроизведения.
От этого отказались: рендер на лету добавлял заметную задержку перед
стартом каждой передачи (около минуты на 3-минутный сигнал). Раз всё
равно конвертация нужна один раз при добавлении сигнала в библиотеку —
логичнее сразу писать готовый формат, а воспроизведение делать
мгновенным.

Компромисс: файлы в библиотеке крупнее (для 3 минут при 2 МГц — около
700 МБ), зато старт передачи не ждёт ничего.

Использование:
    python3 wav_to_iq_library.py input.wav \
        --id stanag-4285 \
        --name "STANAG-4285" \
        --freq 5000000 \
        --description "HF-модем STANAG-4285, тестовая запись" \
        --library ../library
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import hilbert, butter, filtfilt, sosfilt, sosfilt_zi

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from spectrum_cache import build_cache_for_signal
from library_utils import register_signal
from signal_spec import add_spec_arguments, spec_entry_from_args


def convert(input_wav, output_iq, sample_rate, gain, lowpass):
    print(f"Читаю {input_wav} ...")
    fs_in, audio = wavfile.read(input_wav)

    if audio.ndim > 1:
        print("Файл многоканальный — беру только первый канал.")
        audio = audio[:, 0]

    if audio.dtype.kind == "i":
        max_val = np.iinfo(audio.dtype).max
        audio = audio.astype(np.float64) / max_val
    else:
        audio = audio.astype(np.float64)

    print(f"Исходная частота дискретизации: {fs_in} Гц, длительность: {len(audio)/fs_in:.2f} с")

    if lowpass and lowpass > 0:
        nyq = fs_in / 2
        cutoff = min(lowpass, nyq * 0.98)
        print(f"Применяю ФНЧ со срезом {cutoff:.0f} Гц ...")
        b, a = butter(6, cutoff / nyq, btype="low")
        audio = filtfilt(b, a, audio)

    analytic = hilbert(audio)

    peak = np.max(np.abs(analytic))
    if peak > 0:
        analytic = (analytic / peak) * gain

    n_in = analytic.size
    duration = n_in / fs_in
    n_out = int(round(duration * sample_rate))
    t_in = np.arange(n_in) / fs_in

    print(f"Передискретизация {fs_in} Гц -> {sample_rate} Гц (сразу в int8, блоками) ...")
    print(f"Записываю {output_iq} ...")

    # --- ФНЧ ПОСЛЕ передискретизации ---
    # Простая линейная интерполяция при большом коэффициенте передискретизации
    # (тут обычно в сотни раз) создаёт образы (imaging) на частотах вида
    # -(fs_in - f_сигнала) — проверено эмпирически на нескольких сигналах:
    # предсказанная по этой формуле частота образа совпадала с наблюдаемой
    # с точностью до долей кГц. ФНЧ до Гильберта (выше) от этого не спасает —
    # он ограничивает исходный АУДИО-сигнал, а не подавляет артефакт самой
    # интерполяции, возникающий уже после неё. Поэтому нужен ещё один срез —
    # уже на выходной частоте, с сохранением состояния фильтра (zi) между
    # блоками, чтобы не было щелчков на границах блоков.
    post_cutoff = min(lowpass * 1.5 if lowpass else 5000.0, sample_rate / 2 * 0.9)
    # SOS (second-order sections), а не (b, a) — при таком высоком порядке и
    # очень низкой нормированной частоте среза (доли процента от sample_rate/2)
    # представление (b, a) численно неустойчиво (коэффициенты плохо
    # обусловлены), что даёт NaN на выходе. SOS этой проблемы не имеет.
    sos_post = butter(8, post_cutoff / (sample_rate / 2), btype="low", output="sos")
    zi_re = sosfilt_zi(sos_post) * 0.0
    zi_im = zi_re.copy()

    # выходной блок ограничиваем по количеству ВХОДНЫХ отсчётов, чтобы
    # при большом коэффициенте передискретизации не раздувать память
    chunk_in_samples = 50_000
    scale = 127

    with open(output_iq, "wb") as f:
        start_in = 0
        while start_in < n_in:
            end_in = min(start_in + chunk_in_samples, n_in)
            t0 = start_in / fs_in
            t1 = end_in / fs_in
            out_start = int(round(t0 * sample_rate))
            out_end = int(round(t1 * sample_rate))
            if end_in >= n_in:
                out_end = n_out

            t_out = np.arange(out_start, out_end) / sample_rate
            # немного захватываем соседние отсчёты для корректной интерполяции на границах блока
            lo = max(0, start_in - 4)
            hi = min(n_in, end_in + 4)
            re_out = np.interp(t_out, t_in[lo:hi], analytic.real[lo:hi])
            im_out = np.interp(t_out, t_in[lo:hi], analytic.imag[lo:hi])

            re_out, zi_re = sosfilt(sos_post, re_out, zi=zi_re)
            im_out, zi_im = sosfilt(sos_post, im_out, zi=zi_im)

            i8 = np.clip(np.round(re_out * scale), -128, 127).astype(np.int8)
            q8 = np.clip(np.round(im_out * scale), -128, 127).astype(np.int8)
            interleaved = np.empty(i8.size * 2, dtype=np.int8)
            interleaved[0::2] = i8
            interleaved[1::2] = q8
            interleaved.tofile(f)

            start_in = end_in

    file_bytes = n_out * 2
    print(f"Готово: {n_out} комплексных отсчётов, {duration:.2f} с при {sample_rate} Гц.")
    print(f"Размер файла: {file_bytes} байт (~{file_bytes/1e6:.1f} МБ).")
    return duration





def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_wav", help="Входной WAV-файл (моно)")
    ap.add_argument("--id", required=True, help="Уникальный ID сигнала в библиотеке")
    ap.add_argument("--name", required=True, help="Отображаемое имя сигнала")
    ap.add_argument("--freq", type=float, required=True,
                     help="Рекомендуемая частота передачи в эфир, Гц (например 5000000)")
    ap.add_argument("--description", default="", help="Краткое описание сигнала")
    ap.add_argument("--library", default="../library", help="Путь к папке библиотеки")
    ap.add_argument("--sample-rate", type=int, default=2_000_000,
                     help="Частота дискретизации для HackRF, Гц (по умолчанию 2000000 — "
                          "проверено, что стабильно работает; для других значений "
                          "проверьте вручную через hackrf_transfer)")
    ap.add_argument("--gain", type=float, default=0.7, help="Масштаб амплитуды 0..1")
    ap.add_argument("--lowpass", type=float, default=3500.0,
                     help="Срез ФНЧ перед преобразованием Гильберта, Гц (0 = отключить)")
    ap.add_argument("--tx-vga-gain", type=int, default=20,
                     help="TX VGA gain для hackrf_transfer, 0-47 дБ (начните с малого!)")
    ap.add_argument("--amp-enable", action="store_true",
                     help="Включить встроенный RF-усилитель HackRF (+14 дБ). Осторожно!")
    ap.add_argument("--sideband", choices=["usb", "lsb"], default="usb")
    ap.add_argument("--spectrum-freq-min-khz", type=float, default=-5.0,
                     help="Нижняя граница окна спектра в интерфейсе, кГц (по умолчанию -5). "
                          "Не обязано быть симметрично относительно 0 — например, для "
                          "сигнала с полосой 0.3-3.2 кГц уместнее что-то вроде -1..6")
    ap.add_argument("--spectrum-freq-max-khz", type=float, default=5.0,
                     help="Верхняя граница окна спектра в интерфейсе, кГц (по умолчанию +5)")
    ap.add_argument("--spectrum-db-min", type=float, default=-100.0,
                     help="Нижняя граница шкалы амплитуды на графике, дБ (по умолчанию -100)")
    ap.add_argument("--spectrum-db-max", type=float, default=0.0,
                     help="Верхняя граница шкалы амплитуды на графике, дБ (по умолчанию 0)")
    ap.add_argument("--loop", action="store_true", default=True)
    add_spec_arguments(ap)
    args = ap.parse_args()

    library_dir = Path(args.library).resolve()
    output_iq = library_dir / f"{args.id}.cs8"

    duration = convert(args.input_wav, output_iq, args.sample_rate, args.gain, args.lowpass)

    entry = {
        "id": args.id,
        "name": args.name,
        "file": output_iq.name,
        "sample_rate": args.sample_rate,
        "recommended_freq_hz": args.freq,
        "tx_vga_gain": args.tx_vga_gain,
        "amp_enable": args.amp_enable,
        "sideband": args.sideband,
        "gain": args.gain,
        "loop": args.loop,
        "spectrum_freq_min_khz": args.spectrum_freq_min_khz,
        "spectrum_freq_max_khz": args.spectrum_freq_max_khz,
        "spectrum_db_min": args.spectrum_db_min,
        "spectrum_db_max": args.spectrum_db_max,
        "description": args.description,
        "duration_sec": round(duration, 2),
        **spec_entry_from_args(args),
    }
    register_signal(library_dir, entry)
    build_cache_for_signal(entry, library_dir)


if __name__ == "__main__":
    main()
