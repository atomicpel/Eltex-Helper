"""Обзор конфигурации: что на устройстве настроено и что стоит сделать.

Разбирает вывод «show running-config» и возвращает простые факты (имя, адреса,
VLAN, порты, доступ, службы) и советы со ссылками на записи справочника.
Сами пароли, ключи и строки community в обзор не попадают.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADER = re.compile(
    r"^(interface |vlan database|vlan \d|line |security zone-pair |security zone |nat |bridge |router |"
    r"ip dhcp-server pool |ip dhcp pool |username \S+$|object-group |syslog host |ntp server |tunnel |sntp$|"
    r"spanning-tree mst configuration)", re.I)
_PHYS = re.compile(r"^interface\s+(gi|te|fa|fo|tw|hu|ex)[a-z\-]*\s*([\d/]+)$", re.I)
_PHYS_RANGE = re.compile(r"^interface range\s+", re.I)


@dataclass
class Block:
    head: str
    body: list[str] = field(default_factory=list)

    def has(self, prefix: str) -> bool:
        return any(b.startswith(prefix) for b in self.body)

    def value(self, prefix: str) -> str:
        for b in self.body:
            if b.startswith(prefix):
                return b[len(prefix):].strip()
        return ""


def parse(text: str) -> tuple[list[str], list[Block]]:
    """Делит конфигурацию на одиночные строки верхнего уровня и блоки (interface, vlan, …)."""
    top: list[str] = []
    blocks: list[Block] = []
    current: Block | None = None
    indent0, indented = 0, False
    for raw in text.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not s or s.startswith(("#", "Building", "Current configuration")):
            continue
        indent = len(line) - len(line.lstrip())
        if current is not None:
            if s in ("exit", "!", "end") and indent <= indent0:
                current = None
                continue
            if indent > indent0:
                current.body.append(s)
                indented = True
                continue
            if not indented and not _HEADER.match(s):  # вывод без отступов: тело идёт до exit или «!»
                current.body.append(s)
                continue
            current = None  # блок кончился без exit
        if s in ("exit", "!", "end"):
            continue
        if _HEADER.match(s):
            current, indent0, indented = Block(s), indent, False
            blocks.append(current)
        else:
            top.append(s)
    return top, blocks


def _ranges(nums) -> str:
    """[1,2,3,5] -> «1-3, 5»."""
    out, nums = [], sorted(set(nums))
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


def _port_list(names: list[str]) -> str:
    """['gi1/0/1','gi1/0/2','te1/0/1'] -> «gi1/0/1-2, te1/0/1»."""
    groups: dict[str, list[int]] = {}
    for n in names:
        m = re.match(r"^(.*?)(\d+)$", n)
        if m:
            groups.setdefault(m.group(1), []).append(int(m.group(2)))
    return ", ".join(p + _ranges(v).replace(", ", "," ) for p, v in groups.items()) or "нет"


def _short(head: str) -> str:
    m = _PHYS.match(head)
    return (m.group(1).lower() + m.group(2)) if m else head.replace("interface ", "")


def _advice(level: str, text: str, cmd: str = "") -> dict:
    return {"level": level, "text": text, "cmd": cmd}


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} {few}"
    return f"{n} {many}"


# ------------------------------------------------------------------ коммутаторы MES

def _switch(top: list[str], blocks: list[Block], family: str) -> dict:
    sections, advice = [], []
    has = lambda prefix: any(t.startswith(prefix) for t in top)  # noqa: E731
    first = lambda prefix: next((t[len(prefix):].strip() for t in top if t.startswith(prefix)), "")  # noqa: E731

    # --- общее
    hostname = first("hostname ")
    general = [f"Имя устройства: {hostname}" if hostname else "Имя устройства не задано (стоит заводское)."]
    mgmt = []
    for b in blocks:
        m = re.match(r"interface vlan\s*(\d+)$", b.head, re.I)
        if m and b.has("ip address"):
            mgmt.append(f"VLAN {m.group(1)}: {b.value('ip address')}")
    general.append("Адреса управления: " + "; ".join(mgmt) if mgmt else "IP-адрес управления в конфигурации не найден.")
    gw = first("ip default-gateway ") or next((t.split()[-1] for t in top if t.startswith("ip route 0.0.0.0 0.0.0.0")), "")
    general.append(f"Шлюз по умолчанию: {gw}" if gw else "Шлюз по умолчанию не задан: из других сетей коммутатор недоступен.")
    sections.append({"title": "Общее", "items": general})
    if not hostname:
        advice.append(_advice("совет", "Задайте имя устройства, чтобы не путать коммутаторы между собой.", "hostname"))
    if mgmt and not gw:
        advice.append(_advice("совет", "Задайте шлюз по умолчанию, иначе управлять коммутатором можно только из его же сети.",
                              "mgmt-ip"))

    # --- VLAN
    vlans: set[int] = set()
    for b in blocks:
        lines = b.body if b.head.lower() == "vlan database" else [b.head] if re.match(r"vlan [\d,\- ]+$", b.head, re.I) else []
        for ln in lines:
            m = re.match(r"vlan ([\d,\- ]+)$", ln, re.I)
            if m:
                for part in m.group(1).replace(" ", "").split(","):
                    a, _, z = part.partition("-")
                    if a.isdigit():
                        vlans.update(range(int(a), int(z or a) + 1) if int(z or a) - int(a) < 4094 else [])
    vlan_items = [f"Создано VLAN (кроме VLAN 1): {_ranges(vlans)}" if vlans else "Дополнительных VLAN нет, все порты в VLAN 1."]

    # --- порты
    ports = [b for b in blocks if _PHYS.match(b.head)]
    modes: dict[str, list[Block]] = {"access": [], "trunk": [], "general": [], "": []}
    for b in ports:
        mode = b.value("switchport mode ").split()[0] if b.has("switchport mode ") else ""
        if not mode and b.has("switchport access vlan"):
            mode = "access"
        modes.setdefault(mode, modes[""]).append(b)
    by_vlan: dict[str, list[str]] = {}
    for b in modes["access"]:
        by_vlan.setdefault(b.value("switchport access vlan ") or "1", []).append(_short(b.head))
    for vid, names in sorted(by_vlan.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
        vlan_items.append(f"VLAN {vid} без тега (access): порты {_port_list(names)}")
    sections.append({"title": "VLAN", "items": vlan_items})

    port_items = []
    if not ports:
        port_items.append("Порты не настраивались: работают с заводскими настройками.")
    else:
        port_items.append(f"Настроено портов: {len(ports)}. Access: {len(modes['access'])}, trunk: {len(modes['trunk'])}, "
                          f"general: {len(modes['general'])}.")
    for b in (modes["trunk"] + modes["general"])[:12]:
        allowed = [re.sub(r"^add\s+", "", x.split("vlan", 1)[1].strip()) for x in b.body if "allowed vlan" in x]
        desc = b.value("description ")
        what = "trunk" if b in modes["trunk"] else "general"
        port_items.append(f"{_short(b.head)}: {what}" + (f", VLAN: {'; '.join(allowed)}" if allowed else
                          ", список VLAN не задан" if family == "mes23xx" and what == "trunk" else "")
                          + (f" ({desc})" if desc else ""))
    down = [_short(b.head) for b in ports if "shutdown" in b.body]
    if down:
        port_items.append(f"Выключены вручную (shutdown): {_port_list(down)}")
    lag = sorted({b.value("channel-group ").split()[0] for b in ports if b.has("channel-group ")})
    if lag:
        port_items.append("Агрегация каналов: группы " + ", ".join(lag))
    sections.append({"title": "Порты", "items": port_items})
    no_desc = [b for b in modes["trunk"] + modes["general"] if not b.has("description ")]
    if no_desc:
        advice.append(_advice("совет", f"У магистральных портов нет описания: {_port_list([_short(b.head) for b in no_desc])}. "
                                       "Подпишите, куда они ведут.", "port-description"))
    if family == "mes23xx":
        empty = [b for b in modes["trunk"] if not any("allowed vlan" in x for x in b.body)]
        if empty:
            advice.append(_advice("важно", f"На trunk-портах {_port_list([_short(b.head) for b in empty])} не перечислены VLAN: "
                                           "тегированный трафик через них не пойдёт.", "port-trunk"))
    if not modes["trunk"] and not modes["general"] and vlans:
        advice.append(_advice("совет", "VLAN созданы, но нет ни одного trunk-порта: наружу они не выходят.", "port-trunk"))

    # --- доступ
    access = []
    if family == "mes23xx":
        ssh = has("ip ssh server")
        telnet = not has("no ip telnet server")
        access += ["SSH включён." if ssh else "SSH выключен.", "Telnet включён (так по умолчанию)." if telnet else "Telnet выключен."]
    else:
        ssh = has("ssh enable") or not has("ssh disable")
        telnet = has("feature telnet") and not has("no feature telnet")
        access += ["SSH выключен." if has("ssh disable") else "SSH включён." if has("ssh enable")
                   else "SSH: в конфигурации не упомянут, действует значение по умолчанию (проверьте «show ip ssh»).",
                   "Telnet выключен." if has("no feature telnet") else "Telnet включён." if has("feature telnet")
                   else "Telnet: в конфигурации не упомянут, действует значение по умолчанию."]
    users = sorted({t.split()[1] for t in top if t.startswith("username ") and len(t.split()) > 1}
                   | {b.head.split()[1] for b in blocks if b.head.startswith("username ")})
    access.append("Пользователи: " + ", ".join(users) if users else "Своих пользователей в конфигурации не видно "
                  "(заводской вход admin/admin).")
    sections.append({"title": "Доступ", "items": access})
    if telnet and family == "mes23xx":
        advice.append(_advice("важно", "Telnet включён: пароли идут по сети открытым текстом. Включите SSH, проверьте вход и выключите "
                                       "telnet." if not ssh else "SSH уже работает, а telnet всё ещё включён. Выключите его.",
                              "ssh-enable"))
    elif family == "mes23xx" and not ssh:
        advice.append(_advice("важно", "Не включён ни SSH, ни telnet: удалённо на коммутатор не зайти.", "ssh-enable"))
    if not users:
        advice.append(_advice("важно" if family == "mes23xx" else "совет",
                              "В конфигурации не видно своих пользователей. Создайте пользователя с надёжным паролем "
                              "вместо заводского admin/admin.", "user-create"))
    elif users == ["admin"]:
        advice.append(_advice("совет", "Единственный пользователь admin. Убедитесь, что пароль у него не заводской.", "user-create"))

    # --- службы
    services = []
    stp_off = has("no spanning-tree") or has("shutdown spanning-tree")
    mode = first("spanning-tree mode ")
    services.append("Spanning Tree выключен." if stp_off else "Spanning Tree включён" + (f", режим {mode}." if mode else "."))
    lbd = has("loopback-detection enable")
    services.append("Обнаружение петель (loopback-detection) включено." if lbd else "Обнаружение петель (loopback-detection) не включено.")
    snooping = has("ip dhcp snooping")
    if snooping:
        services.append("DHCP snooping включён.")
    time_ok = has("sntp server") or has("ntp server") or any(b.head == "sntp" for b in blocks) or has("clock source") \
        or has("clock time source")
    services.append("Синхронизация времени настроена." if time_ok else "Синхронизация времени не настроена.")
    syslog = has("logging host") or has("logging-server") or has("logging 1") or has("logging 2")
    services.append("Журнал отправляется на syslog-сервер." if syslog else "Syslog-сервер не задан.")
    snmp = [t for t in top if t.startswith(("snmp-server community", "snmp community"))]
    services.append("SNMP настроен." if snmp or has("snmp-server server") else "SNMP не настроен.")
    lldp_off = has("no lldp run")
    if lldp_off:
        services.append("LLDP выключен.")
    sections.append({"title": "Службы и защита", "items": services})

    if stp_off and not lbd:
        advice.append(_advice("важно", "Нет защиты от петель: выключен Spanning Tree и не включён loopback-detection. "
                                       "Одна петля в кабелях положит всю сеть.", "loopback-detection"))
    elif not lbd and modes["access"]:
        advice.append(_advice("совет", "Включите loopback-detection на абонентских портах: он отключит порт, если за ним "
                                       "закольцуют кабель.", "loopback-detection"))
    if any(re.search(r"community \S*\b(public|private)\b", t) for t in snmp):
        advice.append(_advice("важно", "SNMP community стоит стандартное (public или private). Замените его своим.", "snmp"))
    if not time_ok:
        advice.append(_advice("совет", "Настройте синхронизацию времени: без неё в журнале неверные даты.", "ntp"))
    if not syslog:
        advice.append(_advice("совет", "Отправляйте журнал на syslog-сервер: после перезагрузки записи на коммутаторе пропадают.",
                              "syslog"))
    if modes["access"] and not snooping:
        advice.append(_advice("совет", "DHCP snooping защитит от чужих DHCP-серверов на абонентских портах.", "dhcp-snooping"))
    no_guard = [b for b in modes["access"] if not b.has("spanning-tree bpduguard") and not stp_off]
    if no_guard and len(no_guard) == len(modes["access"]):
        advice.append(_advice("совет", "На портах доступа нет portfast и bpduguard: порт дольше поднимается и не защищён от "
                                       "подключённого коммутатора.", "stp"))
    advice.append(_advice("совет", "Сохраните копию этой конфигурации: кнопка «Сделать бэкап» на вкладке «Устройства».",
                          "backup-config"))
    return {"sections": sections, "advice": advice}


# ------------------------------------------------------------------ маршрутизаторы ESR

def _router(top: list[str], blocks: list[Block]) -> dict:
    sections, advice = [], []
    has = lambda prefix: any(t.startswith(prefix) for t in top)  # noqa: E731
    hostname = next((t[9:] for t in top if t.startswith("hostname ")), "")
    general = [f"Имя устройства: {hostname}" if hostname else "Имя устройства не задано."]
    default = [t.split()[-1] for t in top if t.startswith("ip route 0.0.0.0/0")]
    routes = [t for t in top if t.startswith("ip route ") and not t.startswith("ip route 0.0.0.0/0")]
    dhcp_wan = any(b.has("ip address dhcp") for b in blocks)
    general.append(f"Маршрут по умолчанию: через {', '.join(default)}" if default else
                   "Маршрут по умолчанию приходит по DHCP." if dhcp_wan else "Маршрута по умолчанию нет.")
    if routes:
        general.append(f"Статических маршрутов: {len(routes)}.")
    dyn = sorted({b.head.split()[1] for b in blocks if b.head.startswith("router ")})
    if dyn:
        general.append("Динамическая маршрутизация: " + ", ".join(d.upper() for d in dyn))
    sections.append({"title": "Общее", "items": general})

    ifaces, no_zone = [], []
    for b in blocks:
        if not b.head.startswith(("interface ", "bridge ", "tunnel ")):
            continue
        addr = b.value("ip address ")
        if not addr and not b.has("security-zone") and not b.has("bridge-group"):
            continue
        name = b.head.replace("interface ", "")
        zone, desc = b.value("security-zone "), b.value("description ")
        bits = [addr or ("в bridge " + b.value("bridge-group ") if b.has("bridge-group") else "без адреса")]
        bits.append(f"зона {zone}" if zone else "межсетевой экран выключен" if b.has("ip firewall disable") else "без зоны")
        if "shutdown" in b.body:
            bits.append("выключен")
        ifaces.append(f"{name}: " + ", ".join(bits) + (f" ({desc})" if desc else ""))
        if addr and not zone and not b.has("ip firewall disable"):
            no_zone.append(name)
    sections.append({"title": "Интерфейсы", "items": ifaces or ["Интерфейсы с адресами не найдены."]})

    pairs = [b for b in blocks if b.head.startswith("security zone-pair ")]
    fw = [f"{b.head.split()[2]} → {b.head.split()[3]}: {_plural(sum(1 for x in b.body if x.startswith('rule ')), 'правило', 'правила', 'правил')}"
          for b in pairs if len(b.head.split()) >= 4]
    nat_src = any(b.head == "nat source" for b in blocks)
    nat_dst = any(b.head == "nat destination" for b in blocks)
    fw.append("Выход в интернет через NAT (source NAT) настроен." if nat_src else "Source NAT не настроен.")
    if nat_dst:
        fw.append("Есть проброс портов (destination NAT).")
    sections.append({"title": "Межсетевой экран и NAT", "items": fw})

    services = []
    pools = [b.head.split()[-1] for b in blocks if b.head.startswith("ip dhcp-server pool ")]
    services.append("DHCP-сервер: пулы " + ", ".join(pools) if pools else "DHCP-сервер не настроен.")
    ssh, telnet = has("ip ssh server"), has("ip telnet server")
    services.append(("SSH включён." if ssh else "SSH выключен.") + (" Telnet включён." if telnet else " Telnet выключен."))
    users = sorted({b.head.split()[1] for b in blocks if b.head.startswith("username ")}
                   | {t.split()[1] for t in top if t.startswith("username ") and len(t.split()) > 1})
    services.append("Пользователи: " + ", ".join(users) if users else "Своих пользователей нет: действует заводской admin.")
    ntp = has("ntp enable") and (has("ntp server") or any(b.head.startswith("ntp server") for b in blocks))
    services.append("Синхронизация времени настроена." if ntp else "Синхронизация времени не настроена.")
    syslog = any(b.head.startswith("syslog host") for b in blocks) or has("syslog host")
    services.append("Журнал отправляется на syslog-сервер." if syslog else "Syslog-сервер не задан.")
    tunnels = [b.head for b in blocks if b.head.startswith("tunnel ")]
    if tunnels:
        services.append(f"Туннели: {len(tunnels)}.")
    sections.append({"title": "Службы и доступ", "items": services})

    if no_zone:
        advice.append(_advice("важно", f"Интерфейсы с адресом, но без зоны безопасности: {', '.join(no_zone)}. "
                                       "Трафик через них блокируется.", "security-zones"))
    if not default and not dhcp_wan and not dyn:
        advice.append(_advice("важно", "Нет маршрута по умолчанию: в интернет и чужие сети маршрутизатор не попадёт.", "static-route"))
    if not nat_src and len(ifaces) > 1:
        advice.append(_advice("совет", "Если локальной сети нужен выход в интернет, настройте source NAT.", "nat-source"))
    if telnet:
        advice.append(_advice("важно", "Telnet включён: пароли идут открытым текстом. Оставьте только SSH.", "ssh-enable"))
    if not ssh and not telnet:
        advice.append(_advice("важно", "Удалённый доступ выключен: зайти можно только через консольный порт.", "ssh-enable"))
    open_any = [b for b in pairs if b.head.split()[2:4] == ["untrusted", "self"]
                and "action permit" in b.body and not any(x.startswith("match ") for x in b.body)]
    if open_any:
        advice.append(_advice("важно", "В паре зон untrusted → self есть разрешающее правило без условий: маршрутизатор "
                                       "открыт из интернета.", "security-zones"))
    if not users or users == ["admin"]:
        advice.append(_advice("совет", "Убедитесь, что у admin не заводской пароль (password), или создайте своего пользователя.",
                              "user-create"))
    if not ntp:
        advice.append(_advice("совет", "Настройте NTP: без точного времени журнал и сертификаты работают неправильно.", "ntp"))
    if not syslog:
        advice.append(_advice("совет", "Отправляйте журнал на syslog-сервер.", "syslog"))
    advice.append(_advice("совет", "Сохраните копию этой конфигурации: кнопка «Сделать бэкап» на вкладке «Устройства».",
                          "backup-config"))
    return {"sections": sections, "advice": advice}


def analyze(text: str, family: str) -> dict:
    top, blocks = parse(text)
    if len(top) + len(blocks) < 2:
        return {"sections": [], "advice": [], "empty": True}
    result = _router(top, blocks) if family == "esr" else _switch(top, blocks, family)
    order = {"важно": 0, "совет": 1}
    result["advice"].sort(key=lambda a: order.get(a["level"], 2))
    result["empty"] = False
    return result
