"""Живой терминал, как в PuTTY: сеанс SSH, telnet или консольного порта.

Всё, что присылает устройство, сразу попадает в буфер, а страница забирает
его по мере поступления. Нажатия клавиш уходят на устройство без ожидания
конца команды, поэтому работают «?», Tab и постраничный вывод.
"""
from __future__ import annotations

import codecs
import re
import socket
import threading
import time
import uuid

from . import automation

MAX_BUFFER = 2_000_000  # символов журнала на один сеанс
IDLE_CLOSE = 120  # секунд без опроса со страницы: вкладку закрыли, сеанс больше не нужен

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
_LOGIN = re.compile(r"(user ?name|login|user)\s*:\s*$", re.I)
_PASSWORD = re.compile(r"password\s*:\s*$", re.I)


class TermError(RuntimeError):
    pass


class _SshLink:
    def __init__(self, dev, password):
        import paramiko

        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(dev.host, port=dev.port or 22, username=dev.username, password=password,
                            look_for_keys=False, allow_agent=False, timeout=15, banner_timeout=20,
                            auth_timeout=20)
        self.chan = self.client.invoke_shell(term="vt100", width=200, height=50)
        self.chan.settimeout(0.3)

    def recv(self) -> bytes | None:
        try:
            data = self.chan.recv(65536)
        except socket.timeout:
            return b""
        return data or None

    def send(self, data: bytes) -> None:
        self.chan.sendall(data)

    def close(self) -> None:
        self.client.close()


class _TelnetLink:
    """Минимальный telnet: отвечаем на согласование опций и отдаём чистый текст."""

    def __init__(self, dev, _password):
        self.sock = socket.create_connection((dev.host, dev.port or 23), timeout=15)
        self.sock.settimeout(0.3)
        self._rest = b""

    def recv(self) -> bytes | None:
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return b""
        if not data:
            return None
        data, out, reply, i = self._rest + data, bytearray(), bytearray(), 0
        self._rest = b""
        while i < len(data):
            b = data[i]
            if b != IAC:
                out.append(b)
                i += 1
                continue
            if i + 1 >= len(data):
                self._rest = data[i:]
                break
            cmd = data[i + 1]
            if cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= len(data):
                    self._rest = data[i:]
                    break
                opt = data[i + 2]
                if cmd == DO:  # сами ничего не умеем, кроме «не дожидаться конца строки»
                    reply += bytes([IAC, WILL if opt == 3 else WONT, opt])
                elif cmd == WILL:  # эхо и посимвольный режим пусть делает устройство
                    reply += bytes([IAC, DO if opt in (1, 3) else DONT, opt])
                i += 3
            elif cmd == SB:
                end = data.find(bytes([IAC, SE]), i)
                if end < 0:
                    self._rest = data[i:]
                    break
                i = end + 2
            elif cmd == IAC:
                out.append(IAC)
                i += 2
            else:
                i += 2
        if reply:
            self.sock.sendall(bytes(reply))
        return bytes(out)

    def send(self, data: bytes) -> None:
        self.sock.sendall(data.replace(b"\xff", b"\xff\xff"))

    def close(self) -> None:
        self.sock.close()


class _SerialLink:
    def __init__(self, dev, _password):
        import serial

        if not dev.serial_port:
            raise TermError("Не указан консольный порт (COM3, /dev/ttyUSB0)")
        self.ser = serial.Serial(dev.serial_port, dev.baud, timeout=0.3)
        self.ser.write(b"\r")  # разбудить консоль, чтобы она показала приглашение

    def recv(self) -> bytes | None:
        return self.ser.read(self.ser.in_waiting or 1)

    def send(self, data: bytes) -> None:
        self.ser.write(data)

    def close(self) -> None:
        self.ser.close()


LINKS = {"ssh": _SshLink, "telnet": _TelnetLink, "serial": _SerialLink}


