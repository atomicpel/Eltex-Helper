"""Обновление ПО: каталог Eltex, архивы, TFTP-сервер и порядок действий на устройстве."""
import hashlib
import json
import re
import socket
import struct
import threading
import zipfile

import pytest

from eltex_assistant import automation, firmware
from tests.test_assistant import web  # noqa: F401 — фикстура локального сервера


# ------------------------------------------------- каталог

PRODUCT = {"name": "Сервисный маршрутизатор ESR-200", "code": "esr-200", "lifecycle": [{"name": "Осуществляется поддержка"}],
           "upload_center_files": [
               {"department": "po", "type": "version", "title": "Актуальная версия",
                "text": "<p>Версия 1.40.4</p><ul><li>Исправлен &laquo;BGP&raquo;</li></ul>"},
               {"department": "po", "type": "file", "name": "Версия ПО 1.40.4", "extension": "zip", "size": "97 Mb",
                "url": "/storage/upload_center/files/1/esr200-1.40.4-build10.zip", "upload_date": "01.10.2026"},
               {"department": "po", "type": "file", "name": "Версия ПО 1.4.4", "extension": "zip",
                "url": "/storage/upload_center/files/1/esr200-1.4.4-build5.zip"},
               {"department": "po", "type": "file", "name": "Версия ПО 1.4.4 (ESR-100)", "extension": "zip",
                "url": "/storage/upload_center/files/1/esr100-1.4.4-build5.zip"},
               {"department": "po", "type": "file", "name": "MIB 1.40.4", "extension": "zip",
                "url": "/storage/upload_center/files/1/esr-mibs-1.40.4.zip"},
               {"department": "another_po", "type": "file", "name": "ПО Wi-Fi 2.0.0", "extension": "zip",
                "url": "/storage/upload_center/files/1/esr200-wifi-2.0.0.zip"},
               {"department": "docs", "type": "file", "name": "Руководство 1.40.4", "extension": "pdf", "url": "/x.pdf"}]}


def test_catalog_picks_only_firmware_for_this_model(monkeypatch):
    def fake_get(url, **kw):
        if "/search?" in url:
            assert url.endswith("query=ESR-200")  # на сайт уходит только название модели
            return json.dumps({"data": {"catalog": {"goods": [
                {"name": "Сервисный маршрутизатор ESR-2000", "code": "esr-2000"},
                {"name": "Сервисный маршрутизатор ESR-200", "code": "esr-200"}]}}}).encode()
        assert url.endswith("/catalog/good/esr-200?upload_center=true")
        return json.dumps({"data": PRODUCT}).encode()

    monkeypatch.setattr(firmware, "_get", fake_get)
    info = firmware.available("ESR-200")
    assert [r["version"] for r in info["releases"]] == ["1.40.4", "1.4.4"]
    assert info["releases"][0]["url"] == "https://eltex.ru/storage/upload_center/files/1/esr200-1.40.4-build10.zip"
    assert "• Исправлен «BGP»" in info["notes"] and "<" not in info["notes"]
    assert info["lifecycle"] == ["Осуществляется поддержка"]


def test_catalog_unknown_model(monkeypatch):
    monkeypatch.setattr(firmware, "_get", lambda url, **kw: json.dumps({"data": {"catalog": {"goods": []}}}).encode())
    with pytest.raises(firmware.FirmwareError, match="не найдена"):
        firmware.find_product("MES2324")


def test_download_only_from_eltex(tmp_path):
    for url in ("http://eltex.ru/a.zip", "https://evil.example/a.zip", "https://eltex.ru.evil.example/a.zip"):
        with pytest.raises(firmware.FirmwareError, match="официальных"):
            firmware.download(url, tmp_path)


# ------------------------------------------------- архивы

def make_zip(path, files, md5_text=None):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
        if md5_text is not None:
            z.writestr("fw/checksum.md5", md5_text)
    return path


def md5(data):
    return hashlib.md5(data).hexdigest()


def test_unpack_checks_md5_even_when_lines_are_glued(tmp_path):
    iss, boot = b"I" * 5000, b"B" * 700
    arc = make_zip(tmp_path / "mes2400-10.4.6_R1.zip", {"fw/mes2400-1046-R1.iss": iss, "fw/mes2400-1046-R1.boot": boot,
                                                        "fw/readme.pdf": b"x", "../../evil.iss.txt": b"x"},
                   f"{md5(boot)}  mes2400-1046-R1.boot{md5(iss)}  mes2400-1046-R1.iss")
    pkg = firmware.unpack(arc, "mes24xx", log=lambda *_: None)
    assert pkg.image.name == "mes2400-1046-R1.iss" and pkg.boot.name == "mes2400-1046-R1.boot"
    assert pkg.version == "10.4.6" and sorted(pkg.md5_checked) == ["mes2400-1046-R1.boot", "mes2400-1046-R1.iss"]
    assert sorted(p.name for p in pkg.folder.iterdir()) == ["mes2400-1046-R1.boot", "mes2400-1046-R1.iss"]


