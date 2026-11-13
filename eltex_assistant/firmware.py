"""Обновление ПО устройств Eltex: поиск прошивки, скачивание, TFTP и загрузка на устройство.

Порядок действий взят из официальных инструкций Eltex по обновлению ПО
(MES23xx/33xx/53xx; MES14xx/24xx/3400/37xx; ESR, версия 1.40):

  1. узнать текущую версию ПО на устройстве;
  2. найти прошивку для модели в центре загрузки Eltex (eltex.ru) и скачать её;
  3. проверить контрольную сумму MD5 из архива;
  4. сделать резервную копию конфигурации;
  5. поднять на этом компьютере TFTP-сервер и дать устройству команду забрать файл;
  6. убедиться, что новый образ записан, и только по отдельному подтверждению перезагрузить.

Ничего не перезагружается без явного подтверждения человека.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import automation, knowledge

SITE = "https://eltex.ru"
API = "https://api.eltex.ru/api"
USER_AGENT = "Mozilla/5.0 (eltex-assistant)"

# Какой файл из архива что означает
IMAGE_EXT = {"mes23xx": ".ros", "mes24xx": ".iss", "esr": ".firmware"}
BOOT_EXT = {"mes24xx": ".boot"}
# Имя файла ПО для ESR зависит от модели (таблица из руководства по обновлению ESR 1.40)
ESR_PREFIX = {
    "ESR-10": "esr1x", "ESR-12V": "esr1x", "ESR-12VF": "esr1x", "ESR-14VF": "esr1x",
    "ESR-15": "esr15", "ESR-15R": "esr15", "ESR-15VF": "esr15", "ESR-20": "esr2x", "ESR-21": "esr2x",
    "ESR-30": "esr3x", "ESR-31": "esr3x", "ESR-100": "esr200", "ESR-200": "esr200",
    "ESR-1000": "esr1000", "ESR-1200": "esr1200", "ESR-1500": "esr15xx", "ESR-1511": "esr15xx",
    "ESR-1700": "esr1700",
}
ESR_MIN_AUTO = (1, 17, 2)  # с этой версии обновление «кумулятивное»: достаточно загрузить один файл ПО


class FirmwareError(RuntimeError):
    pass


def vtuple(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", version or "")[:4])


# ---------------------------------------------------------------- центр загрузки Eltex

def _get(url: str, timeout: float = 30, tries: int = 4) -> bytes:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, */*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise FirmwareError("На сайте Eltex такой страницы нет (404).") from e
            last = e
        except (OSError, urllib.error.URLError) as e:
            last = e
        time.sleep(1 + attempt)
    raise FirmwareError(f"Не удалось связаться с сайтом Eltex ({urllib.parse.urlparse(url).netloc}): {last}. "
                        "Проверьте доступ в интернет или укажите файл прошивки, скачанный вручную.")


@dataclass
class Release:
    name: str
    version: str
    url: str
    size: str = ""
    date: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "version": self.version, "url": self.url, "size": self.size, "date": self.date}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_product(model: str) -> dict:
    """Ищет модель в каталоге Eltex и возвращает её карточку вместе с файлами центра загрузки."""
    data = json.loads(_get(f"{API}/search?query={urllib.parse.quote(model)}"))["data"]
    goods = (data.get("catalog") or {}).get("goods") or []
    want = _norm(model)
    exact = [g for g in goods if _norm(str(g.get("vendor_code") or "")) == want
             or _norm(str(g.get("name") or "").split()[-1] if g.get("name") else "") == want]
    if not exact:
        names = ", ".join(str(g.get("name")) for g in goods[:5]) or "ничего"
        raise FirmwareError(f"Модель {model} не найдена в каталоге Eltex. Похожие: {names}.")
    code = exact[0]["code"]
    return json.loads(_get(f"{API}/catalog/good/{urllib.parse.quote(code)}?upload_center=true"))["data"]


def releases_from_product(product: dict, model: str) -> tuple[list[Release], str]:
    """Из карточки товара выбирает архивы с ПО (без MIB, инструкций и ПО других линеек) и описание версии."""
    family = knowledge.get_model(model).family
    prefix = ESR_PREFIX.get(knowledge.get_model(model).name) if family == "esr" else None
    out: list[Release] = []
    notes = ""
    for f in product.get("upload_center_files") or []:
        if f.get("department") != "po":
            continue
        if f.get("type") == "version":
            if f.get("title") == "Актуальная версия":
                text = re.sub(r"</(p|li|ul)>", "\n", f.get("text") or "")
                notes = html.unescape(re.sub(r"<[^>]+>", "", text.replace("<li>", "• "))).strip()
            continue
        url, name = f.get("url") or "", f.get("name") or ""
        base = url.rsplit("/", 1)[-1].lower()
        if f.get("extension") != "zip" or "mib" in base or "mib" in name.lower():
            continue
        if prefix and not base.startswith(prefix + "-"):
            continue
        m = re.search(r"(\d+\.\d+(?:\.\d+){0,2})", name) or re.search(r"(\d+\.\d+(?:\.\d+){0,2})", base)
        if not m:
            continue
        out.append(Release(name=name, version=m.group(1), url=urllib.parse.urljoin(SITE, url),
                           size=f.get("size") or "", date=f.get("upload_date") or ""))
    out.sort(key=lambda r: vtuple(r.version), reverse=True)
    return out, notes


def available(model: str) -> dict:
    product = find_product(model)
    rels, notes = releases_from_product(product, model)
    return {"model": model, "product": product.get("name") or model,
            "lifecycle": [x.get("name") for x in product.get("lifecycle") or []],
            "releases": [r.as_dict() for r in rels], "notes": notes[:3000],
            "page": f"{SITE}/download/{product.get('code')}/"}


# ---------------------------------------------------------------- скачивание и проверка

def download(url: str, folder: str | Path, log=print, progress=None) -> Path:
    """Скачивает архив в папку (с докачкой при обрыве). Уже скачанный целиком файл повторно не качает."""
    host = urllib.parse.urlparse(url).netloc
    if not url.startswith("https://") or not (host == "eltex.ru" or host.endswith(".eltex.ru")
                                              or host.endswith(".eltex-co.ru") or host == "eltex-co.ru"):
        raise FirmwareError("Прошивки скачиваются только с официальных сайтов Eltex (eltex.ru, eltex-co.ru).")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w.\-]", "_", urllib.parse.unquote(url.rsplit("/", 1)[-1]))
    target, part = folder / name, folder / (name + ".part")
    if target.exists() and zipfile.is_zipfile(target):
        log(f"Архив уже скачан: {target.name}")
        return target
    last: Exception | None = None
    for attempt in range(8):
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **({"Range": f"bytes={have}-"} if have else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if have and r.status != 206:  # сервер не умеет докачку: начинаем заново
                    have = 0
                total = have + int(r.headers.get("Content-Length") or 0)
                with open(part, "ab" if have else "wb") as f:
                    done = have
                    while True:
                        chunk = r.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
            if total and part.stat().st_size < total:
                raise OSError("соединение оборвалось")
            part.replace(target)
            log(f"Скачано: {target.name} ({target.stat().st_size // 1024 // 1024} МБ)")
            return target
        except urllib.error.HTTPError as e:
            if e.code == 416 and have:  # файл уже целиком на диске
                part.replace(target)
                return target
            last = e
        except (OSError, urllib.error.URLError) as e:
            last = e
        log(f"Обрыв при скачивании, пробую ещё раз ({attempt + 1})…")
        time.sleep(2)
    raise FirmwareError(f"Не удалось скачать {url}: {last}")


@dataclass
class Package:
    """Распакованная прошивка: файл ПО и, если есть, начальный загрузчик."""
    folder: Path
    image: Path
    boot: Path | None = None
    version: str = ""
    md5_checked: list[str] = field(default_factory=list)


def _md5(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324 — это контрольная сумма из архива производителя, не защита
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unpack(archive: str | Path, family: str, log=print) -> Package:
    """Распаковывает архив (или берёт готовый файл ПО), находит нужные файлы и сверяет MD5."""
    archive = Path(archive)
    if not archive.exists():
        raise FirmwareError(f"Файл не найден: {archive}")
    image_ext, boot_ext = IMAGE_EXT[family], BOOT_EXT.get(family)
    if archive.suffix.lower() == image_ext:
        log("Указан готовый файл ПО, контрольную сумму сверить не с чем.")
        return Package(archive.parent, archive, version=_version_from_name(archive.name))
    if not zipfile.is_zipfile(archive):
        raise FirmwareError(f"{archive.name}: это не архив zip и не файл ПО {image_ext} для этой серии.")
    folder = archive.with_suffix("")
    folder.mkdir(exist_ok=True)
    sums: dict[str, str] = {}
    files: dict[str, Path] = {}
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            base = os.path.basename(info.filename)
            if info.is_dir() or not base:
                continue
            ext = os.path.splitext(base)[1].lower()
            if ext == ".md5":
                # в файлах Eltex строки иногда слиты без перевода строки, поэтому ищем пары «сумма  имя»
                for digest, fname in re.findall(r"([0-9a-f]{32})\s+(?:\./)?([\w.\-]+?)(?=[0-9a-f]{32}\s|\s|$)",
                                                z.read(info).decode("utf-8", "replace")):
                    sums[fname] = digest
            elif ext in (image_ext, boot_ext):
                out = folder / base  # только имя файла: пути из архива не используются
                with z.open(info) as src, open(out, "wb") as dst:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        dst.write(chunk)
                files[ext] = out
    if image_ext not in files:
        raise FirmwareError(f"В архиве {archive.name} нет файла ПО {image_ext}: он не для этой серии устройств.")
    pkg = Package(folder, files[image_ext], files.get(boot_ext) if boot_ext else None,
                  version=_version_from_name(archive.name) or _version_from_name(files[image_ext].name))
    for f in (pkg.image, pkg.boot):
        if f is None:
            continue
        expected = sums.get(f.name)
        if not expected:
            log(f"Для {f.name} в архиве нет контрольной суммы.")
            continue
        if _md5(f) != expected:
            raise FirmwareError(f"Контрольная сумма {f.name} не совпала: файл повреждён при скачивании. "
                                f"Удалите {archive.name} и скачайте заново.")
        pkg.md5_checked.append(f.name)
        log(f"Контрольная сумма {f.name} совпала.")
    return pkg


def _version_from_name(name: str) -> str:
    m = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", name)
    if m:
        return m.group(1)
    m = re.search(r"-(\d)(\d)(\d{1,2})-(\d+)", name)  # mes3300-4027-4R1.ros -> 4.0.27.4
    if m:
        return ".".join(m.groups())
    m = re.search(r"-(\d)(\d)(\d{1,2})(?:-|\.)", name)  # mes3300-4027-R1 -> 4.0.27
    return ".".join(m.groups()) if m else ""


# ---------------------------------------------------------------- TFTP-сервер

class TftpServer:
    """Маленький TFTP-сервер только на чтение: отдаёт заранее перечисленные файлы и больше ничего.

    Поддерживает опции blksize/tsize/timeout и переход номера блока через 65535,
    без которого файлы больше 32 МБ (ПО ESR) по TFTP не передаются.
    """

    def __init__(self, files: list[Path], host: str = "0.0.0.0", port: int = 69, log=lambda *_: None) -> None:  # noqa: S104
        self.files = {p.name: Path(p) for p in files}
        self.log = log
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((host, port))
        except OSError as e:
            self.sock.close()
            raise FirmwareError(
                f"Не удалось занять порт TFTP {port}: {e}. Возможно, уже запущен другой TFTP-сервер "
                "или не хватает прав (в Linux порт 69 доступен только администратору).") from e
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.5)
        self.transfers: dict[str, dict] = {}  # имя файла -> {sent, size, done, error, client}
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop = True
        self._thread.join(2)
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _serve(self) -> None:
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) < 4 or struct.unpack("!H", data[:2])[0] != 1:  # только запросы на чтение
                self._error(self.sock, addr, 4, "only read requests are served")
                continue
            parts = data[2:].split(b"\0")
            name = os.path.basename(parts[0].decode("utf-8", "replace").replace("\\", "/"))
            opts = {parts[i].decode().lower(): parts[i + 1].decode() for i in range(2, len(parts) - 1, 2) if parts[i]}
            path = self.files.get(name)
            if path is None:
                self.log(f"TFTP: {addr[0]} запросил файл «{name}», которого нет в раздаче.")
                self._error(self.sock, addr, 1, "file not found")
                continue
            threading.Thread(target=self._send, args=(addr, path, opts), daemon=True).start()

    @staticmethod
    def _error(sock, addr, code: int, msg: str) -> None:
        try:
            sock.sendto(struct.pack("!HH", 5, code) + msg.encode() + b"\0", addr)
        except OSError:
            pass

    def _send(self, addr, path: Path, opts: dict) -> None:
        size = path.stat().st_size
        state = self.transfers[path.name] = {"sent": 0, "size": size, "done": False, "error": "", "client": addr[0]}
        self.log(f"TFTP: {addr[0]} забирает {path.name} ({size // 1024} КБ)…")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.sock.getsockname()[0], 0))
        blksize, timeout = 512, 3.0
        ack_opts = []
        try:
            if "blksize" in opts:
                blksize = max(8, min(int(opts["blksize"]), 1428))
                ack_opts += [b"blksize", str(blksize).encode()]
            if "tsize" in opts:
                ack_opts += [b"tsize", str(size).encode()]
            if "timeout" in opts:
                timeout = max(1.0, min(float(opts["timeout"]), 30.0))
                ack_opts += [b"timeout", opts["timeout"].encode()]
        except ValueError:
            pass
        sock.settimeout(timeout)

        def exchange(packet: bytes, want_block: int) -> bool:
            for _ in range(8):
                sock.sendto(packet, addr)
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        data, src = sock.recvfrom(1024)
                    except socket.timeout:
                        break
                    if src != addr or len(data) < 4:
                        continue
                    op, block = struct.unpack("!HH", data[:4])
                    if op == 5:
                        state["error"] = "устройство прервало передачу: " + data[4:].split(b"\0")[0].decode("utf-8", "replace")
                        return False
                    if op == 4 and block == want_block:
                        return True
            state["error"] = "устройство перестало отвечать во время передачи"
            return False

        try:
            if ack_opts and not exchange(struct.pack("!H", 6) + b"\0".join(ack_opts) + b"\0", 0):
                return
            block = 0
            with open(path, "rb") as f:
                while not self._stop:
                    chunk = f.read(blksize)
                    block = (block + 1) & 0xFFFF
                    if not exchange(struct.pack("!HH", 3, block) + chunk, block):
                        return
                    state["sent"] += len(chunk)
                    if len(chunk) < blksize:
                        state["done"] = True
                        self.log(f"TFTP: {path.name} передан полностью.")
                        return
        finally:
            if state["error"]:
                self.log(f"TFTP: {state['error']}")
            sock.close()


def local_ip_for(host: str) -> str:
    """Адрес этого компьютера в сторону устройства (его устройство и увидит как TFTP-сервер)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    except OSError as e:
        raise FirmwareError(f"Не удалось определить адрес этого компьютера для связи с {host}: {e}") from e
    finally:
        s.close()


