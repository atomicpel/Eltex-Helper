import pytest

from eltex_assistant import automation, checker, knowledge, templates


def test_every_model_has_known_family_and_commands():
    fams = knowledge.families()
    for m in knowledge.models().values():
        assert m.family in fams
        assert knowledge.commands(m.family), f"нет команд для {m.family}"


@pytest.mark.parametrize("raw,expected", [
    ("mes 2324", "MES2324"), ("MES2308", "MES2308"), ("esr12v", "ESR-12V"),
    ("ESR 200", "ESR-200"), ("esr-15r", "ESR-15R"), ("mes2411x", "MES2411X"),
])
def test_model_normalization(raw, expected):
    assert knowledge.get_model(raw).name == expected


def test_unknown_model():
    with pytest.raises(knowledge.UnknownModel):
        knowledge.get_model("DES-3200")


def test_command_ids_unique_per_family():
    for fam in knowledge.families():
        ids = [c.id for c in knowledge.commands(fam)]
        assert len(ids) == len(set(ids)), fam


def test_core_tasks_exist_everywhere():
    for fam in knowledge.families():
        for cid in ("show-version", "show-running", "save-config", "hostname", "user-create", "reload"):
            assert knowledge.get_command(cid, fam), (fam, cid)


@pytest.mark.parametrize("query,family,expected", [
    ("транк", "mes23xx", "port-trunk"),
    ("сохранить", "esr", "save-config"),
    ("nat интернет", "esr", "nat-source"),
    ("найти mac", "mes23xx", "show-mac"),
    ("dhcp сервер", "esr", "dhcp-server"),
])
def test_search(query, family, expected):
    ids = [c.id for _, c in knowledge.search(query, family, limit=3)]
    assert expected in ids, ids


def _sample_params(t):
    vals = {}
    for k, spec in t.params.items():
        if spec.get("required"):
            vals[k] = {"mgmt_ip": "10.0.0.10", "gateway": "10.0.0.1", "vlan": "10", "ports": "1-8",
                       "uplink_vlans": "10,100", "ntp_server": "10.0.0.5"}.get(k, "Str0ngPass")
    if "uplink" in vals:
        vals["uplink"] = "gigabitethernet 1/0/10"
    return vals


@pytest.mark.parametrize("t", templates.list_templates(), ids=lambda t: t.name)
def test_templates_render_and_pass_checker(t):
    for fam in t.bodies:
        vals = _sample_params(t)
        text = templates.render(t.name, fam, vals)
        assert "{{" not in text and "{%" not in text
        errors = [i for i in checker.check(text, fam) if i.level == "ошибка"]
        assert not errors, (t.name, fam, errors)


def test_template_missing_required():
    with pytest.raises(templates.TemplateError):
        templates.render("esr-office", "esr", {})


def test_esr_template_wan_variants():
    dhcp = templates.render("esr-office", "esr", {"admin_password": "x"})
    assert "ip address dhcp" in dhcp and "ip route 0.0.0.0/0" not in dhcp
    static = templates.render("esr-office", "esr", {"admin_password": "x", "wan_ip": "203.0.113.2/30",
                                                    "wan_gw": "203.0.113.1"})
    assert "ip address 203.0.113.2/30" in static and "ip route 0.0.0.0/0 203.0.113.1" in static


def test_checker_cross_family():
    assert checker.check("ip address 10.0.0.1 255.255.255.0", "esr")
    assert checker.check("commit", "mes23xx")
    assert not checker.check("interface gigabitethernet 1/0/1", "mes24xx")
    assert checker.check("vlan database", "mes24xx")
    assert not checker.check("interface gigabitethernet 1/0/1\n ip address 10.0.0.1/24\ncommit\nconfirm", "esr")


