"""Маленький «поддельный» Eltex на псевдотерминале — для проверки работы
через консольный порт настоящим netmiko, без железа (только Linux/macOS)."""
import os
import select
import threading

try:
    import tty  # нет в Windows: там тесты с псевдотерминалом пропускаются
except ImportError:  # pragma: no cover
    tty = None


class FakeEltex:
    MORE = "--More--"

    def __init__(self, family="mes23xx", hostname="sw1", logged_in=False, paging_off=("terminal datadump",),
                 config_lines=0):
        """paging_off — какие команды устройство понимает для отключения постраничного вывода
        (у MES24xx это не «terminal datadump»); config_lines — дополнительные строки в
        show running-config, чтобы вывод не помещался на одну страницу."""
        self.family, self.hostname = family, hostname
        self.paging_off = tuple(paging_off)
        self.paging = True
        self.pending: list[str] = []
        self.extra = [f"interface gigabitethernet 0/{i}\n description port{i}" for i in range(1, config_lines + 1)]
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        self.port = os.ttyname(self.slave)
        self.received: list[str] = []
        self.state = "exec" if logged_in else "login"
        self.config = False
        self._stop = False
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def close(self):
        # Сначала останавливаем поток и только потом закрываем дескрипторы:
        # иначе освободившийся номер дескриптора достанется новому сокету
        # (например, SSH в следующем тесте), и старый поток начнёт читать из него.
        self._stop = True
        self._t.join(timeout=2)
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def _w(self, s: str):
        try:
            os.write(self.master, s.replace("\n", "\r\n").encode())
        except OSError:
            self._stop = True

    def _prompt(self):
        if self.state == "login":
            self._w("\nUser Name:")
        elif self.state == "password":
            self._w("\nPassword:")
        else:
            self._w(f"\n{self.hostname}{'(config)' if self.config else ''}#")

    def _page(self):
        page = 20 if self.paging else len(self.pending)
        chunk, self.pending = self.pending[:page], self.pending[page:]
        self._w("\n".join(chunk))
        if self.pending:
            self._w("\n" + self.MORE)
            self.state = "more"
        else:
            self.state = "exec"
            self._prompt()

    def _loop(self):
        buf = ""
        while not self._stop:
            try:
                ready, _, _ = select.select([self.master], [], [], 0.1)
                if not ready:
                    continue
                data = os.read(self.master, 1024).decode(errors="ignore")
            except (OSError, ValueError):
                return
            for ch in data:
                if self.state == "more":
                    if ch in " \r\n":
                        self._w("\r        \r")  # как настоящий коммутатор: стираем «--More--»
                        self._page()
                    elif ch in "qQ":
                        self.pending, self.state = [], "exec"
                        self._w("\r        \r")
                        self._prompt()
                    continue
                if self.state == "yn":
                    self.received.append(ch)
                    self.state = "exec"
                    self._w(ch + "\nCopy succeeded")
                    self._prompt()
                elif ch in "\r\n":
                    line, buf = buf.strip(), ""
                    self._handle(line)
                else:
                    buf += ch
                    if self.state != "password":
                        self._w(ch)

    def _handle(self, line: str):
        if self.state == "login":
            if line:
                self.state = "password"
        elif self.state == "password":
            if line:
                self.state = "exec"
        elif line:
            self.received.append(line)
            low = line.lower()
            if low in ("configure", "configure terminal"):
                self.config = True
            elif low in ("end", "exit"):
                self.config = False
            elif low in self.paging_off:
                self.paging = False
            elif low.startswith("hostname ") and self.config:
                pass
            elif low == "show running-config":
                text = f"hostname {self.hostname}\ninterface vlan 1\n ip address 10.0.0.2 255.255.255.0"
                text += "".join("\n" + e for e in self.extra) + "\nend-of-config"
                self.pending = text.split("\n")
                self._w("\n")
                self._page()
                return
            elif low == "copy running-config startup-config" and self.family == "mes23xx":
                self._w("\nOverwrite file [startup-config].... (Y/N)[N] ?")
                self.state = "yn"
                return
            elif low in ("commit", "confirm") and self.family == "esr":
                self._w("\nConfiguration has been successfully applied")
            else:
                self._w("\n% Unrecognized command")
        self._prompt()