# ---------------------------------------------------------------- работа с устройством

VERSION_CMDS = {"mes23xx": ["show version"], "mes24xx": ["show bootvar", "show system information"],
                "esr": ["show version", "show bootvar"]}


def parse_version(output: str, family: str) -> str:
    """Версия работающего сейчас ПО из вывода show version / show bootvar."""
    ver = r"(\d+\.\d+\.\d+(?:\.\d+)?)"
    if family == "esr":
        m = re.search(r"SW version:?\s*(?:\S+\s+)?" + ver + r"(?:\s+build\s+(\d+))?", output, re.I)
        if m:
            return m.group(1)
        for line in output.splitlines():  # таблица show bootvar: строка образа со статусом Active
            if re.search(r"\bActive\b", line) and "Not Active" not in line:
                m = re.search(ver, line)
                if m:
                    return m.group(1)
        return ""
    m = re.search(r"Active[- ]image[^\n]*\n\s*Version:?\s*" + ver, output, re.I)
    if m:
        return m.group(1)
    for pattern in (r"Active[^\n]*?" + ver, r"(?:Software|Firmware|SW)\s*version[^\n\d]*" + ver, r"Version[^\n\d]*" + ver):
        m = re.search(pattern, output, re.I)
        if m:
            return m.group(1)
    return ""