@pytest.mark.parametrize("line", [
    "copy running-config startup-config", "write memory", "vlan database",
    "switchport trunk allowed vlan add 10", "ip ssh server", "ip default-gateway 10.0.0.1",
    "terminal datadump", "show mac address-table", "sntp server 10.0.0.5", "snmp-server community public ro",
    "logging host 10.0.0.9", "spanning-tree bpduguard", "spanning-tree mode rstp", "commit",
])
def test_checker_mes24xx_rejects_foreign_syntax(line):
    """Синтаксис MES23xx/ESR, которого нет в ПО 10.x (сверено с руководством 10.4.6)."""
    assert [i for i in checker.check(line, "mes24xx") if i.level == "ошибка"], line


def test_checker_mes24xx_port_naming_and_vlan_active():
    levels = lambda text: [i.level for i in checker.check(text, "mes24xx")]  # noqa: E731
    assert levels("interface gigabitethernet 0/1\n no shutdown") == ["внимание"]
    assert "ошибка" in levels("interface gigabitethernet 0/1\ninterface gigabitethernet 1/0/2")
    assert levels("vlan 10,20") == ["внимание"]
    assert levels("vlan 10,20\n vlan active") == []
    assert checker.port_styles("interface range gi 1/0/1-8\ninterface gi 0/2") == {"1/0/N", "0/N"}


def test_mes2408b_reference_matches_manual():
    assert knowledge.get_model("MES2408B").family == "mes24xx"
    assert knowledge.get_model("MES2411X").family == "mes24xx"
    by_id = {c.id: c for c in knowledge.commands("mes24xx")}
    text = lambda cid: str(by_id[cid].commands)  # noqa: E731
    assert "set cli pagination off" in text("paging")
    assert "write startup-config" in text("save-config")
    assert "vlan active" in text("vlan-create")
    assert "show system information" in text("show-version")
    assert "allowed vlan" not in text("port-trunk") or "general" in text("port-trunk")


def test_prepare_lines():
    assert automation.prepare_lines("configure\n! c\n\nhostname X\nend\n") == ["hostname X"]


# ------------------------------------------------- SSH-логика на фейковом соединении

class FakeConn:
    def __init__(self, log, fail=False):
        self.log, self.fail = log, fail

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def send_config_set(self, lines, **kw):
        if self.fail:
            raise ValueError("% Unrecognized command")
        self.log.append(("config", tuple(lines)))
        return "ok"

    def commit(self):
        self.log.append("commit")
        return "committed"

    def send_command_timing(self, cmd, **kw):
        self.log.append(cmd)
        return "Overwrite file [startup-config].... (Y/N)[N] ?" if cmd.startswith("copy") else ""


class StyleConn(FakeConn):
    """Коммутатор MES24xx с ПО 10.4.x: порты 1/0/N."""
    base_prompt = "sw"

    def send_command(self, cmd, **kw):
        self.log.append(cmd)
        return "Port      Status  Duplex Speed\nGi1/0/1   up      Full   1000\nGi1/0/2   down    -      -\n"


def test_push_mes24xx_stops_on_wrong_port_naming(monkeypatch):
    log = []
    monkeypatch.setattr(automation, "connect", lambda d, p: StyleConn(log))
    dev = automation.Device("sw", "h", "MES2408B")
    with pytest.raises(automation.PushError, match="1/0/N"):
        automation.push(dev, "pw", "interface gigabitethernet 0/3\n shutdown", log=lambda *_: None)
    assert not any(isinstance(x, tuple) for x in log)  # в конфигурацию ничего не ушло
    automation.push(dev, "pw", "interface gigabitethernet 1/0/3\n shutdown", log=lambda *_: None)
    assert log[-1] == "write startup-config"


def test_push_mes23xx_saves(monkeypatch):
    log = []
    monkeypatch.setattr(automation, "connect", lambda d, p: FakeConn(log))
    dev = automation.Device("sw", "1.1.1.1", "MES2324")
    automation.push(dev, "pw", "configure\nhostname A\nend", log=lambda *_: None)
    assert log == [("config", ("hostname A",)), "copy running-config startup-config", "Y"]


def test_push_mes24xx_saves(monkeypatch):
    log = []
    monkeypatch.setattr(automation, "connect", lambda d, p: FakeConn(log))
    automation.push(automation.Device("sw", "h", "MES2428"), "pw", "hostname A", log=lambda *_: None)
    assert log[-1] == "write startup-config"