def test_unpack_refuses_damaged_or_foreign_archive(tmp_path):
    arc = make_zip(tmp_path / "esr200-1.40.4-build10.zip", {"esr200-1.40.4-build10.firmware": b"data"},
                   f"{md5(b'other')}  ./esr200-1.40.4-build10.firmware\n")
    with pytest.raises(firmware.FirmwareError, match="не совпала"):
        firmware.unpack(arc, "esr", log=lambda *_: None)
    with pytest.raises(firmware.FirmwareError, match="не для этой серии"):
        firmware.unpack(arc, "mes23xx", log=lambda *_: None)
    ros = tmp_path / "mes3300-4027-4R1.ros"
    ros.write_bytes(b"x")
    assert firmware.unpack(ros, "mes23xx", log=lambda *_: None).version == "4.0.27.4"


# ------------------------------------------------- версии и план

def test_parse_version():
    assert firmware.parse_version("Active-image: flash://system/images/image1.ros\n  Version: 4.0.3\n  Commit: x\n"
                                  "Inactive-image: flash://system/images/image2.ros\n  Version: 4.0.27.4\n", "mes23xx") == "4.0.3"
    assert firmware.parse_version("Boot version:\n 1.17.3.11\nSW version:\n 1.17.3 build 11[f1e2]\nHW version:\n 1v2", "esr") == "1.17.3"
    assert firmware.parse_version("Image  Version      Date        Status      After reboot\n"
                                  "1      1.14.5 build 3   date    Not Active\n2      1.23.0 build 7   date    Active   *\n", "esr") == "1.23.0"
    assert firmware.parse_version("Hardware Version : 1v0\nFirmware Version : 10.3.6.6\n", "mes24xx") == "10.3.6.6"
    assert firmware.parse_version("нет доступа", "esr") == ""


def test_plan_blocks_what_is_not_safe():
    assert firmware.plan("MES2324", "4.0.3", "4.0.27.4")["ok"]
    assert "уже установлена" in firmware.plan("MES2324", "4.0.27.4", "4.0.27.4")["blockers"][0]
    assert firmware.plan("MES2324", "4.0.27", "4.0.27.4")["ok"]
    assert "Понижение" in firmware.plan("ESR-200", "1.40.4", "1.4.4")["blockers"][0]
    old = firmware.plan("ESR-200", "1.4.4", "1.40.4")
    assert not old["ok"] and "1.17.2" in old["blockers"][0]
    cfg = "interface gigabitethernet 1/0/1\n  vrrp ip 10.0.0.1\n  vrrp\nexit\ntunnel vti 1\n  local address 1.1.1.1\nexit\n"
    warn = " ".join(firmware.plan("ESR-200", "1.23.0", "1.40.4", cfg)["warnings"])
    assert "vrrp id" in warn and "VTI" in warn
    assert "1/0/N" in " ".join(firmware.plan("MES2408B", "10.3.6", "10.4.6")["warnings"])
    assert "1/0/N" not in " ".join(firmware.plan("MES2408B", "10.4.1", "10.4.6")["warnings"])


# ------------------------------------------------- TFTP

