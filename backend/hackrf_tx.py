"""
Обёртка для передачи через HackRF One с помощью штатной утилиты
hackrf_transfer.

ВАЖНО (история вопроса): изначально данные подавались через stdin
(`-t -`) потоково из Python в реальном времени — это оказалось
нестабильным на Windows (передача обрывалась через ~1 секунду с
ошибкой "streaming terminated (-1004)"), тогда как чтение готового
IQ-файла с диска через `-t <file> -R` работает стабильно сколько
угодно долго (проверено — 19+ секунд без сбоев). Поэтому здесь HackRF
читает файл библиотеки напрямую в цикле (флаг -R) — конвертер
(tools/wav_to_iq_library.py) сразу пишет файлы в нужном для HackRF
формате, никакого рендера "на лету" при воспроизведении не требуется.

Если бинарник hackrf_transfer не найден в PATH (или явно не задан
через HACKRF_TRANSFER_BIN) — класс переходит в режим симуляции,
удобный для разработки интерфейса без подключённого HackRF.
"""
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

HACKRF_TRANSFER_BIN = os.environ.get("HACKRF_TRANSFER_BIN", "hackrf_transfer")
IS_WINDOWS = sys.platform == "win32"

# hackrf_transfer в норме печатает строку статуса ("X MiB / Y sec = ...")
# примерно раз в секунду. Если дольше этого времени не было ни одной
# строки — считаем, что USB-передача заглохла, даже если сам процесс
# формально жив (poll() не отличает "работает" от "завис, но не вышел").
STALL_TIMEOUT_SEC = 5.0


def _sibling_tool(binary_path: str, tool_name: str) -> str:
    p = Path(binary_path)
    if p.parent != Path("."):
        candidate = p.parent / (tool_name + p.suffix)
        if candidate.exists():
            return str(candidate)
    return tool_name