def device_version(dev: automation.Device, password: str) -> dict:
    with automation.connect(dev, password) as conn:
        out = "\n".join(automation.send_show(conn, c, read_timeout=60) for c in VERSION_CMDS[dev.family])
    version = parse_version(out, dev.family)
    return {"version": version, "raw": out[-3000:]}


def plan(model: str, current: str, target: str, config: str = "") -> dict:
    """Можно ли обновлять автоматически и о чём предупредить (по официальным инструкциям)."""
    family = knowledge.get_model(model).family
    cur, new = vtuple(current), vtuple(target)
    blockers, warnings = [], []
    if not cur:
        warnings.append("Не удалось определить текущую версию ПО на устройстве. Проверьте её сами командой show version.")
    elif (cur + (0,) * 4)[:4] == (new + (0,) * 4)[:4]:
        blockers.append(f"На устройстве уже установлена версия {current}.")
    elif cur > new:
        blockers.append(f"На устройстве версия {current}, она новее предлагаемой {target}. Понижение версии программа "
                        "не выполняет: старое ПО может не принять конфигурацию.")
    if family == "esr" and cur and cur < ESR_MIN_AUTO:
        blockers.append(
            f"Версия {current} старше 1.17.2. Для таких версий Eltex требует обновлять ещё и загрузчик, готовить "
            "конфигурацию, а с 1.4.3 и старше идти через промежуточную версию. Это нужно делать вручную по руководству "
            "по обновлению ПО (PDF лежит в архиве с прошивкой).")
    if family == "esr" and cur:
        if cur <= (1, 28, 2) and re.search(r"^\s*(ipv6 )?vrrp ip ", config, re.M) and \
                len(re.findall(r"^\s*(?:ipv6 )?vrrp id ", config, re.M)) < len(re.findall(r"^\s*(?:ipv6 )?vrrp ip ", config, re.M)):
            warnings.append("В конфигурации есть VRRP без «vrrp id»: после обновления настройки VRRP на таких интерфейсах "
                            "будут удалены. Сначала задайте vrrp id.")
        if cur <= (1, 24, 9):
            for block in re.findall(r"^tunnel vti \d+\n(?:[ \t]+.*\n)*", config, re.M):
                if "ip address" not in block:
                    warnings.append("Есть туннель VTI без IP-адреса: после обновления связанный с ним IPsec VPN будет "
                                    "выключен. Сначала задайте адрес на туннеле.")
                    break
        if cur <= (1, 23, 5) and "ip nhrp ipsec" in config:
            warnings.append("Маршрутизатор работает в DMVPN: с версии 1.23.6 IPsec там работает только в транспортном "
                            "режиме. Обновляйте все маршрутизаторы облака согласованно.")
    if family == "mes24xx" and cur and cur < (10, 4) <= new:
        warnings.append("При переходе с ПО 10.3.x на 10.4.x имена портов меняются с 0/N на 1/0/N. После обновления "
                        "проверьте конфигурацию портов.")
    if family == "mes24xx":
        warnings.append("Вместе с ПО обновляется начальный загрузчик, как требует инструкция Eltex для этой серии.")
    warnings.append("Во время загрузки ПО и перезагрузки нельзя выключать питание устройства.")
    return {"ok": not blockers, "blockers": blockers, "warnings": warnings}


