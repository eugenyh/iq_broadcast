"""
Общее описание "паспортных" технических полей сигнала — модуляция,
скорость, FEC и т.п. Используется:
- конвертерами (tools/wav_to_iq_library.py, tools/generate_sine.py) —
  чтобы не дублировать список CLI-аргументов в двух местах;
- backend/main.py — как значения по умолчанию для полей манифеста при
  импорте бандла (MANIFEST_DEFAULTS).

Все поля необязательные и по умолчанию None — они чисто информационные,
на саму передачу сигнала никак не влияют.
"""

# (имя_поля, python-тип для argparse, CLI-флаг, текст помощи)
SPEC_FIELDS = [
    ("modulation", str, "--modulation",
     "Тип модуляции (FSK, PSK, MFSK, AFSK, GMSK и т.д.) — самое важное поле"),
    ("tone_count", int, "--tone-count",
     "Порядок модуляции / число тонов (2 = BFSK, 4, 8, 16 = MFSK16 и т.д.) — для FSK/MFSK"),
    ("baud_rate", float, "--baud-rate",
     "Символьная скорость, Бод (например 31.25, 100, 45.45)"),
    ("shift_hz", float, "--shift-hz",
     "Сдвиг/разнос тонов, Гц (например 170, 200, 15.625) — для FSK/MFSK"),
    ("bandwidth_hz", float, "--bandwidth-hz",
     "Номинальная полоса сигнала по спецификации, Гц (например ~50, 300, 500)"),
    ("bitrate_bps", float, "--bitrate-bps",
     "Фактическая скорость передачи данных, бит/с — если отличается от baud"),
    ("encoding", str, "--encoding",
     "Кодирование/алфавит (Baudot, ASCII, Varicode, тональный набор и т.д.)"),
    ("fec", str, "--fec",
     "Коррекция ошибок: есть/нет, тип (Viterbi, Reed-Solomon и т.п.)"),
    ("interleaving", str, "--interleaving",
     "Интерливинг: есть/нет (устойчивость к замираниям)"),
]

SPEC_DEFAULTS = {name: None for name, *_ in SPEC_FIELDS}


def add_spec_arguments(ap):
    """Добавляет в argparse.ArgumentParser все паспортные поля разом."""
    group = ap.add_argument_group("технический паспорт сигнала (все поля необязательные)")
    for name, type_, flag, help_text in SPEC_FIELDS:
        group.add_argument(flag, type=type_, default=None, help=help_text)


def spec_entry_from_args(args) -> dict:
    """Достаёт значения паспортных полей из argparse.Namespace в dict
    с ключами, как в library.json (совпадают с именами атрибутов)."""
    return {name: getattr(args, name) for name, *_ in SPEC_FIELDS}