class HackRFTransmitter:
    def __init__(self, frequency_hz, sample_rate_hz, tx_vga_gain=20,
                 amp_enable=False, loop=True, bandwidth_hz=None):
        self.frequency_hz = int(frequency_hz)
        self.sample_rate_hz = int(sample_rate_hz)
        self.tx_vga_gain = int(tx_vga_gain)
        self.amp_enable = bool(amp_enable)
        self.loop = bool(loop)
        self.bandwidth_hz = bandwidth_hz
        self.proc = None
        self.simulate = shutil.which(HACKRF_TRANSFER_BIN) is None
        self._last_activity = None
        self._recent_output = deque(maxlen=20)
        self._reader_thread = None
        # Точка отсчёта текущего прохода по файлу и его номер — берутся из
        # РЕАЛЬНОГО вывода hackrf_transfer ("Input file end reached. Rewind
        # to beginning."), а не из предположения "sample_rate постоянна и
        # время не дрейфует". Пока ни одного возврата к началу не замечено
        # (первый проход ещё не закончился, либо это симуляция без
        # реального hackrf_transfer) — остаётся None, и main.py откатывается
        # на оценку по времени с момента старта.
        self.cycle_start_time = None
        self.cycle_count = 0

    def _preflight_check(self, retries=3, delay=1.0):
        """Проверка hackrf_info из ТОГО ЖЕ комплекта, что и hackrf_transfer.
        Повторяем несколько раз с паузой: сразу после остановки предыдущей
        передачи USB-устройство может ещё не быть до конца освобождено
        системой, и первая попытка иногда получает ложный 'not found'."""
        info_bin = _sibling_tool(HACKRF_TRANSFER_BIN, "hackrf_info")
        last_output = ""
        for attempt in range(1, retries + 1):
            try:
                probe = subprocess.run([info_bin], capture_output=True, timeout=5, text=True)
                output = (probe.stdout or "") + (probe.stderr or "")
                if "Found HackRF" in output:
                    return
                last_output = output
            except FileNotFoundError:
                return  # hackrf_info не нашёлся рядом — пропускаем пре-проверку
            except subprocess.TimeoutExpired:
                last_output = "(hackrf_info завис дольше 5 секунд)"
            if attempt < retries:
                time.sleep(delay)

        raise RuntimeError(
            f"HackRF не обнаружен через '{info_bin}' после {retries} попыток.\n"
            f"Возможные причины: устройство занято другой программой, "
            f"нужно переподключить USB, либо HACKRF_TRANSFER_BIN указывает "
            f"на бинарник из другого комплекта, чем ожидалось.\n"
            f"Вывод hackrf_info:\n{last_output}"
        )

    def start(self, iq_file_path: Path):
        """Запускает hackrf_transfer, читающий готовый файл с диска в цикле (-R)."""
        if self.simulate:
            print(f"[hackrf_tx] '{HACKRF_TRANSFER_BIN}' не найден в PATH — режим симуляции. "
                  f"Задайте переменную окружения HACKRF_TRANSFER_BIN, если утилита установлена "
                  f"в нестандартном месте (например, PothosSDR на Windows).")
            return

        self._preflight_check()

        cmd = [
            HACKRF_TRANSFER_BIN,
            "-t", str(iq_file_path),
            "-f", str(self.frequency_hz),
            "-s", str(self.sample_rate_hz),
            "-x", str(self.tx_vga_gain),
            "-a", "1" if self.amp_enable else "0",
        ]
        if self.loop:
            cmd += ["-R"]  # зациклить чтение файла
        if self.bandwidth_hz:
            cmd += ["-b", str(int(self.bandwidth_hz))]

        print("[hackrf_tx] запуск:", " ".join(cmd))
        popen_kwargs = {}
        if IS_WINDOWS:
            # Своя, ОТДЕЛЬНАЯ консоль для hackrf_transfer (а не просто своя
            # группа процессов): CTRL_C_EVENT нельзя надёжно адресовать
            # процессу с CREATE_NEW_PROCESS_GROUP — такие процессы по
            # умолчанию игнорируют Ctrl+C. С собственной консолью можно
            # временно подключиться к ней из отдельного процесса-помощника
            # и послать событие именно в неё — см. _send_ctrl_c_via_console().
            # Само окно консоли при этом скрываем (SW_HIDE) — она нужна
            # технически, но визуально мешать не должна.
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            popen_kwargs["startupinfo"] = startupinfo

        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
        self._last_activity = time.monotonic()
        self.cycle_start_time = time.monotonic()
        self.cycle_count = 1
        self._reader_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader_thread.start()

        time.sleep(0.5)
        if self.proc.poll() is not None:
            raise RuntimeError(
                f"hackrf_transfer завершился сразу после запуска:\n" + "\n".join(self._recent_output)
            )

    def _read_stderr(self):
        """
        Фоновый поток: читает диагностический вывод hackrf_transfer
        построчно из stderr (а не stdout — hackrf_transfer весь свой текст,
        включая периодические строки статуса вида 'X MiB / Y sec = ...',
        печатает именно в stderr; в stdout при работе в режиме -t не пишет
        вообще ничего). Обновляет метку времени последней активности — на
        этом строится детекция 'завис, но не вышел' в check_alive().
        """
        try:
            for raw_line in iter(self.proc.stderr.readline, b""):
                line = raw_line.decode(errors="replace").rstrip()
                if line:
                    self._recent_output.append(line)
                    if "Rewind to beginning" in line:
                        # достоверный факт: hackrf_transfer реально начал
                        # новый проход по файлу именно сейчас
                        self.cycle_count += 1
                        self.cycle_start_time = time.monotonic()
                self._last_activity = time.monotonic()
        except Exception:
            pass

    def check_alive(self):
        """
        True — процесс жив и данные реально идут (есть недавняя активность в выводе).
        False — процесс завершился САМ, кодом 0 (обычное дело, если
        loop=False и файл просто доигран до конца) — это НЕ ошибка.
        Исключение — либо процесс вышел с ненулевым кодом (реальный сбой),
        либо процесс формально жив, но давно не подавал признаков жизни в
        выводе (USB-передача заглохла — proc.poll() этого не видит, только
        мониторинг вывода).
        """
        if self.simulate or self.proc is None:
            return True

        ret = self.proc.poll()
        if ret is not None:
            if ret == 0:
                return False
            tail = "\n".join(self._recent_output)
            raise RuntimeError(f"hackrf_transfer неожиданно завершился (код {ret}):\n{tail}")

        if self._last_activity is not None:
            silence = time.monotonic() - self._last_activity
            if silence > STALL_TIMEOUT_SEC:
                tail = "\n".join(self._recent_output)
                raise RuntimeError(
                    f"hackrf_transfer жив, но не подаёт признаков передачи "
                    f"уже {silence:.1f} с (USB-стрим завис). Последний вывод:\n{tail}"
                )
        return True

    def close(self):
        if self.simulate:
            return
        if self.proc is not None and self.proc.poll() is None:
            self._graceful_stop()
        print("[hackrf_tx] TX остановлен")

    def _graceful_stop(self):
        """
        Обычный terminate() на Windows — это жёсткий TerminateProcess(),
        не аналог Ctrl-C. hackrf_transfer корректно освобождает устройство
        (hackrf_stop_tx -> hackrf_close -> hackrf_exit) только получив
        настоящий CTRL_C_EVENT (проверено по логу успешного ручного теста:
        "Caught signal 0" — 0 это именно код CTRL_C_EVENT, не CTRL_BREAK).
        Без этого HackRF остаётся в состоянии "передача идёт" на уровне
        прошивки, и следующий hackrf_open() видит "not found", пока не
        переподключишь USB физически.

        Просто послать CTRL_C_EVENT нельзя: если у процесса своя группа
        (CREATE_NEW_PROCESS_GROUP), Windows такие процессы по умолчанию
        от Ctrl+C ограждает. Поэтому у hackrf_transfer своя ОТДЕЛЬНАЯ
        консоль (CREATE_NEW_CONSOLE в start()), а отправка события
        делается через отдельный вспомогательный процесс — см.
        _send_ctrl_c_via_console().

        На Linux/macOS всё проще — обычный SIGINT.
        Если ничего не сработало за отведённое время — terminate()/kill()
        как крайняя мера (тогда HackRF может потребовать переподключения).
        """
        if IS_WINDOWS:
            if self._send_ctrl_c_via_console():
                try:
                    self.proc.wait(timeout=5)
                    return
                except subprocess.TimeoutExpired:
                    pass
        else:
            try:
                self.proc.send_signal(signal.SIGINT)
                self.proc.wait(timeout=5)
                return
            except Exception:
                pass

        print("[hackrf_tx] корректная остановка не сработала, "
              "убиваю процесс принудительно — HackRF может потребовать переподключения")
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()

    def _send_ctrl_c_via_console(self) -> bool:
        """
        Посылает CTRL_C_EVENT в консоль дочернего hackrf_transfer (Windows).

        КРИТИЧНО: FreeConsole()/AttachConsole() — это вызовы уровня ВСЕГО
        процесса, а не потока. Если сделать это прямо в нашем потоке
        внутри uvicorn — на короткое время ВЕСЬ процесс (включая другие
        потоки — обработку HTTP-запросов, логирование) остаётся без
        консоли и может зависнуть. Поэтому вся эта возня выполняется в
        отдельном, коротко живущем вспомогательном процессе — он
        подключается к консоли hackrf_transfer, шлёт Ctrl+C и сразу же
        завершается, никак не трогая консоль самого uvicorn.
        """
        helper_code = (
            "import ctypes,sys\n"
            "pid=int(sys.argv[1])\n"
            "k=ctypes.windll.kernel32\n"
            "k.FreeConsole()\n"
            "ok=k.AttachConsole(pid)\n"
            "if ok:\n"
            "    k.SetConsoleCtrlHandler(None,True)\n"
            "    k.GenerateConsoleCtrlEvent(0,0)\n"
            "sys.exit(0 if ok else 1)\n"
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", helper_code, str(self.proc.pid)],
                timeout=5,
                capture_output=True,
            )
            return result.returncode == 0
        except Exception as e:
            print(f"[hackrf_tx] не удалось отправить CTRL_C_EVENT: {e}")
            return False