def tftp_get(port, name, blksize=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(5)
    req = struct.pack("!H", 1) + name.encode() + b"\0octet\0"
    if blksize:
        req += b"blksize\0" + str(blksize).encode() + b"\0tsize\0000\0"
    s.sendto(req, ("127.0.0.1", port))
    data, size, tsize = bytearray(), blksize or 512, None
    try:
        while True:
            pkt, addr = s.recvfrom(70000)
            op, block = struct.unpack("!HH", pkt[:4])
            if op == 5:
                raise OSError(pkt[4:-1].decode())
            if op == 6:
                opts = pkt[2:].split(b"\0")
                tsize = int(opts[opts.index(b"tsize") + 1])
                s.sendto(struct.pack("!HH", 4, 0), addr)
                continue
            data += pkt[4:]
            s.sendto(struct.pack("!HH", 4, block), addr)
            if len(pkt) - 4 < size:
                return bytes(data), tsize
    finally:
        s.close()


def test_tftp_serves_only_listed_files_and_survives_block_rollover(tmp_path):
    big = tmp_path / "fw.firmware"
    big.write_bytes(bytes(range(256)) * 2200)  # 563 200 байт: при блоке 8 байт это больше 65535 блоков
    small = tmp_path / "boot.boot"
    small.write_bytes(b"z" * 1024)  # кратно размеру блока: нужен завершающий пустой блок
    (tmp_path / "secret.txt").write_text("x")
    with firmware.TftpServer([big, small], host="127.0.0.1", port=0) as srv:
        data, tsize = tftp_get(srv.port, "fw.firmware", blksize=8)
        assert data == big.read_bytes() and tsize == big.stat().st_size
        assert tftp_get(srv.port, "/boot.boot")[0] == small.read_bytes()
        for _ in range(50):
            if srv.transfers["fw.firmware"]["done"]:
                break
            threading.Event().wait(0.05)
        assert srv.transfers["fw.firmware"]["done"] and srv.transfers["fw.firmware"]["client"] == "127.0.0.1"
        for name in ("secret.txt", "../secret.txt", str(tmp_path / "secret.txt")):
            with pytest.raises(OSError, match="not found"):
                tftp_get(srv.port, name)


# ------------------------------------------------- порядок действий на устройстве

class FwConn:
    """Устройство, которое по команде копирования действительно забирает файл с нашего TFTP-сервера."""
    base_prompt = "sw"

    def __init__(self, log, bootvar="", fail=""):
        self.log, self.bootvar, self.fail, self.got = log, bootvar, fail, {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def send_command(self, cmd, **kw):
        self.log.append(cmd)
        return self.bootvar

    def send_command_timing(self, cmd, **kw):
        self.log.append(cmd)
        m = re.search(r"tftp://127\.0\.0\.1:(\d+):?/(\S+)", cmd)
        if m and self.fail:
            return self.fail
        if m:
            threading.Thread(target=lambda: self.got.__setitem__(m.group(2), tftp_get(int(m.group(1)), m.group(2), 1024)[0])).start()
            return ""
        if cmd.startswith("boot system image"):
            return "Continue? (y/N): "
        if cmd == "y":
            return "Boot image set successfully.\nsw# "
        return ""

    def read_until_pattern(self, **kw):
        return "\nCopy success\nsw# "

    def read_channel(self):
        return ""


@pytest.fixture
def fw(tmp_path, monkeypatch):
    monkeypatch.setattr(firmware, "local_ip_for", lambda host: "127.0.0.1")
    monkeypatch.setattr(automation, "backup", lambda d, p, out: (tmp_path / "b.cfg", ""))
    image = tmp_path / "fw.bin"
    image.write_bytes(b"F" * 30000)
    boot = tmp_path / "fw.boot"
    boot.write_bytes(b"B" * 3000)
    return image, boot


def test_upload_mes23xx(fw, monkeypatch):
    log = []
    conn = FwConn(log, "Active-image: flash://system/images/image1.ros\n  Version: 4.0.3\n")
    monkeypatch.setattr(automation, "connect", lambda d, p: conn)
    msgs = []
    res = firmware.upload(automation.Device("sw", "10.0.0.2", "MES2324"), "pw", firmware.Package(fw[0].parent, fw[0]),
                          msgs.append, tftp_port=0, backup_dir="b")
    assert re.fullmatch(r"boot system tftp://127\.0\.0\.1:\d+/fw\.bin", log[0]) and log[-1] == "show bootvar"
    assert conn.got["fw.bin"] == fw[0].read_bytes() and "Version" in res["bootvar"]
    assert not any("reload" in c for c in log)  # перезагрузка только отдельной командой
    assert any("Резервная копия" in m for m in msgs)


def test_upload_mes24xx_sends_bootloader_first(fw, monkeypatch):
    log = []
    conn = FwConn(log)
    monkeypatch.setattr(automation, "connect", lambda d, p: conn)
    firmware.upload(automation.Device("sw", "10.0.0.2", "MES2408B"), "pw", firmware.Package(fw[0].parent, fw[0], fw[1]),
                    lambda *_: None, tftp_port=0)
    copies = [c for c in log if c.startswith("copy")]
    assert copies[0].endswith("/fw.boot boot") and copies[1].endswith("/fw.bin image")
    assert conn.got["fw.boot"] == fw[1].read_bytes()


def test_upload_esr_selects_new_image(fw, monkeypatch):
    log = []
    conn = FwConn(log, "Image  Version\n1  1.23.0 build 7  Active  *\n2  1.40.4 build 10  Not Active\n")
    monkeypatch.setattr(automation, "connect", lambda d, p: conn)
    firmware.upload(automation.Device("r1", "10.0.0.1", "ESR-200"), "pw", firmware.Package(fw[0].parent, fw[0]),
                    lambda *_: None, tftp_port=0)
    assert re.fullmatch(r"copy tftp://127\.0\.0\.1:\d+:/fw\.bin system:firmware", log[0])
    assert "boot system image-2" in log and log[log.index("boot system image-2") + 1] == "y"


def test_upload_reports_device_refusal_and_refuses_console(fw, monkeypatch):
    conn = FwConn([], fail="%Unable to copy remote file")
    monkeypatch.setattr(automation, "connect", lambda d, p: conn)
    with pytest.raises(firmware.FirmwareError, match="отказалось"):
        firmware.upload(automation.Device("sw", "10.0.0.2", "MES2408B"), "pw", firmware.Package(fw[0].parent, fw[0]),
                        lambda *_: None, tftp_port=0)
    dev = automation.Device("sw", "COM3", "MES2324", transport="serial", serial_port="COM3")
    with pytest.raises(firmware.FirmwareError, match="консольный"):
        firmware.upload(dev, "pw", firmware.Package(fw[0].parent, fw[0]), lambda *_: None)


def test_reload_saves_and_confirms(monkeypatch):
    log = []

    class Conn(FwConn):
        def send_command_timing(self, cmd, **kw):
            self.log.append(cmd)
            return "Continue ? (Y/N)[N]" if cmd == "reload" else ""

    monkeypatch.setattr(automation, "connect", lambda d, p: Conn(log))
    firmware.reload(automation.Device("sw", "10.0.0.2", "MES2408B"), "pw", log=lambda *_: None)
    assert log == ["write startup-config", "reload", "y"]


# ------------------------------------------------- страница

def test_gui_firmware_needs_confirmation(web, monkeypatch):  # noqa: F811
    target = {"name": "sw", "host": "10.0.0.2", "model": "MES2324", "password": "pw"}
    url = "https://eltex.ru/storage/upload_center/files/29/mes3300-4027-4R1.zip"
    assert web("/api/fw/upload", {"target": target, "url": url})[0] == 400
    assert web("/api/fw/upload", {"target": target, "confirm": True})[0] == 400
    assert web("/api/fw/reload", {"target": target})[0] == 400
    assert web("/api/fw/upload", {"target": dict(target, transport="serial", serial_port="COM3"), "url": url,
                                  "confirm": True})[0] == 400


def test_gui_firmware_check_and_upload(web, monkeypatch, tmp_path):  # noqa: F811
    target = {"name": "sw", "host": "10.0.0.2", "model": "MES2324", "password": "pw"}
    current = {"version": "4.0.3"}
    monkeypatch.setattr(firmware, "device_version", lambda d, p: dict(current, raw=""))
    monkeypatch.setattr(firmware, "available", lambda m: {"model": m, "product": m, "lifecycle": [], "notes": "n", "page": "p",
                        "releases": [{"name": "ПО", "version": "4.0.27.4", "url": "u", "size": "", "date": ""}]})
    _, r = web("/api/fw/check", {"target": target})
    res = web.wait(r["job"])["result"]
    assert res["current"] == "4.0.3" and res["target"] == "4.0.27.4" and res["plan"]["ok"]

    ros = tmp_path / "mes3300-4027-4R1.ros"
    ros.write_bytes(b"x")
    called = []
    monkeypatch.setattr(firmware, "upload", lambda d, p, pkg, log, backup_dir=None: called.append(pkg.image.name) or
                        {"bootvar": "ok", "server": "s"})
    _, r = web("/api/fw/upload", {"target": target, "file": str(ros), "confirm": True})
    assert web.wait(r["job"])["result"]["version"] == "4.0.27.4" and called == [ros.name]
    current["version"] = "4.0.27.4"  # то же ПО второй раз не заливаем
    _, r = web("/api/fw/upload", {"target": target, "file": str(ros), "confirm": True})
    assert "уже установлена" in web.wait(r["job"])["error"] and len(called) == 1

    def offline(m):
        raise firmware.FirmwareError("нет интернета")

    monkeypatch.setattr(firmware, "available", offline)
    _, r = web("/api/fw/check", {"target": target})
    res = web.wait(r["job"])["result"]
    assert res["site_error"] == "нет интернета" and res["current"] == "4.0.27.4"