def test_push_esr_commit_then_confirm_in_new_session(monkeypatch):
    log, sessions = [], []

    def fake_connect(d, p):
        sessions.append(1)
        return FakeConn(log)

    monkeypatch.setattr(automation, "connect", fake_connect)
    automation.push(automation.Device("r", "h", "ESR-12V"), "pw", "hostname R", log=lambda *_: None)
    assert log == [("config", ("hostname R",)), "commit", "confirm"]
    assert len(sessions) == 2


def test_push_esr_no_confirm_if_access_lost(monkeypatch):
    log, calls = [], []

    def fake_connect(d, p):
        calls.append(1)
        if len(calls) > 1:
            raise TimeoutError("нет ответа")
        return FakeConn(log)

    monkeypatch.setattr(automation, "connect", fake_connect)
    with pytest.raises(automation.PushError, match="откатит"):
        automation.push(automation.Device("r", "h", "ESR-200"), "pw", "hostname R", log=lambda *_: None)
    assert "confirm" not in log


def test_push_esr_rollback_on_cli_error(monkeypatch):
    log = []
    monkeypatch.setattr(automation, "connect", lambda d, p: FakeConn(log, fail=True))
    with pytest.raises(automation.PushError, match="rollback"):
        automation.push(automation.Device("r", "h", "ESR-15R"), "pw", "bad cmd", log=lambda *_: None)
    assert log == ["rollback"] and "commit" not in log


def test_inventory(tmp_path, monkeypatch):
    p = tmp_path / "inv.yaml"
    p.write_text("defaults: {username: u}\ndevices:\n  a: {host: 1.1.1.1, model: ESR-200}\n", encoding="utf-8")
    inv = automation.load_inventory(p)
    assert inv["a"].username == "u" and inv["a"].netmiko_type == "eltex_esr"
    monkeypatch.setenv("ELTEX_PASSWORD_A", "secret")
    assert automation.resolve_password(inv["a"], {}) == "secret"


# ------------------------------------------------- консольный порт: настоящий netmiko + фейковое устройство на pty

import sys

from tests.fake_device import FakeEltex

needs_pty = pytest.mark.skipif(sys.platform == "win32", reason="pty есть только на Unix")


@pytest.fixture
def serial_dev(monkeypatch):
    import netmiko.base_connection as bc

    monkeypatch.setattr(bc, "check_serial_port", lambda name: name)  # pty нет в списке COM-портов
    created = []

    def make(family="mes23xx", model="MES2324", **kw):  # noqa: ANN001
        fake = FakeEltex(family=family, **kw)
        created.append(fake)
        return fake, automation.Device("con", "console", model, transport="serial", serial_port=fake.port)

    yield make
    for f in created:
        f.close()


@needs_pty
def test_serial_login_and_show(serial_dev):
    fake, dev = serial_dev()
    out = automation.run_commands(dev, "pw", ["show running-config"])
    assert "ip address 10.0.0.2 255.255.255.0" in out
    assert "terminal datadump" in fake.received


@needs_pty
def test_serial_already_logged_in(serial_dev):
    fake, dev = serial_dev(logged_in=True)
    assert "hostname sw1" in automation.run_commands(dev, "pw", ["show running-config"])


@needs_pty
def test_serial_push_mes_saves_with_y(serial_dev):
    fake, dev = serial_dev()
    automation.push(dev, "pw", "configure\nhostname NEW\nend", log=lambda *_: None)
    r = fake.received
    i_cfg, i_copy = r.index("hostname NEW"), r.index("copy running-config startup-config")
    assert i_cfg < i_copy and r[i_copy + 1] == "Y"  # сначала конфигурация, потом сохранение с ответом Y


@needs_pty
def test_serial_push_stops_on_cli_error(serial_dev):
    fake, dev = serial_dev()
    with pytest.raises(automation.PushError):
        automation.push(dev, "pw", "bogus command\nhostname X", log=lambda *_: None)
    assert "hostname X" not in fake.received
    assert "copy running-config startup-config" not in fake.received


