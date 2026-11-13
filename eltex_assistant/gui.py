"""Графический интерфейс: локальный веб-сервер + страница в браузере.

Сервер слушает только 127.0.0.1 и требует секретный токен в каждом запросе
к API, чтобы посторонний сайт, открытый в том же браузере, не мог через него
управлять оборудованием. Пароли устройств никуда не записываются: они
приходят из формы и живут только на время операции.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from . import __version__, checker, knowledge, templates, terminal

HTML = Path(__file__).parent / "gui.html"


class ApiError(Exception):
    pass


class Jobs:
    """Долгие операции (сканирование, SSH) выполняются в фоне; страница опрашивает состояние."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, fn) -> str:
        jid = uuid.uuid4().hex
        job = {"done": False, "log": [], "result": None, "error": None, "progress": None}
        with self._lock:
            self._jobs[jid] = job

        def run() -> None:
            try:
                job["result"] = fn(job)
            except Exception as e:  # noqa: BLE001 — любую ошибку показываем пользователю
                plain = isinstance(e, (ApiError, ValueError, RuntimeError))
                job["error"] = str(e) if plain else f"{type(e).__name__}: {e}"
            job["done"] = True

        threading.Thread(target=run, daemon=True).start()
        return jid

    def get(self, jid: str) -> dict:
        job = self._jobs.get(jid)
        if job is None:
            raise ApiError("Операция не найдена")
        return job