def _wait_transfer(tftp: TftpServer, name: str, log, start_timeout: float = 90, stall_timeout: float = 120) -> None:
    """Ждёт, пока устройство заберёт файл. Истину о передаче знает наш же TFTP-сервер."""
    started = time.monotonic()
    last_sent, last_change, last_report = -1, time.monotonic(), 0.0
    while True:
        st = tftp.transfers.get(name)
        now = time.monotonic()
        if st is None:
            if now - started > start_timeout:
                raise FirmwareError(
                    "Устройство не обратилось к TFTP-серверу на этом компьютере. Проверьте, что устройство может "
                    "достучаться до компьютера (ping с устройства) и что брандмауэр Windows разрешил программе "
                    "принимать подключения (UDP, порт 69).")
        else:
            if st["error"]:
                raise FirmwareError(f"Передача {name} не удалась: {st['error']}.")
            if st["done"]:
                return
            if st["sent"] != last_sent:
                last_sent, last_change = st["sent"], now
            elif now - last_change > stall_timeout:
                raise FirmwareError(f"Передача {name} остановилась на {st['sent'] * 100 // max(st['size'], 1)} %.")
            if now - last_report > 10:
                last_report = now
                log(f"Передано {st['sent'] * 100 // max(st['size'], 1)} % файла {name}")
        time.sleep(0.3)