@needs_pty
def test_serial_push_esr_commit_confirm(serial_dev):
    fake, dev = serial_dev(family="esr", model="ESR-12V", hostname="esr")
    automation.push(dev, "pw", "hostname R1", log=lambda *_: None)
    r = [c for c in fake.received if c != "exit"]  # «exit» при отключении может прийти, а может не успеть
    assert r.index("hostname R1") < r.index("commit") < r.index("confirm")


# ------------------------------------------------- сканер

import socket
import threading

from eltex_assistant import scan


def test_expand_targets():
    assert scan.expand_targets("10.0.0.1-3") == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    assert scan.expand_targets("10.0.0.9-10.0.0.10, 10.0.0.9") == ["10.0.0.9", "10.0.0.10"]
    assert len(scan.expand_targets("192.168.1.0/24")) == 254
    with pytest.raises(ValueError):
        scan.expand_targets("10.0.0.0/8")
    with pytest.raises(ValueError):
        scan.expand_targets("не адрес")


def _snmp_response(values):
    vbs = b"".join(
        scan._ber(0x30, scan._ber_oid(oid) + scan._ber(0x04, v.encode())) for oid, v in values.items()
    )
    pdu = scan._ber(0xA2, scan._ber_int(1) + scan._ber_int(0) + scan._ber_int(0) + scan._ber(0x30, vbs))
    return scan._ber(0x30, scan._ber_int(1) + scan._ber(0x04, b"public") + pdu)


def test_snmp_get_against_fake_agent():
    long_descr = "Eltex MES2324 28-port 1G/10G Managed Switch " + "x" * 200  # длина > 127: длинная форма BER
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def serve():
        data, addr = srv.recvfrom(4096)
        assert b"secret" in data
        srv.sendto(_snmp_response({scan.SNMP_OIDS["sysDescr"]: long_descr,
                                   scan.SNMP_OIDS["sysName"]: "sw-floor1"}), addr)

    threading.Thread(target=serve, daemon=True).start()
    res = scan.snmp_get("127.0.0.1", "secret", timeout=2, port=port)
    srv.close()
    assert res == {"sysDescr": long_descr, "sysName": "sw-floor1"}


def test_snmp_garbage_is_rejected():
    with pytest.raises(ValueError):
        scan.snmp_parse_response(b"\x30\x05abc")


def test_tcp_banner():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        c, _ = srv.accept()
        c.sendall(b"\xff\xfb\x01\xff\xfb\x03\r\nUser Name:")
        c.close()

    threading.Thread(target=serve, daemon=True).start()
    ok, _ = scan.tcp_probe("127.0.0.1", port, 1)
    assert ok
    srv.close()
    assert scan._clean(b"\xff\xfb\x01\xff\xfb\x03\r\nUser Name:") == "User Name:"
    assert scan.tcp_probe("127.0.0.1", 1, 0.3) == (False, "")


def test_identify_and_inventory():
    a = scan.Host("10.0.0.11", snmp={"sysDescr": "MES2324 28-port", "sysName": "sw floor1"}, ports={"ssh": True})
    b = scan.Host("10.0.0.1", telnet_banner="ESR-12V login:", ports={"ssh": False, "telnet": True})
    c = scan.Host("10.0.0.50", mac="A8:F9:4B:00:11:22")
    d = scan.Host("10.0.0.60", snmp={"sysDescr": "Linux server"})
    e = scan.Host("10.0.0.70", http_title="MES5448 web")
    for h in (a, b, c, d, e):
        scan.identify(h)
    assert (a.vendor, a.model, a.family) == ("Eltex", "MES2324", "mes23xx")
    assert (b.model, b.family) == ("ESR-12V", "esr")
    assert c.vendor.startswith("Eltex") and not c.model
    assert not d.vendor
    assert e.vendor == "Eltex" and e.model == "MES5448" and not e.family  # Eltex, но модели нет в базе
    inv = scan.to_inventory([a, b, c, d, e])
    assert inv["devices"] == {
        "sw-floor1": {"host": "10.0.0.11", "model": "MES2324"},
        "esr-12v-10-0-0-1": {"host": "10.0.0.1", "model": "ESR-12V", "transport": "telnet"},
    }


