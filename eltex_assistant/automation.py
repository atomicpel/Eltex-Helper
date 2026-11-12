"""SSH-автоматизация на базе netmiko: выполнение команд, бэкапы, применение конфигурации.

Безопасность прежде всего:
  * push по умолчанию сначала показывает, что будет отправлено, и просит подтверждения;
  * ошибки CLI («% Unrecognized command» и т.п.) останавливают отправку;
  * на ESR используется commit с проверкой доступа по новому SSH-сеансу и
    только потом confirm — если доступ потерян, ESR сам откатит изменения.
"""
from __future__ import annotations

import datetime as dt
import difflib
import getpass
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import knowledge

CLI_ERROR = r"(% ?(Unrecognized|Invalid|Incomplete|Ambiguous|Unknown|Error|Wrong)|Syntax error|\^)"


@dataclass
class Device:
    name: str
    host: str
    model: str
    username: str = "admin"
    password: str | None = None
    port: int | None = None
    transport: str = "ssh"  # ssh | telnet | serial
    serial_port: str | None = None  # COM3, /dev/ttyUSB0
    baud: int = 115200

    @property
    def family(self) -> str:
        return knowledge.get_model(self.model).family

    @property
    def netmiko_type(self) -> str:
        return knowledge.families()[self.family]["netmiko"]


