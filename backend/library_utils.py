"""
Общие функции для чтения и обновления library.json.

Используются везде, где идёт речь о библиотеке сигналов:
- tools/wav_to_iq_library.py, tools/generate_sine.py — при добавлении сигнала
- backend/main.py — при импорте бандла через веб-интерфейс
- tools/export_bundle.py — при экспорте
"""
import json
from pathlib import Path


def load_library(library_dir: Path) -> list:
    lib_file = library_dir / "library.json"
    if not lib_file.exists():
        return []
    with open(lib_file, "r", encoding="utf-8") as f:
        return json.load(f)


def register_signal(library_dir: Path, entry: dict):
    """Добавляет запись в library.json, заменяя существующую с тем же id."""
    library_dir.mkdir(parents=True, exist_ok=True)
    signals = load_library(library_dir)
    signals = [s for s in signals if s["id"] != entry["id"]]
    signals.append(entry)
    with open(library_dir / "library.json", "w", encoding="utf-8") as f:
        json.dump(signals, f, ensure_ascii=False, indent=2)
    print(f"[library] запись '{entry['id']}' сохранена в library.json")