# ------------------------------------------------- графический интерфейс (API)

import json
import time
import urllib.error
import urllib.request

from eltex_assistant import gui


@pytest.fixture
def web(tmp_path):
    httpd, app = gui.make_server(0, tmp_path / "inventory.yaml")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None, token=app.token, host=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None)
        req.add_header("X-Token", token)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait(job_id):
        for _ in range(100):
            _, j = call(f"/api/job?id={job_id}")
            if j["done"]:
                return j
            time.sleep(0.1)
        raise AssertionError("операция не завершилась")

    call.wait, call.app, call.base = wait, app, base
    yield call
    httpd.shutdown()
    httpd.server_close()


def test_gui_requires_token_and_local_host(web):
    assert web("/api/meta", token="wrong")[0] == 403
    assert web("/api/meta", token="")[0] == 403
    assert web("/api/meta", host="evil.example:80")[0] == 403
    code, meta = web("/api/meta")
    assert code == 200 and {m["name"] for m in meta["models"]} >= {"MES2324", "ESR-200"}
    page = urllib.request.urlopen(web.base + "/").read().decode()
    assert "Помощник Eltex" in page and web.app.token not in page


def test_gui_search_render_check(web):
    _, r = web("/api/search?q=%D1%82%D1%80%D0%B0%D0%BD%D0%BA&model=MES2324")
    assert r["items"][0]["id"] == "port-trunk"
    code, r = web("/api/render", {"name": "esr-office", "model": "ESR-12V", "params": {"admin_password": "Xy9!"}})
    assert code == 200 and "nat source" in r["text"] and not [i for i in r["issues"] if i["level"] == "ошибка"]
    assert web("/api/render", {"name": "esr-office", "model": "ESR-12V", "params": {}})[0] == 400
    _, r = web("/api/check", {"text": "commit", "model": "MES2308"})
    assert r["issues"][0]["level"] == "ошибка"


def test_gui_inventory_never_returns_passwords(web):
    web.app.inventory_path.write_text(
        "devices:\n  r1: {host: 10.0.0.1, model: ESR-200, password: topsecret}\n", encoding="utf-8")
    _, r = web("/api/inventory")
    assert "topsecret" not in json.dumps(r)
    code, r = web("/api/inventory", {"devices": [{"name": "r1", "host": "10.0.0.1", "model": "ESR-200"},
                                                 {"name": "sw", "serial_port": "COM3", "model": "MES2324",
                                                  "transport": "serial"}]})
    assert code == 200 and len(r["devices"]) == 2
    saved = web.app.inventory_path.read_text(encoding="utf-8")
    assert "topsecret" in saved and "COM3" in saved  # пароль, вписанный в файл вручную, не теряется
    assert web("/api/inventory", {"devices": [{"name": "x", "host": "1.1.1.1", "model": "DES-3200"}]})[0] == 400


def test_gui_push_needs_confirmation_and_valid_config(web, monkeypatch):
    log = []
    monkeypatch.setattr(automation, "connect", lambda d, p: FakeConn(log))
    target = {"name": "sw", "host": "10.0.0.2", "model": "MES2324", "password": "pw"}
    assert web("/api/push", {"target": target, "text": "hostname A"})[0] == 400          # нет confirm
    assert web("/api/push", {"target": target, "text": "commit", "confirm": True})[0] == 400  # чужой синтаксис
    assert log == []
    code, r = web("/api/push", {"target": target, "text": "hostname A", "confirm": True})
    j = web.wait(r["job"])
    assert j["error"] is None and ("config", ("hostname A",)) in log and "Y" in log


def test_gui_run_reports_connection_error(web, monkeypatch):
    def boom(d, p):
        raise TimeoutError("нет ответа")

    monkeypatch.setattr(automation, "connect", boom)
    _, r = web("/api/run", {"target": {"host": "10.0.0.2", "model": "MES2324"}, "commands": "show version"})
    assert "Устройство не отвечает (10.0.0.2)" in web.wait(r["job"])["error"]


