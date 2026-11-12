"""Сканирование сети: поиск устройств и определение, что это за оборудование.

Для каждого адреса пробуем всё, что можно сделать без особых прав:
  * ICMP (системный ping);
  * TCP-порты SSH 22, Telnet 23, HTTP 80, HTTPS 443 и чтение баннеров/заголовков;
  * SNMP v1/v2c (sysDescr, sysName, sysObjectID) по списку community;
  * MAC-адрес из ARP-таблицы компьютера и производитель по нему.
По собранному определяем Eltex и модель, чтобы сразу добавить в инвентарь.

Сканируйте только свои сети.
"""
from __future__ import annotations

import ipaddress
import platform
import re
import socket
import ssl
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from . import knowledge

TCP_PORTS = {22: "ssh", 23: "telnet", 80: "http", 443: "https"}
# Префиксы MAC, зарегистрированные за Eltex (подсказка, не доказательство)
ELTEX_OUI = {"A8:F9:4B", "E0:D9:E3", "E8:28:C1"}
MODEL_RE = re.compile(r"\b(MES\d{4}\w*|ESR-?\d+\w*)\b", re.I)
MAX_HOSTS = 4096


@dataclass
class Host:
    ip: str
    alive: bool = False
    ping: bool = False
    ports: dict[str, bool] = field(default_factory=dict)
    ssh_banner: str = ""
    telnet_banner: str = ""
    http_title: str = ""
    http_server: str = ""
    snmp: dict[str, str] = field(default_factory=dict)
    snmp_community: str = ""
    mac: str = ""
    vendor: str = ""
    model: str = ""
    family: str = ""
    name: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def expand_targets(spec: str) -> list[str]:
    """'10.0.0.0/24', '10.0.0.5', '10.0.0.10-10.0.0.50', '10.0.0.10-50', несколько через запятую/пробел."""
    out: list[str] = []
    for part in re.split(r"[,\s]+", spec.strip()):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start = ipaddress.ip_address(a)
            end = ipaddress.ip_address(b if "." in b else a.rsplit(".", 1)[0] + "." + b)
            if int(end) < int(start):
                raise ValueError(f"Конец диапазона меньше начала: {part}")
            out += [str(ipaddress.ip_address(i)) for i in range(int(start), int(end) + 1)]
        elif "/" in part:
            net = ipaddress.ip_network(part, strict=False)
            out += [str(h) for h in net.hosts()] or [str(net.network_address)]
        else:
            out.append(str(ipaddress.ip_address(part)))
        if len(out) > MAX_HOSTS:
            raise ValueError(f"Слишком много адресов (больше {MAX_HOSTS}). Сканируйте по частям.")
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- отдельные пробы

def ping(ip: str, timeout: float = 1.0) -> bool:
    if platform.system() == "Windows":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), ip]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    # Windows возвращает 0 и при «узел недоступен» — проверяем наличие TTL в ответе
    return r.returncode == 0 and (platform.system() != "Windows" or b"TTL=" in r.stdout.upper())


def tcp_probe(ip: str, port: int, timeout: float = 1.0) -> tuple[bool, str]:
    """Открыт ли порт и что устройство говорит первым (баннер)."""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout)
            banner = b""
            if port in (22, 23):
                try:
                    banner = s.recv(256)
                except OSError:
                    pass
            return True, _clean(banner)
    except OSError:
        return False, ""


def _clean(data: bytes) -> str:
    data = re.sub(rb"\xff[\xfb-\xfe].", b"", data)  # telnet-согласование
    text = data.decode("latin-1", "ignore")
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    return re.sub(r"[^\x20-\x7e]+", " ", text).strip()[:120]


def http_probe(ip: str, port: int, timeout: float = 2.0) -> tuple[str, str]:
    """Заголовок страницы и Server для веб-интерфейса."""
    try:
        raw = socket.create_connection((ip, port), timeout=timeout)
        raw.settimeout(timeout)
        if port == 443:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # у сетевого железа самоподписанные сертификаты; мы только читаем заголовок
            try:
                ctx.minimum_version = ssl.TLSVersion.TLSv1
                ctx.set_ciphers("ALL:@SECLEVEL=0")
            except (ValueError, ssl.SSLError):
                pass
            raw = ctx.wrap_socket(raw)
        with raw as s:
            s.sendall(f"GET / HTTP/1.0\r\nHost: {ip}\r\nUser-Agent: eltex-assistant\r\n\r\n".encode())
            data = b""
            while len(data) < 16384:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
    except (OSError, ssl.SSLError):
        return "", ""
    text = data.decode("utf-8", "ignore")
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    server = re.search(r"^Server:\s*(.+)$", text, re.I | re.M)
    return (_clean(title.group(1).encode()) if title else "", server.group(1).strip()[:80] if server else "")


