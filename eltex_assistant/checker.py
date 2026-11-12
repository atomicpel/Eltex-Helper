"""Проверка конфигурации/скрипта на типичные ошибки для выбранного семейства.

Ловит то, на чём чаще всего ошибаются при переходе между сериями Eltex:
не тот синтаксис сохранения, формат адресов, имена портов и т.п.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Issue:
    line_no: int
    line: str
    level: str  # "ошибка" | "внимание"
    message: str


_IP_MASK = re.compile(r"^\s*ip address \d+\.\d+\.\d+\.\d+ \d+\.\d+\.\d+\.\d+\s*$")
_IP_PREFIX = re.compile(r"^\s*ip address \d+\.\d+\.\d+\.\d+/\d+\s*$")
_PORT_3 = re.compile(r"(gigabitethernet|tengigabitethernet|gi|te)\s*\d+/\d+/\d+", re.I)
_PORT_2 = re.compile(r"(gigabitethernet|tengigabitethernet|gi|te)\s*\d+/\d+(?!/)", re.I)
_PLAIN_PASSWORD = re.compile(r"password\s+(admin|password|123456|eltex)\b", re.I)


def check(text: str, family: str) -> list[Issue]:
    issues: list[Issue] = []
    lines = text.splitlines()
    has_commit = False
    has_confirm = False
    for i, raw in enumerate(lines, 1):
        line = raw.rstrip()
        s = line.strip().lower()
        if not s or s.startswith(("!", "#")):
            continue

        def add(level: str, msg: str) -> None:
            issues.append(Issue(i, line, level, msg))

        if _PLAIN_PASSWORD.search(line):
            add("внимание", "Стандартный/слабый пароль. Задайте надёжный.")

        if family == "esr":
            if _IP_MASK.match(line):
                add("ошибка", "На ESR адрес задаётся как IP/префикс, например «ip address 192.168.1.1/24».")
            if s.startswith(("copy running-config startup-config", "write")):
                add("ошибка", "На ESR конфигурация сохраняется командами «commit» и «confirm».")
            if s.startswith("vlan database"):
                add("ошибка", "На ESR нет «vlan database»: используйте сабинтерфейсы (gi1/0/N.VLAN) или bridge.")
            if s.startswith("switchport"):
                add("внимание", "switchport на ESR есть только на части моделей с коммутаторными портами; обычно используют bridge/сабинтерфейсы.")
            if s.startswith("ip route ") and re.match(r"ip route \d+\.\d+\.\d+\.\d+ \d+\.\d+\.\d+\.\d+", s):
                add("ошибка", "На ESR маршрут пишется как «ip route 0.0.0.0/0 <шлюз>».")
            if s == "commit":
                has_commit = True
            if s == "confirm":
                has_confirm = True
            if _PORT_2.search(line) and not _PORT_3.search(line):
                add("внимание", "Порты ESR обычно называются 1/0/N.")

        elif family == "mes23xx":
            if _IP_PREFIX.match(line):
                add("ошибка", "На MES23xx адрес задаётся с маской: «ip address 192.168.1.10 255.255.255.0».")
            if s in ("commit", "confirm"):
                add("ошибка", "commit/confirm есть только на ESR. На MES23xx: «copy running-config startup-config».")
            if s.startswith("write startup-config"):
                add("ошибка", "Это синтаксис MES24xx. На MES23xx: «copy running-config startup-config».")
            if re.match(r"vlan \d", s) and not _inside(lines, i, "vlan database"):
                add("внимание", "На MES23xx VLAN создаются внутри «vlan database».")
            if _PORT_2.search(line) and not _PORT_3.search(line):
                add("ошибка", "Порты MES23xx называются 1/0/N (например gigabitethernet 1/0/1).")
            if s.startswith("security zone") or s.startswith("nat source"):
                add("ошибка", "Это команда ESR, на коммутаторе MES её нет.")

        elif family == "mes24xx":
            if _IP_PREFIX.match(line):
                add("ошибка", "На MES24xx адрес задаётся с маской.")
            if s in ("commit", "confirm"):
                add("ошибка", "commit/confirm есть только на ESR. На MES24xx: «write startup-config».")
            if s.startswith("copy running-config startup-config"):
                add("внимание", "На MES24xx сохранение обычно «write startup-config».")
            if s.startswith("vlan database"):
                add("ошибка", "На MES24xx нет «vlan database»: VLAN создаётся командой «vlan <ID>» в режиме config.")
            if _PORT_3.search(line):
                add("ошибка", "Порты MES24xx называются 0/N (например gigabitethernet 0/1).")
            if s.startswith("security zone") or s.startswith("nat source"):
                add("ошибка", "Это команда ESR, на коммутаторе MES её нет.")

    if family == "esr" and has_commit and not has_confirm:
        issues.append(Issue(0, "", "внимание", "Есть commit, но нет confirm: через таймаут конфигурация откатится."))
    return issues


def _inside(lines: list[str], line_no: int, block_start: str) -> bool:
    """Грубая проверка: была ли выше строка block_start без exit между ними."""
    for j in range(line_no - 2, -1, -1):
        s = lines[j].strip().lower()
        if s.startswith(block_start):
            return True
        if s in ("exit", "end"):
            return False
    return False