# ------------------------------------------------- настоящий SSH: paramiko-сервер + фейковое устройство

@needs_pty
def test_real_ssh_session_end_to_end():
    """Проверяет всю цепочку SSH (шифрование, вход, команды, сохранение) без железа."""
    import os
    import select

    import paramiko

    fake = FakeEltex(logged_in=True)
    host_key = paramiko.RSAKey.generate(2048)

    class Server(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            ok = (username, password) == ("netadmin", "s3cret")
            return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

        def get_allowed_auths(self, username):
            return "password"

        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED

        def check_channel_shell_request(self, channel):
            return True

        def check_channel_pty_request(self, *a):
            return True

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]

    def serve():
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            t = paramiko.Transport(client)
            t.add_server_key(host_key)
            try:
                t.start_server(server=Server())
            except Exception:  # noqa: BLE001 — неудачный вход
                continue
            chan = t.accept(10)
            if chan is None:
                continue
            os.write(fake.slave, b"\r")  # показать приглашение
            while t.is_active():  # мост: SSH-канал <-> «консоль» фейкового устройства
                r, _, _ = select.select([chan, fake.slave], [], [], 0.2)
                try:
                    if chan in r:
                        data = chan.recv(1024)
                        if not data:
                            break
                        os.write(fake.slave, data)
                    if fake.slave in r:
                        chan.send(os.read(fake.slave, 1024))
                except OSError:
                    break
            t.close()

    threading.Thread(target=serve, daemon=True).start()
    dev = automation.Device("sw", "127.0.0.1", "MES2324", username="netadmin", port=port)
    try:
        assert "ip address 10.0.0.2" in automation.run_commands(dev, "s3cret", ["show running-config"])
        automation.push(dev, "s3cret", "hostname NEW", log=lambda *_: None)
        r = fake.received
        i_copy = r.index("copy running-config startup-config")
        assert r.index("hostname NEW") < i_copy and r[i_copy + 1] == "Y"
        with pytest.raises(Exception) as bad:
            automation.run_commands(dev, "wrong", ["show version"])
        assert "Неверный логин или пароль" in automation.friendly_error(bad.value, dev)
    finally:
        listener.close()
        fake.close()


# ------------------------------------------------- понятное сообщение, если не хватает библиотек

def test_missing_dependency_message(tmp_path):
    import subprocess
    from pathlib import Path

    (tmp_path / "yaml.py").write_text("raise ImportError(\"No module named 'yaml'\", name='yaml')\n")
    root = str(Path(__file__).resolve().parent.parent)
    env = {**__import__("os").environ, "PYTHONPATH": str(tmp_path) + __import__("os").pathsep + root,
           "TEMP": str(tmp_path), "TMPDIR": str(tmp_path), "TMP": str(tmp_path)}
    r = subprocess.run([sys.executable, "-m", "eltex_assistant", "models"], capture_output=True, env=env,
                       cwd=str(tmp_path), timeout=60)
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 1 and "PyYAML" in err and "pip install ." in err
    assert "Traceback" in (tmp_path / "eltex-helper-error.log").read_text(encoding="utf-8")


# ------------------------------------------------- постраничный вывод (MES24xx не знает «terminal datadump»)

@needs_pty
@pytest.mark.parametrize("paging_off", [("terminal length 0",), ("set cli pagination off",), ()],
                         ids=["terminal-length", "set-cli-pagination", "no-way-to-disable"])
def test_long_output_on_mes24xx_is_fast_and_complete(serial_dev, paging_off):
    fake, dev = serial_dev(family="mes24xx", model="MES2408B", logged_in=True, paging_off=paging_off,
                           config_lines=60)
    start = time.monotonic()
    out = automation.run_commands(dev, "pw", ["show running-config", "show running-config"])
    elapsed = time.monotonic() - start
    assert out.count("end-of-config") == 2 and out.count("description port60") == 2
    assert "More" not in out
    assert elapsed < 25, f"слишком долго: {elapsed:.0f} с"