class Session:
    def __init__(self, dev: automation.Device, password: str = "") -> None:
        if dev.transport not in LINKS:
            raise TermError(f"Неизвестный способ подключения «{dev.transport}»")
        self.id = uuid.uuid4().hex
        self.dev = dev
        self.state = "connecting"  # connecting | open | closed
        self.error = ""
        self._text = ""
        self._base = 0  # сколько символов уже выброшено из начала журнала
        self._cond = threading.Condition()
        self._link = None
        self._last_poll = time.monotonic()
        # по telnet и через консоль логин спрашивает само устройство: отвечаем один раз
        self._auto = [] if dev.transport == "ssh" else [(_LOGIN, dev.username), (_PASSWORD, password)]
        if not password:
            self._auto = []
        self._auto_until = 0.0
        threading.Thread(target=self._run, args=(password,), daemon=True).start()

    # ------------------------------------------------------------ поток чтения

    def _run(self, password: str) -> None:
        try:
            link = LINKS[self.dev.transport](self.dev, password)
        except Exception as e:  # noqa: BLE001 — показываем человеку понятную причину
            msg = str(e) if isinstance(e, TermError) else automation.friendly_error(e, self.dev)
            if type(e).__name__ == "SSHException":
                msg = f"Не удалось установить SSH-сеанс: {e}. Проверьте, что на устройстве включён SSH."
            self._finish(msg)
            return
        with self._cond:
            if self.state == "closed":  # закрыли, пока подключались
                link.close()
                return
            self._link, self.state = link, "open"
            self._cond.notify_all()
        self._auto_until = time.monotonic() + 30
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while self.state == "open":
                if time.monotonic() - self._last_poll > IDLE_CLOSE:
                    self._finish("")
                    break
                data = link.recv()
                if data is None:
                    self._finish("")
                    break
                if data:
                    self._append(decoder.decode(data))
        except Exception as e:  # noqa: BLE001
            self._finish("" if self.state == "closed" else f"Связь прервалась: {e}")
        finally:
            try:
                link.close()
            except Exception:  # noqa: BLE001
                pass

    def _append(self, text: str) -> None:
        with self._cond:
            self._text += text
            if len(self._text) > MAX_BUFFER:
                cut = len(self._text) - MAX_BUFFER // 2
                self._text, self._base = self._text[cut:], self._base + cut
            tail = self._text[-80:]
            self._cond.notify_all()
        if self._auto and time.monotonic() < self._auto_until:
            for i, (pattern, answer) in enumerate(self._auto):
                if pattern.search(tail):
                    del self._auto[i]
                    self.write(answer + "\r")
                    break

    def _finish(self, error: str) -> None:
        with self._cond:
            if self.state != "closed":
                self.state, self.error = "closed", error
            self._cond.notify_all()

    # ------------------------------------------------------------ для страницы

    def read(self, pos: int, wait: float = 15.0) -> dict:
        """Отдаёт всё, что пришло после позиции pos; если нового нет, ждёт до wait секунд."""
        self._last_poll = time.monotonic()
        deadline = time.monotonic() + wait
        with self._cond:
            while True:
                end = self._base + len(self._text)
                if pos < self._base or pos > end:
                    pos = self._base
                if pos < end or self.state == "closed":
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            data = self._text[pos - self._base:]
            self._last_poll = time.monotonic()
            return {"data": data, "pos": pos + len(data), "state": self.state, "error": self.error}

    def write(self, data: str) -> None:
        link = self._link
        if self.state != "open" or link is None:
            raise TermError("Сеанс закрыт")
        try:
            link.send(data.encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            self._finish(f"Связь прервалась: {e}")
            raise TermError("Связь с устройством прервалась") from e

    def close(self) -> None:
        self._finish("")


class Sessions:
    def __init__(self) -> None:
        self._items: dict[str, Session] = {}
        self._lock = threading.Lock()

    def open(self, dev: automation.Device, password: str) -> Session:
        s = Session(dev, password)
        with self._lock:
            for k in [k for k, v in self._items.items() if v.state == "closed"]:
                del self._items[k]
            self._items[s.id] = s
        return s

    def get(self, sid: str) -> Session:
        s = self._items.get(sid)
        if s is None:
            raise TermError("Сеанс не найден. Подключитесь заново.")
        return s

    def close_all(self) -> None:
        for s in list(self._items.values()):
            s.close()
