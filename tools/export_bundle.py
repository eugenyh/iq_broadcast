#!/usr/bin/env python3
"""
export_bundle.py

Собирает выбранные сигналы библиотеки в один .tar-файл для передачи
другому пользователю — тот импортирует его прямо через веб-интерфейс
(кнопка "Импорт"), и сигналы появятся в его библиотеке.

Кэш спектра в бандл НЕ кладём — на стороне получателя он пересчитается
автоматически при импорте (тем же кодом, что и у обычного конвертера),
поэтому не завязываемся на версию spectrum_cache.py на его машине.

Структура архива:
    manifest.json        — список метаданных сигналов (как в library.json)
    signals/<id>.cs8      — сами IQ-файлы

Использование:
    python3 export_bundle.py --ids stanag-4285 test-tone-1khz --output bundle.tar --library ../library
    python3 export_bundle.py --all --output full_export.tar --library ../library
"""
import argparse
import io
import json
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from library_utils import load_library


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", nargs="*", default=None, help="ID сигналов для экспорта")
    ap.add_argument("--all", action="store_true", help="Экспортировать всю библиотеку")
    ap.add_argument("--output", required=True, help="Путь к создаваемому .tar файлу")
    ap.add_argument("--library", default="../library", help="Путь к папке библиотеки")
    args = ap.parse_args()

    if not args.all and not args.ids:
        print("Укажите --ids <id ...> или --all")
        return

    library_dir = Path(args.library).resolve()
    signals = load_library(library_dir)

    if not args.all:
        wanted = set(args.ids)
        found = [s for s in signals if s["id"] in wanted]
        missing = wanted - {s["id"] for s in found}
        if missing:
            print(f"Не найдены в библиотеке: {', '.join(sorted(missing))}")
            return
        signals = found

    if not signals:
        print("Нечего экспортировать.")
        return

    manifest = []
    with tarfile.open(args.output, "w") as tar:
        for entry in signals:
            src_file = library_dir / entry["file"]
            if not src_file.exists():
                print(f"Пропускаю '{entry['id']}': файл {entry['file']} не найден на диске")
                continue
            tar.add(src_file, arcname=f"signals/{entry['file']}")
            manifest.append(entry)
            print(f"  + {entry['id']} ({entry['file']})")

        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))

    out_path = Path(args.output)
    print(f"\nЭкспортировано {len(manifest)} сигнал(ов) в {out_path} "
          f"({out_path.stat().st_size/1e6:.1f} МБ)")


if __name__ == "__main__":
    main()