def _read_until_prompt(conn, timeout: float) -> str:
    """Дочитывает вывод до приглашения: после передачи устройство ещё пишет образ во флеш."""
    prompt = re.escape(conn.base_prompt)
    try:
        return conn.read_until_pattern(pattern=rf"{prompt}[^\n]*[>#]\s*$", read_timeout=timeout)
    except Exception as e:  # noqa: BLE001
        raise FirmwareError(f"Устройство не вернуло приглашение после загрузки файла: {e}") from e


FAIL = re.compile(r"(%\s?(Unable|Copied invalid|Error|Invalid|Unrecognized|Incomplete|Wrong)|copy operation (has )?failed|"
                  r"fail(ed|ure)|not enough|Timeout)", re.I)


def _copy(conn, tftp: TftpServer, cmd: str, name: str, log) -> str:
    log(f"Команда устройству: {cmd}")
    out = conn.send_command_timing(cmd, read_timeout=5, last_read=1.0, strip_prompt=False, strip_command=False)
    if re.search(r"\(y/n\)|\[y/n\]", out, re.I) and not FAIL.search(out):  # человек уже подтвердил загрузку в программе
        out += conn.send_command_timing("y", read_timeout=5, last_read=1.0, strip_prompt=False, strip_command=False)
    if FAIL.search(out):
        raise FirmwareError(f"Устройство отказалось выполнять команду:\n{out.strip()}")
    try:
        _wait_transfer(tftp, name, log)
    except FirmwareError as e:
        tail = ""
        try:
            tail = conn.read_channel()
        except Exception:  # noqa: BLE001
            pass
        raise FirmwareError(f"{e}\nОтвет устройства:\n{(out + tail).strip()[-800:]}") from e
    log("Файл передан, устройство записывает его в память. Не выключайте питание.")
    out += _read_until_prompt(conn, 900)
    if FAIL.search(out):
        raise FirmwareError(f"Устройство сообщило об ошибке при записи:\n{out.strip()[-800:]}")
    return out