class App:
    def __init__(self, inventory_path: str | Path = "inventory.yaml") -> None:
        self.token = secrets.token_urlsafe(24)
        self.jobs = Jobs()
        self.terms = terminal.Sessions()
        self.inventory_path = Path(inventory_path)

    # ------------------------------------------------------------ справочник

    def meta(self, _q) -> dict:
        fams = knowledge.families()
        return {
            "version": __version__,
            "families": {k: {"title": v["title"], "save": v["save"], "notes": v.get("notes", []),
                             "default_login": v["default_login"]} for k, v in fams.items()},
            "models": [{"name": m.name, "family": m.family, "kind": m.kind, "description": m.description,
                        "unverified": m.unverified} for m in knowledge.models().values()],
            "templates": [{"name": t.name, "title": t.title, "families": list(t.bodies), "params": t.params}
                          for t in templates.list_templates()],
        }

    @staticmethod
    def _cmd(c: knowledge.Command) -> dict:
        return {"id": c.id, "family": c.family, "title": c.title, "mode": c.mode, "commands": c.commands,
                "example": c.example, "explain": c.explain, "firmware": c.firmware, "confidence": c.confidence}

    def search(self, q) -> dict:
        family = knowledge.resolve_family(q.get("model") or None)
        text = (q.get("q") or "").strip()
        if text:
            items = [c for _, c in knowledge.search(text, family, limit=12)]
        else:
            items = knowledge.commands(family)
        return {"items": [self._cmd(c) for c in items]}

    def render(self, body) -> dict:
        family = knowledge.resolve_family(body.get("model"))
        if not family:
            raise ApiError("Выберите модель")
        params = {k: v for k, v in (body.get("params") or {}).items() if v not in ("", None)}
        text = templates.render(body["name"], family, params)
        return {"text": text, "issues": self._issues(text, family)}

    @staticmethod
    def _issues(text: str, family: str) -> list[dict]:
        return [{"line_no": i.line_no, "line": i.line.strip(), "level": i.level, "message": i.message}
                for i in checker.check(text, family)]

    def check(self, body) -> dict:
        family = knowledge.resolve_family(body.get("model"))
        if not family:
            raise ApiError("Выберите модель")
        return {"issues": self._issues(body.get("text", ""), family)}

    # ------------------------------------------------------------ устройства

    def inventory_get(self, _q) -> dict:
        if not self.inventory_path.exists():
            return {"path": os.path.abspath(self.inventory_path), "devices": []}
        data = yaml.safe_load(self.inventory_path.read_text(encoding="utf-8")) or {}
        defaults = data.get("defaults") or {}
        devices = []
        for name, d in (data.get("devices") or {}).items():
            d = {**defaults, **(d or {})}
            d.pop("password", None)  # пароли из файла в браузер не отдаём
            devices.append({"name": name, **d})
        return {"path": os.path.abspath(self.inventory_path), "devices": devices}

    def inventory_save(self, body) -> dict:
        devices = {}
        for d in body.get("devices") or []:
            name = str(d.get("name") or "").strip()
            if not name:
                raise ApiError("У устройства должно быть имя")
            knowledge.get_model(str(d.get("model", "")))
            entry = {k: d[k] for k in ("host", "model", "username", "transport", "serial_port", "baud", "port")
                     if d.get(k) not in (None, "")}
            devices[name] = entry
        old = {}
        if self.inventory_path.exists():
            old = yaml.safe_load(self.inventory_path.read_text(encoding="utf-8")) or {}
            for name, entry in devices.items():  # сохраняем пароли, если человек сам вписал их в файл
                pw = ((old.get("devices") or {}).get(name) or {}).get("password")
                if pw:
                    entry["password"] = pw
        self.inventory_path.write_text(
            yaml.safe_dump({"devices": devices}, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return self.inventory_get(None)

    def ports(self, _q) -> dict:
        from . import automation

        return {"ports": [{"device": d, "description": desc} for d, desc in automation.list_serial_ports()]}

    def _device(self, t: dict):
        from . import automation

        if not t or not t.get("model"):
            raise ApiError("Не выбрано устройство или не указана модель")
        knowledge.get_model(t["model"])
        transport = (t.get("transport") or "ssh").lower()
        if transport == "serial" and not t.get("serial_port"):
            raise ApiError("Для работы через консоль укажите порт (COM3, /dev/ttyUSB0)")
        if transport != "serial" and not t.get("host"):
            raise ApiError("Не указан адрес устройства")
        password = t.get("password") or ""
        if not password and t.get("name") and self.inventory_path.exists():
            inv = automation.load_inventory(self.inventory_path)
            if t["name"] in inv and inv[t["name"]].password:
                password = inv[t["name"]].password
        dev = automation.Device(
            name=t.get("name") or t.get("host") or t.get("serial_port"),
            host=t.get("host") or t.get("serial_port") or "",
            model=t["model"], username=t.get("username") or "admin", transport=transport,
            serial_port=t.get("serial_port") or None, baud=int(t.get("baud") or 115200),
            port=int(t["port"]) if t.get("port") else None,
        )
        return dev, password

    # ------------------------------------------------------------ фоновые операции

    @staticmethod
    def _wrap(dev, fn):
        """Переводит ошибки подключения на понятный русский."""
        from . import automation

        def inner(job):
            try:
                return fn(job)
            except automation.PushError:
                raise
            except Exception as e:  # noqa: BLE001
                raise ApiError(automation.friendly_error(e, dev)) from e

        return inner

    def scan(self, body) -> dict:
        from . import scan

        spec = (body.get("targets") or "").strip()
        if not spec:
            raise ApiError("Укажите подсеть или диапазон адресов")
        scan.expand_targets(spec)  # ошибку в адресах показываем сразу
        communities = [c.strip() for c in (body.get("communities") or "public").split(",") if c.strip()]

        def work(job):
            def progress(done, total):
                job["progress"] = [done, total]

            hosts = scan.scan(spec, communities, float(body.get("timeout") or 1.0), progress=progress)
            return {"hosts": [h.as_dict() for h in hosts]}

        return {"job": self.jobs.start(work)}

    def run(self, body) -> dict:
        from . import automation

        dev, password = self._device(body.get("target"))
        cmds = [c.strip() for c in (body.get("commands") or "").splitlines() if c.strip()]
        if not cmds:
            raise ApiError("Введите хотя бы одну команду")
        return {"job": self.jobs.start(self._wrap(dev, lambda job: {"output": automation.run_commands(dev, password, cmds)}))}

    def backup(self, body) -> dict:
        from . import automation

        dev, password = self._device(body.get("target"))

        def work(job):
            path, diff = automation.backup(dev, password, self.inventory_path.parent / "backups")
            return {"path": os.path.abspath(path), "diff": diff, "config": path.read_text(encoding="utf-8")}

        return {"job": self.jobs.start(self._wrap(dev, work))}

    def push(self, body) -> dict:
        from . import automation

        dev, password = self._device(body.get("target"))
        text = body.get("text") or ""
        if not automation.prepare_lines(text):
            raise ApiError("Конфигурация пуста")
        errors = [i for i in self._issues(text, dev.family) if i["level"] == "ошибка"]
        if errors:
            raise ApiError("Конфигурация не подходит для этой модели: "
                           + "; ".join(f"строка {e['line_no']}: {e['message']}" for e in errors))
        if body.get("confirm") is not True:
            raise ApiError("Отправка не подтверждена")
        save = bool(body.get("save", True))

        def work(job):
            out = automation.push(dev, password, text, save=save, log=job["log"].append)
            return {"output": out}

        return {"job": self.jobs.start(self._wrap(dev, work))}

    # ------------------------------------------------------------ живой терминал

    def term_open(self, body) -> dict:
        dev, password = self._device(body.get("target"))
        return {"id": self.terms.open(dev, password).id, "family": dev.family}

    def term_read(self, q) -> dict:
        try:
            return self.terms.get(q.get("id", "")).read(int(q.get("pos") or 0), wait=min(float(q.get("wait") or 15), 25.0))
        except terminal.TermError as e:
            raise ApiError(str(e)) from e

    def term_write(self, body) -> dict:
        data = body.get("data")
        if not isinstance(data, str) or len(data) > 100_000:
            raise ApiError("Слишком длинная вставка")
        try:
            self.terms.get(body.get("id", "")).write(data)
        except terminal.TermError as e:
            raise ApiError(str(e)) from e
        return {"ok": True}

    def term_close(self, body) -> dict:
        try:
            self.terms.get(body.get("id", "")).close()
        except terminal.TermError:
            pass
        return {"ok": True}

    def hint(self, body) -> dict:
        """Подсказка к строке, которую человек сейчас набирает в терминале."""
        family = knowledge.resolve_family(body.get("model"))
        line = " ".join(str(body.get("line") or "").split())
        if not family or len(line) < 2:
            return {"issues": [], "matches": []}
        low, matches, seen = line.lower(), [], set()
        for c in knowledge.commands(family):
            for raw in str(c.commands).splitlines():
                cmd = raw.strip()
                if cmd and cmd.lower().startswith(low) and cmd not in seen:
                    seen.add(cmd)
                    matches.append({"command": cmd, "title": c.title, "id": c.id})
        return {"issues": [i for i in self._issues(line, family) if i["line_no"]], "matches": matches[:8]}

    def job(self, q) -> dict:
        j = self.jobs.get(q.get("id", ""))
        return {k: j[k] for k in ("done", "log", "result", "error", "progress")}

    ROUTES_GET = {"/api/meta": "meta", "/api/search": "search", "/api/inventory": "inventory_get",
                  "/api/ports": "ports", "/api/job": "job", "/api/term/read": "term_read"}
    ROUTES_POST = {"/api/render": "render", "/api/check": "check", "/api/inventory": "inventory_save",
                   "/api/scan": "scan", "/api/run": "run", "/api/backup": "backup", "/api/push": "push",
                   "/api/term/open": "term_open", "/api/term/write": "term_write", "/api/term/close": "term_close",
                   "/api/hint": "hint"}


def make_handler(app: App, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "eltex-assistant"

        def log_message(self, *args) -> None:  # тишина в консоли
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _api(self, routes: dict, arg) -> None:
            path = urlparse(self.path).path
            # Host проверяем против DNS-rebinding, токен — против запросов с чужих страниц
            if self.headers.get("Host", "") not in allowed_hosts:
                return self._json(403, {"error": "Недопустимый адрес"})
            if not secrets.compare_digest(self.headers.get("X-Token", ""), app.token):
                return self._json(403, {"error": "Нет доступа. Откройте программу заново командой eltex gui."})
            name = routes.get(path)
            if not name:
                return self._json(404, {"error": "Не найдено"})
            try:
                self._json(200, getattr(app, name)(arg()))
            except (ApiError, ValueError, KeyError) as e:
                msg = str(e) if not isinstance(e, KeyError) else f"Не хватает поля {e}"
                self._json(400, {"error": msg})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/":
                if self.headers.get("Host", "") not in allowed_hosts:
                    return self._send(403, b"forbidden", "text/plain")
                return self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
            self._api(App.ROUTES_GET, lambda: {k: v[0] for k, v in parse_qs(url.query).items()})

        def do_POST(self) -> None:  # noqa: N802
            def body():
                n = int(self.headers.get("Content-Length") or 0)
                if n > 2_000_000:
                    raise ApiError("Слишком большой запрос")
                return json.loads(self.rfile.read(n) or b"{}")

            self._api(App.ROUTES_POST, body)

    return Handler


def make_server(port: int = 8765, inventory: str | Path = "inventory.yaml") -> tuple[ThreadingHTTPServer, App]:
    app = App(inventory)
    last_error: OSError | None = None
    for p in ([port] if port == 0 else range(port, port + 20)):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), lambda *a: None)
            break
        except OSError as e:
            last_error = e
    else:
        raise OSError(f"Не удалось занять порт {port}: {last_error}")
    real = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(app, {f"127.0.0.1:{real}", f"localhost:{real}"})
    return httpd, app


def serve(port: int = 8765, open_browser: bool = True, inventory: str | Path = "inventory.yaml") -> None:
    httpd, app = make_server(port, inventory)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/#t={app.token}"
    print("Помощник Eltex запущен. Откройте в браузере:")
    print(f"  {url}")
    print("Остановить: Ctrl+C")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        app.terms.close_all()
        httpd.server_close()