# ---------------------------------------------------------------- SNMP (минимальная своя реализация, без библиотек)

SNMP_OIDS = {
    "sysDescr": (1, 3, 6, 1, 2, 1, 1, 1, 0),
    "sysObjectID": (1, 3, 6, 1, 2, 1, 1, 2, 0),
    "sysName": (1, 3, 6, 1, 2, 1, 1, 5, 0),
    "sysLocation": (1, 3, 6, 1, 2, 1, 1, 6, 0),
}


def _ber(tag: int, payload: bytes) -> bytes:
    n = len(payload)
    if n < 0x80:
        return bytes([tag, n]) + payload
    ln = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(ln)]) + ln + payload


def _ber_int(v: int) -> bytes:
    return _ber(0x02, v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big", signed=True))


def _ber_oid(oid: tuple[int, ...]) -> bytes:
    body = bytes([oid[0] * 40 + oid[1]])
    for sub in oid[2:]:
        chunk = [sub & 0x7F]
        sub >>= 7
        while sub:
            chunk.append(0x80 | (sub & 0x7F))
            sub >>= 7
        body += bytes(reversed(chunk))
    return _ber(0x06, body)


def snmp_build_get(community: str, oids: list[tuple[int, ...]], request_id: int = 1, version: int = 1) -> bytes:
    """version: 0 = SNMPv1, 1 = SNMPv2c."""
    varbinds = b"".join(_ber(0x30, _ber_oid(o) + _ber(0x05, b"")) for o in oids)
    pdu = _ber(0xA0, _ber_int(request_id) + _ber_int(0) + _ber_int(0) + _ber(0x30, varbinds))
    return _ber(0x30, _ber_int(version) + _ber(0x04, community.encode()) + pdu)


def _ber_read(data: bytes, pos: int) -> tuple[int, bytes, int]:
    tag = data[pos]
    ln = data[pos + 1]
    pos += 2
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    return tag, data[pos:pos + ln], pos + ln


def _oid_decode(body: bytes) -> tuple[int, ...]:
    out = [body[0] // 40, body[0] % 40]
    v = 0
    for b in body[1:]:
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            out.append(v)
            v = 0
    return tuple(out)


def snmp_parse_response(data: bytes) -> dict[tuple[int, ...], str]:
    """Возвращает {oid: значение строкой}. Бросает ValueError на мусоре."""
    try:
        _, msg, _ = _ber_read(data, 0)
        _, _, p = _ber_read(msg, 0)          # version
        _, _, p = _ber_read(msg, p)          # community
        tag, pdu, _ = _ber_read(msg, p)
        if tag != 0xA2:
            raise ValueError("не GetResponse")
        _, _, p = _ber_read(pdu, 0)          # request-id
        _, _, p = _ber_read(pdu, p)          # error-status
        _, _, p = _ber_read(pdu, p)          # error-index
        _, vbs, _ = _ber_read(pdu, p)
        out = {}
        p = 0
        while p < len(vbs):
            _, vb, p = _ber_read(vbs, p)
            _, oid, q = _ber_read(vb, 0)
            vtag, val, _ = _ber_read(vb, q)
            if vtag == 0x04:
                text = val.decode("utf-8", "replace").strip()
            elif vtag == 0x06:
                text = ".".join(map(str, _oid_decode(val)))
            elif vtag in (0x02, 0x41, 0x42, 0x43):
                text = str(int.from_bytes(val, "big"))
            else:
                text = ""  # noSuchObject и пр.
            out[_oid_decode(oid)] = text
        return out
    except IndexError as e:
        raise ValueError("повреждённый SNMP-ответ") from e


def snmp_get(ip: str, community: str, timeout: float = 1.0, port: int = 161) -> dict[str, str]:
    oids = list(SNMP_OIDS.values())
    names = {v: k for k, v in SNMP_OIDS.items()}
    for version in (1, 0):  # сначала v2c, затем v1 (старые прошивки)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            try:
                s.sendto(snmp_build_get(community, oids, version=version), (ip, port))
                data, _ = s.recvfrom(4096)
                parsed = snmp_parse_response(data)
            except (OSError, ValueError):
                continue
            result = {names[o]: v for o, v in parsed.items() if o in names and v}
            if result:
                return result
    return {}


# ---------------------------------------------------------------- ARP

def arp_table() -> dict[str, str]:
    """IP -> MAC из ARP-кэша компьютера (работает для адресов в своей подсети)."""
    out: dict[str, str] = {}
    cmds = [["arp", "-a"]] if platform.system() == "Windows" else [["ip", "neigh"], ["arp", "-an"]]
    for cmd in cmds:
        try:
            text = subprocess.run(cmd, capture_output=True, timeout=5).stdout.decode("latin-1", "ignore")
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in text.splitlines():
            ip = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line)
            mac = re.search(r"([0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5})", line)
            if ip and mac:
                m = mac.group(1).upper().replace("-", ":")
                if m not in ("FF:FF:FF:FF:FF:FF", "00:00:00:00:00:00"):
                    out[ip.group(1)] = m
        if out:
            break
    return out


# ---------------------------------------------------------------- сборка результата

def identify(h: Host) -> None:
    """Определяет производителя/модель по всему собранному."""
    evidence = " ".join([h.snmp.get("sysDescr", ""), h.snmp.get("sysName", ""), h.http_title,
                         h.telnet_banner, h.ssh_banner, h.http_server])
    m = MODEL_RE.search(evidence)
    if m:
        raw = m.group(1).upper()
        try:
            model = knowledge.get_model(raw)
            h.model, h.family = model.name, model.family
        except knowledge.UnknownModel:
            h.model = raw  # Eltex, но модели пока нет в базе
        h.vendor = "Eltex"
    elif re.search(r"eltex", evidence, re.I) or h.snmp.get("sysObjectID", "").startswith("1.3.6.1.4.1.35265"):
        h.vendor = "Eltex"
    elif h.mac[:8] in ELTEX_OUI:
        h.vendor = "Eltex (по MAC)"
    h.name = h.snmp.get("sysName", "")


def scan_host(ip: str, communities: list[str], timeout: float = 1.0) -> Host:
    h = Host(ip=ip)
    h.ping = ping(ip, timeout)
    for port, name in TCP_PORTS.items():
        ok, banner = tcp_probe(ip, port, timeout)
        h.ports[name] = ok
        if ok and port == 22:
            h.ssh_banner = banner
        if ok and port == 23:
            h.telnet_banner = banner
    for port, name in ((80, "http"), (443, "https")):
        if h.ports.get(name) and not h.http_title:
            h.http_title, h.http_server = http_probe(ip, port, timeout + 1)
    h.alive = h.ping or any(h.ports.values())
    # SNMP пробуем и у «молчащих» адресов: устройство может отвечать только по SNMP
    for c in communities:
        res = snmp_get(ip, c, timeout)
        if res:
            h.snmp, h.snmp_community, h.alive = res, c, True
            break
    return h


def scan(spec: str, communities: list[str] | None = None, timeout: float = 1.0, workers: int = 64,
         progress=None) -> list[Host]:
    ips = expand_targets(spec)
    communities = communities or ["public"]
    done = 0

    def job(ip: str) -> Host:
        nonlocal done
        h = scan_host(ip, communities, timeout)
        done += 1
        if progress:
            progress(done, len(ips))
        return h

    with ThreadPoolExecutor(max_workers=workers) as ex:
        hosts = list(ex.map(job, ips))
    arp = arp_table()
    alive = [h for h in hosts if h.alive or h.ip in arp]
    for h in alive:
        h.mac = arp.get(h.ip, "")
        h.alive = True
        identify(h)
    return alive


def to_inventory(hosts: list[Host], username: str = "admin") -> dict:
    """Готовый инвентарь из найденных устройств Eltex с известной моделью."""
    devices = {}
    for h in hosts:
        if not h.family:
            continue
        name = re.sub(r"[^\w.-]", "-", h.name) if h.name else f"{h.model.lower()}-{h.ip.replace('.', '-')}"
        entry = {"host": h.ip, "model": h.model}
        if not h.ports.get("ssh") and h.ports.get("telnet"):
            entry["transport"] = "telnet"
        devices[name] = entry
    return {"defaults": {"username": username}, "devices": devices}