def upload(dev: automation.Device, password: str, pkg: Package, log=print, tftp_port: int = 69,
           with_boot: bool = True, backup_dir: str | Path | None = None) -> dict:
    """Резервная копия, TFTP, загрузка файлов на устройство и проверка. Без перезагрузки."""
    family = dev.family
    if dev.transport == "serial":
        raise FirmwareError("Через консольный порт файл ПО не передать: устройству нужен доступ к этому компьютеру "
                            "по сети. Подключитесь по SSH или telnet.")
    if backup_dir is not None:
        path, _ = automation.backup(dev, password, backup_dir)
        log(f"Резервная копия конфигурации: {path}")
    my_ip = local_ip_for(dev.host)
    files = [pkg.image] + ([pkg.boot] if pkg.boot and with_boot else [])
    with _start_tftp(files, tftp_port, log) as tftp:
        log(f"TFTP-сервер запущен на {my_ip}:{tftp.port}")
        port = "" if tftp.port == 69 else f":{tftp.port}"
        with automation.connect(dev, password) as conn:
            if family == "mes23xx":
                _copy(conn, tftp, f"boot system tftp://{my_ip}{port}/{pkg.image.name}", pkg.image.name, log)
            elif family == "mes24xx":
                if pkg.boot and with_boot:
                    _copy(conn, tftp, f"copy tftp://{my_ip}{port}/{pkg.boot.name} boot", pkg.boot.name, log)
                    log("Начальный загрузчик обновлён.")
                _copy(conn, tftp, f"copy tftp://{my_ip}{port}/{pkg.image.name} image", pkg.image.name, log)
            else:
                _copy(conn, tftp, f"copy tftp://{my_ip}{port}:/{pkg.image.name} system:firmware", pkg.image.name, log)
                log("Выбираю новый образ для следующей загрузки (boot system)…")
                bootvar = automation.send_show(conn, "show bootvar", read_timeout=60)
                image = _esr_inactive_image(bootvar)
                out = conn.send_command_timing(f"boot system image-{image}", read_timeout=20, strip_prompt=False)
                if re.search(r"\(y/N\)", out, re.I):
                    out += conn.send_command_timing("y", read_timeout=600, last_read=8.0, strip_prompt=False)
                if "successfully" not in out.lower():
                    out += _read_until_prompt(conn, 600)
                if "successfully" not in out.lower():
                    raise FirmwareError(f"Не удалось выбрать новый образ для загрузки:\n{out.strip()[-600:]}")
            check = automation.send_show(conn, "show bootvar", read_timeout=60)
    log("Новое ПО записано. Оно начнёт работать после перезагрузки устройства.")
    return {"bootvar": check, "server": f"{my_ip}:{tftp.port}"}


