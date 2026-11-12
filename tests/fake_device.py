"""Маленький «поддельный» Eltex на псевдотерминале — для проверки работы
через консольный порт настоящим netmiko, без железа (только Linux/macOS)."""
import os
import threading

try:
    import tty  # нет в Windows: там тесты с псевдотерминалом пропускаются
except ImportError:  # pragma: no cover
    tty = None


class FakeEltex:
    def __init__(self, family="mes23xx", hostname="sw1", logged_in=False):
        self.family, self.hostname = family, hostname
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
        self._stop = True
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

    def _loop(self):
        buf = ""
        while not self._stop:
            try:
                data = os.read(self.master, 1024).decode(errors="ignore")
            except OSError:
                return
            for ch in data:
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
            elif low == "terminal datadump" or low.startswith("hostname ") and self.config:
                pass
            elif low == "show running-config":
                self._w(f"\nhostname {self.hostname}\ninterface vlan 1\n ip address 10.0.0.2 255.255.255.0")
            elif low == "copy running-config startup-config" and self.family == "mes23xx":
                self._w("\nOverwrite file [startup-config].... (Y/N)[N] ?")
                self.state = "yn"
                return
            elif low in ("commit", "confirm") and self.family == "esr":
                self._w("\nConfiguration has been successfully applied")
            else:
                self._w("\n% Unrecognized command")
        self._prompt()
