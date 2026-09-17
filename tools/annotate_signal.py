#!/usr/bin/env python3
"""
annotate_signal.py

Обновляет "паспортные" технические поля (модуляция, baud, FEC и т.п.)
уже существующей в библиотеке записи — без переконвертации IQ-файла и
без пересчёта кэша спектра (эти поля никак не влияют ни на сам файл,
ни на его спектр — только на отображение в интерфейсе).

Указывайте только те флаги, которые хотите изменить — остальные поля
записи останутся как есть.

Использование:
    python3 annotate_signal.py --id stanag-4285 \
        --modulation "PSK (BPSK/QPSK/8PSK)" --baud-rate 2400 \
        --bandwidth-hz 3000 --fec "Свёрточное (Витерби)" --interleaving "да"
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from library_utils import load_library, register_signal
from signal_spec import add_spec_arguments, spec_entry_from_args


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", required=True, help="ID сигнала в библиотеке")
    ap.add_argument("--library", default="../library", help="Путь к папке библиотеки")
    add_spec_arguments(ap)
    args = ap.parse_args()

    library_dir = Path(args.library).resolve()
    signals = load_library(library_dir)
    entry = next((s for s in signals if s["id"] == args.id), None)
    if entry is None:
        print(f"Сигнал '{args.id}' не найден в библиотеке.")
        return

    updates = {k: v for k, v in spec_entry_from_args(args).items() if v is not None}
    if not updates:
        print("Не указано ни одного поля для обновления (см. --help).")
        return

    entry.update(updates)
    register_signal(library_dir, entry)
    print(f"'{args.id}': обновлены поля {list(updates.keys())}")


if __name__ == "__main__":
    main()