def _start_tftp(files: list[Path], port: int, log) -> TftpServer:
    try:
        return TftpServer(files, port=port, log=log)
    except FirmwareError:
        if port != 69:
            raise
        # стандартный порт занят или недоступен без прав администратора: устройства Eltex умеют и нестандартный
        log("Порт TFTP 69 занять не удалось, использую порт 6969.")
        return TftpServer(files, port=6969, log=log)


def _esr_inactive_image(bootvar: str) -> int:
    for line in bootvar.splitlines():
        m = re.match(r"\s*([12])\s+\S", line)
        if m and "Not Active" in line:
            return int(m.group(1))
    raise FirmwareError(f"Не удалось понять по show bootvar, в какой образ записано новое ПО:\n{bootvar}")


def reload(dev: automation.Device, password: str, save: bool = True, log=print) -> None:
    """Перезагружает устройство. Вызывается только после явного подтверждения человеком."""
    family = dev.family
    with automation.connect(dev, password) as conn:
        if save and family != "esr":
            log("Сохраняю конфигурацию перед перезагрузкой…")
            automation.save_config(conn, family)
        cmd = "reload system" if family == "esr" else "reload"
        log(f"Команда устройству: {cmd}")
        out = conn.send_command_timing(cmd, read_timeout=10, strip_prompt=False)
        for _ in range(3):  # устройство может спросить про несохранённую конфигурацию и про саму перезагрузку
            if not re.search(r"\(y/n\)|\[y/n\]|\?\s*$", out, re.I):
                break
            try:
                out = conn.send_command_timing("y", read_timeout=10, strip_prompt=False)
            except Exception:  # noqa: BLE001 — связь оборвалась: устройство уже перезагружается
                break
    log("Устройство перезагружается. Обычно это занимает от двух до пяти минут.")


def wait_back(dev: automation.Device, password: str, log=print, timeout: float = 600, pause: float = 15) -> str:
    """Ждёт, пока устройство вернётся после перезагрузки, и возвращает версию ПО."""
    time.sleep(min(pause * 2, 30) if pause >= 1 else pause)  # дать устройству уйти в перезагрузку
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            v = device_version(dev, password)
            log(f"Устройство снова на связи, версия ПО: {v['version'] or 'не определена'}")
            return v["version"]
        except Exception as e:  # noqa: BLE001
            last = automation.friendly_error(e, dev)
            log("Устройство ещё не отвечает, жду…")
            time.sleep(pause)
    raise FirmwareError(f"Устройство не вернулось на связь за {int(timeout // 60)} мин. Последняя ошибка: {last} "
                        "Проверьте его через консольный порт.")