def load_inventory(path: str | Path) -> dict[str, Device]:
    """Инвентарь YAML:

    defaults: {username: admin}
    devices:
      sw-office-1: {host: 10.0.0.11, model: MES2324}
      esr-office:  {host: 10.0.0.1,  model: ESR-12V, username: netadmin}

    Пароли в файл лучше не класть: берутся из переменной окружения
    ELTEX_PASSWORD (или ELTEX_PASSWORD_<ИМЯ>), иначе спрашиваются.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults", {}) or {}
    out = {}
    for name, d in (data.get("devices") or {}).items():
        merged = {**defaults, **(d or {})}
        out[name] = Device(
            name=name,
            host=str(merged.get("host") or merged.get("serial_port") or name),
            model=str(merged["model"]),
            username=str(merged.get("username", "admin")),
            password=merged.get("password"),
            port=int(merged["port"]) if merged.get("port") else None,
            transport=str(merged.get("transport", "serial" if merged.get("serial_port") else "ssh")).lower(),
            serial_port=merged.get("serial_port"),
            baud=int(merged.get("baud", 115200)),
        )
    return out


def resolve_password(dev: Device, cache: dict[str, str]) -> str:
    if dev.password:
        return dev.password
    env_specific = "ELTEX_PASSWORD_" + re.sub(r"\W", "_", dev.name).upper()
    for var in (env_specific, "ELTEX_PASSWORD"):
        if os.environ.get(var):
            return os.environ[var]
    key = dev.username
    if key not in cache:
        cache[key] = getpass.getpass(f"Пароль для {dev.username}@{dev.host}: ")
    return cache[key]


def connect(dev: Device, password: str):
    """Подключение по SSH (по умолчанию), telnet или через консольный порт.

    У netmiko для Eltex объявлен только SSH, но те же классы умеют telnet и
    serial, если создать их напрямую с суффиксом _telnet/_serial в device_type.
    """
    from netmiko.ssh_dispatcher import CLASS_MAPPER  # импорт здесь, чтобы справочник работал и без netmiko

    cls = CLASS_MAPPER[dev.netmiko_type]
    common = dict(username=dev.username, password=password, fast_cli=False)
    if dev.transport == "serial":
        if not dev.serial_port:
            raise ValueError(f"{dev.name}: для transport: serial нужен serial_port (COM3, /dev/ttyUSB0)")

        class Console(cls):  # type: ignore[misc, valid-type]
            def serial_login(self, *args, **kwargs):
                # MES спрашивает «User Name:» (с пробелом) — штатный шаблон netmiko его не узнаёт
                return self.telnet_login(username_pattern=r"(?:user ?name|username|login|user:)")

        return Console(
            device_type=dev.netmiko_type + "_serial",
            serial_settings={"port": dev.serial_port, "baudrate": dev.baud},
            **common,
        )
    if dev.transport == "telnet":
        return cls(
            device_type=dev.netmiko_type + "_telnet", host=dev.host, port=dev.port or 23, conn_timeout=15, **common
        )
    if dev.transport != "ssh":
        raise ValueError(f"{dev.name}: неизвестный transport «{dev.transport}» (ssh, telnet, serial)")
    return cls(device_type=dev.netmiko_type, host=dev.host, port=dev.port or 22, conn_timeout=15, **common)


def friendly_error(e: Exception, dev: Device | None = None) -> str:
    """Понятное описание типичных ошибок подключения."""
    name = type(e).__name__
    where = ""
    if dev is not None:
        where = f" ({dev.serial_port if dev.transport == 'serial' else dev.host})"
    if "Authentication" in name:
        return f"Неверный логин или пароль{where}."
    if "Timeout" in name or isinstance(e, (TimeoutError, ConnectionRefusedError)):
        if dev is not None and dev.transport == "serial":
            return f"Устройство не отвечает через консольный порт{where}. Проверьте кабель, номер порта и скорость."
        return (f"Устройство не отвечает{where}. Проверьте адрес, доступность по сети и то, "
                "что на нём включён нужный способ доступа (SSH или telnet).")
    if "SerialException" in name:
        return f"Не удалось открыть консольный порт{where}: возможно, он занят другой программой или указан неверно."
    return f"{name}: {e}"


def list_serial_ports() -> list[tuple[str, str]]:
    from serial.tools import list_ports

    return [(p.device, p.description or "") for p in list_ports.comports()]


# ---------------------------------------------------------------- выполнение команд

def run_commands(dev: Device, password: str, cmds: list[str]) -> str:
    with connect(dev, password) as conn:
        parts = []
        for c in cmds:
            out = conn.send_command(c, read_timeout=60)
            parts.append(f"{conn.find_prompt()} {c}\n{out}")
        return "\n".join(parts)


def run_many(devs: list[Device], passwords: dict[str, str], cmds: list[str], workers: int = 8) -> dict[str, str]:
    def job(d: Device) -> tuple[str, str]:
        try:
            return d.name, run_commands(d, passwords[d.name], cmds)
        except Exception as e:  # noqa: BLE001 — показываем любую ошибку по устройству
            return d.name, "ОШИБКА: " + friendly_error(e, d)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(job, devs))


# ---------------------------------------------------------------- бэкапы

def backup(dev: Device, password: str, out_dir: str | Path) -> tuple[Path, str]:
    """Сохраняет running-config в out_dir/<имя>/<дата>.cfg и возвращает diff с прошлым бэкапом."""
    with connect(dev, password) as conn:
        cfg = conn.send_command("show running-config", read_timeout=120)
    folder = Path(out_dir) / dev.name
    folder.mkdir(parents=True, exist_ok=True)
    previous = sorted(folder.glob("*.cfg"))
    path = folder / (dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".cfg")
    path.write_text(cfg + "\n", encoding="utf-8")
    diff = ""
    if previous:
        old = previous[-1].read_text(encoding="utf-8").splitlines()
        diff = "\n".join(
            difflib.unified_diff(old, cfg.splitlines(), previous[-1].name, path.name, lineterm="")
        )
    return path, diff


# ---------------------------------------------------------------- применение конфигурации

def prepare_lines(text: str) -> list[str]:
    """Убирает пустые строки, комментарии и входы/выходы из config-режима
    (netmiko сам заходит в режим конфигурации)."""
    skip = {"configure", "configure terminal", "conf t", "end"}
    out = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith(("!", "#")) or s.lower() in skip:
            continue
        out.append(raw.rstrip())
    return out


def save_config(conn, family: str) -> str:
    if family == "mes23xx":
        out = conn.send_command_timing("copy running-config startup-config", read_timeout=60)
        if re.search(r"\(Y/N\)|\[N\]|\?", out):
            out += conn.send_command_timing("Y", read_timeout=60)
        return out
    if family == "mes24xx":
        return conn.send_command_timing("write startup-config", read_timeout=60)
    raise ValueError("Для ESR используйте commit/confirm")


class PushError(RuntimeError):
    pass


def push(dev: Device, password: str, config_text: str, save: bool = True, log=print) -> str:
    lines = prepare_lines(config_text)
    if not lines:
        raise PushError("Нечего отправлять")
    family = dev.family
    with connect(dev, password) as conn:
        log(f"[{dev.name}] отправляю {len(lines)} строк конфигурации…")
        try:
            out = conn.send_config_set(
                lines, error_pattern=CLI_ERROR, read_timeout=60, exit_config_mode=True
            )
        except Exception as e:  # netmiko бросает ConfigInvalidException на error_pattern
            if family == "esr":
                conn.send_command_timing("rollback")
                raise PushError(
                    f"Устройство отвергло команду, candidate-конфигурация отменена (rollback):\n{e}"
                ) from e
            raise PushError(
                "Устройство отвергло команду. Отправка остановлена; команды ДО ошибки уже применены "
                f"в running-config, но не сохранены:\n{e}"
            ) from e

        if family == "esr":
            log(f"[{dev.name}] commit…")
            out += "\n" + conn.commit()
        elif save:
            log(f"[{dev.name}] сохраняю в startup-config…")
            out += "\n" + save_config(conn, family)

    if family == "esr":
        log(f"[{dev.name}] проверяю, что доступ по SSH сохранился (новый сеанс)…")
        try:
            with connect(dev, password) as conn2:
                if save:
                    out += "\n" + conn2.send_command_timing("confirm")
                    log(f"[{dev.name}] confirm выполнен, конфигурация сохранена.")
                else:
                    log(f"[{dev.name}] confirm НЕ выполнен (--no-save): конфигурация откатится по таймеру.")
        except Exception as e:  # noqa: BLE001
            raise PushError(
                "После commit не удалось зайти на ESR заново. confirm не выполнен — маршрутизатор сам "
                f"откатит изменения по таймеру (обычно 600 с). Причина: {e}"
            ) from e
    return out
