"""Командная строка помощника: eltex <команда> …   (подробно: eltex -h)"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import checker, knowledge, templates

CONF_MARK = {"high": "", "medium": "  [проверьте «?» на устройстве]", "low": "  [черновик, синтаксис не подтверждён]"}


def _out(s: str = "") -> None:
    print(s)


def _family_arg(args) -> str | None:
    try:
        return knowledge.resolve_family(getattr(args, "model", None))
    except knowledge.UnknownModel as e:
        sys.exit(str(e))


def _print_command(c: knowledge.Command, full: bool = True) -> None:
    fam = knowledge.families()[c.family]
    _out(f"── {c.title}  ({c.family}, id: {c.id}){CONF_MARK.get(c.confidence, '')}")
    if c.mode:
        _out(f"   режим: {c.mode}")
    for line in c.commands.splitlines():
        _out(f"   {line}")
    if full:
        if c.example:
            _out("   пример:")
            for line in c.example.splitlines():
                _out(f"     {line}")
        if c.explain:
            _out(f"   ⓘ {c.explain}")
        if c.firmware:
            _out(f"   ⚙ версии ПО: {c.firmware}")
        if c.mode in ("config", "interface") and c.family != "esr":
            _out(f"   сохранить: {fam['save']}")
        if c.mode in ("config", "interface") and c.family == "esr":
            _out("   применить: commit → (проверить доступ) → confirm")
    _out()


# ---------------------------------------------------------------- справочник

def cmd_models(args) -> None:
    fams = knowledge.families()
    by_family: dict[str, list] = {}
    for m in knowledge.models().values():
        by_family.setdefault(m.family, []).append(m)
    for fam, ms in by_family.items():
        f = fams[fam]
        _out(f"[{fam}] {f['title']}")
        _out(f"   сохранение: {f['save']};  вход в конфиг: {f['config_enter']};  логин по умолчанию: {f['default_login']}")
        for m in ms:
            mark = "  (семейство не подтверждено)" if m.unverified else ""
            _out(f"   • {m.name:10} {m.description}{mark}")
        for n in f.get("notes", []):
            _out(f"   ⓘ {n}")
        _out()


def cmd_search(args) -> None:
    family = _family_arg(args)
    query = " ".join(args.query)
    res = knowledge.search(query, family, limit=args.limit)
    if not res:
        _out("Ничего не нашёл. Попробуйте другие слова (например: vlan, trunk, nat, сохранить, ssh).")
        return
    if family is None:
        _out("Модель не указана — показываю для всех семейств. Уточните: -m MES2324 / -m ESR-12V\n")
    for _, c in res[: args.limit]:
        _print_command(c, full=not args.short)


def cmd_show(args) -> None:
    family = _family_arg(args)
    fams = [family] if family else list(knowledge.families())
    found = False
    for f in fams:
        c = knowledge.get_command(args.id, f)
        if c:
            found = True
            _print_command(c)
        elif family:
            _out(f"Для семейства {f} записи «{args.id}» пока нет.")
    if not found and not family:
        ids = sorted({c.id for c in knowledge.commands()})
        _out(f"Нет записи «{args.id}». Доступные id: {', '.join(ids)}")


def cmd_list(args) -> None:
    family = _family_arg(args)
    for c in knowledge.commands(family):
        _out(f"{c.family:8} {c.id:22} {c.title}")


def cmd_ask(args) -> None:
    """Интерактивный режим: пишете задачу — получаете команды."""
    family = _family_arg(args)
    _out("Помощник Eltex. Опишите задачу словами (например «настроить транк»).")
    _out("Команды: :m <модель> — выбрать модель, :q — выход.\n")
    while True:
        try:
            q = input(f"[{family or 'все'}]> ").strip()
        except (EOFError, KeyboardInterrupt):
            _out()
            return
        if not q:
            continue
        if q in (":q", "exit", "выход"):
            return
        if q.startswith(":m"):
            try:
                family = knowledge.resolve_family(q[2:].strip()) if q[2:].strip() else None
            except knowledge.UnknownModel as e:
                _out(str(e))
            continue
        res = knowledge.search(q, family, limit=3)
        if not res:
            _out("Не нашёл. Попробуйте иначе.\n")
        for _, c in res:
            _print_command(c)


# ---------------------------------------------------------------- шаблоны и проверка

def _parse_params(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            sys.exit(f"Параметр «{p}» должен быть в виде ключ=значение")
        k, v = p.split("=", 1)
        out[k.strip()] = v
    return out


def cmd_template_list(args) -> None:
    for t in templates.list_templates():
        _out(f"{t.name:16} {t.title}   [семейства: {', '.join(t.bodies)}]")
        for k, spec in t.params.items():
            req = "обязательный" if spec.get("required") else f"по умолчанию: {spec.get('default')!r}"
            _out(f"     {k:16} {spec.get('help', '')} ({req})")
        _out()


def cmd_template_render(args) -> None:
    family = _family_arg(args)
    if not family:
        sys.exit("Укажите модель: -m MES2324")
    try:
        text = templates.render(args.name, family, _parse_params(args.param))
    except templates.TemplateError as e:
        sys.exit(str(e))
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        _out(f"Сохранено в {args.output}")
    else:
        _out(text)


def cmd_check(args) -> None:
    family = _family_arg(args)
    if not family:
        sys.exit("Укажите модель: -m MES2324")
    text = Path(args.file).read_text(encoding="utf-8")
    issues = checker.check(text, family)
    if not issues:
        _out("Замечаний нет.")
        return
    for i in issues:
        where = f"строка {i.line_no}: {i.line.strip()}" if i.line_no else "весь файл"
        _out(f"[{i.level}] {where}\n          {i.message}")
    if any(i.level == "ошибка" for i in issues):
        sys.exit(1)


# ---------------------------------------------------------------- SSH

def _select_devices(args):
    from . import automation

    serial_port = getattr(args, "serial", None)
    if args.host or serial_port:
        if not args.model:
            sys.exit("Нужно указать модель: -m MES2324")
        try:
            knowledge.get_model(args.model)
        except knowledge.UnknownModel as e:
            sys.exit(str(e))
        transport = "serial" if serial_port else ("telnet" if args.telnet else "ssh")
        return [automation.Device(
            name=args.host or serial_port, host=args.host or serial_port, model=args.model,
            username=args.user or "admin", transport=transport, serial_port=serial_port, baud=args.baud,
            port=args.port,
        )]
    if not args.inventory:
        sys.exit("Укажите --host АДРЕС -m МОДЕЛЬ, --serial COM3 -m МОДЕЛЬ или --inventory файл.yaml")
    inv = automation.load_inventory(args.inventory)
    if args.device:
        missing = [d for d in args.device if d not in inv]
        if missing:
            sys.exit(f"Нет в инвентаре: {', '.join(missing)}")
        return [inv[d] for d in args.device]
    return list(inv.values())


def _passwords(devs) -> dict[str, str]:
    from . import automation

    cache: dict[str, str] = {}
    return {d.name: automation.resolve_password(d, cache) for d in devs}


def cmd_ssh_run(args) -> None:
    from . import automation

    devs = _select_devices(args)
    cmds = args.command or []
    if args.file:
        cmds += [l for l in Path(args.file).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not cmds:
        sys.exit("Нет команд: -c \"show version\" или --file")
    for name, out in automation.run_many(devs, _passwords(devs), cmds).items():
        _out(f"===== {name} =====\n{out}\n")


def cmd_ssh_backup(args) -> None:
    from . import automation

    devs = _select_devices(args)
    pw = _passwords(devs)
    for d in devs:
        try:
            path, diff = automation.backup(d, pw[d.name], args.output)
            _out(f"{d.name}: сохранено в {path}")
            if diff:
                _out(f"  изменения с прошлого бэкапа:\n{diff}\n")
        except Exception as e:  # noqa: BLE001
            _out(f"{d.name}: ОШИБКА {automation.friendly_error(e, d)}")


def cmd_ssh_push(args) -> None:
    from . import automation

    devs = _select_devices(args)
    text = Path(args.file).read_text(encoding="utf-8")
    lines = automation.prepare_lines(text)
    for d in devs:
        issues = checker.check(text, d.family)
        errors = [i for i in issues if i.level == "ошибка"]
        if errors:
            for i in errors:
                _out(f"[{d.name}] строка {i.line_no}: {i.line.strip()} — {i.message}")
            sys.exit(f"Файл не подходит для {d.model} ({d.family}). Отправка отменена.")
    _out("Будет отправлено на: " + ", ".join(f"{d.name} ({d.host}, {d.model})" for d in devs))
    _out("-" * 60)
    _out("\n".join(lines))
    _out("-" * 60)
    if args.dry_run:
        _out("Режим --dry-run: ничего не отправлено.")
        return
    if not args.yes:
        if input("Отправить? Введите «да»: ").strip().lower() not in ("да", "yes", "y"):
            _out("Отменено.")
            return
    pw = _passwords(devs)
    failed = 0
    for d in devs:
        try:
            out = automation.push(d, pw[d.name], text, save=not args.no_save, log=_out)
            if args.verbose:
                _out(out)
            _out(f"[{d.name}] готово.\n")
        except Exception as e:  # noqa: BLE001
            failed += 1
            _out(f"[{d.name}] {e if isinstance(e, automation.PushError) else automation.friendly_error(e, d)}\n")
            if not args.continue_on_error:
                sys.exit("Остановлено после первой ошибки (используйте --continue-on-error, чтобы продолжать).")
    if failed:
        sys.exit(1)


def cmd_neighbors(args) -> None:
    from . import automation

    devs = _select_devices(args)
    for name, out in automation.run_many(devs, _passwords(devs), ["show lldp neighbors"]).items():
        _out(f"===== {name} =====\n{out}\n")


# ---------------------------------------------------------------- сканирование и консоль

def cmd_scan(args) -> None:
    import yaml

    from . import scan

    def progress(done: int, total: int) -> None:
        if sys.stderr.isatty():
            print(f"\rпроверено {done}/{total}", end="", file=sys.stderr, flush=True)

    try:
        hosts = scan.scan(" ".join(args.targets), args.community, args.timeout, args.workers, progress)
    except ValueError as e:
        sys.exit(f"Не понял адреса: {e}")
    if sys.stderr.isatty():
        print(file=sys.stderr)
    if not hosts:
        _out("Никто не ответил.")
        return
    _out(f"{'IP':15} {'MAC':17} {'пинг':4} {'открыто':22} {'кто':16} {'модель':10} имя / описание")
    for h in hosts:
        open_ports = ",".join(n for n, ok in h.ports.items() if ok)
        if h.snmp:
            open_ports = (open_ports + ",snmp").strip(",")
        info = h.name or h.snmp.get("sysDescr", "") or h.http_title or h.telnet_banner or h.ssh_banner
        _out(f"{h.ip:15} {h.mac:17} {'да' if h.ping else '-':4} {open_ports:22} {h.vendor:16} {h.model:10} {info[:60]}")
    eltex = [h for h in hosts if h.vendor]
    _out(f"\nОтветило адресов: {len(hosts)}, из них Eltex: {len(eltex)}.")
    if args.save_inventory:
        inv = scan.to_inventory(hosts, args.user or "admin")
        Path(args.save_inventory).write_text(
            yaml.safe_dump(inv, allow_unicode=True, sort_keys=False), encoding="utf-8")
        _out(f"В инвентарь {args.save_inventory} записано устройств: {len(inv['devices'])}.")


def cmd_console(args) -> None:
    from . import automation

    if not args.port:
        ports = automation.list_serial_ports()
        if not ports:
            _out("Последовательных портов не найдено. Подключите консольный кабель (USB-COM) и проверьте драйвер.")
        for dev, desc in ports:
            _out(f"{dev:16} {desc}")
        return
    from serial.tools import miniterm

    _out(f"Консоль {args.port}, {args.baud} 8N1. Выход: Ctrl+]   (нажмите Enter, чтобы увидеть приглашение)")
    sys.argv = ["miniterm", args.port, str(args.baud), "--eol", "CR"]
    miniterm.main()


def cmd_gui(args) -> None:
    from . import gui

    gui.serve(args.port, open_browser=not args.no_browser, inventory=args.inventory)


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eltex", description="Помощник по коммутаторам и маршрутизаторам Eltex")
    sub = p.add_subparsers(dest="cmd", required=True)

    def model_opt(sp):
        sp.add_argument("-m", "--model", help="модель (MES2324, ESR-12V…) или семейство (mes23xx, mes24xx, esr)")

    s = sub.add_parser("models", help="поддерживаемые модели и их особенности")
    s.set_defaults(func=cmd_models)

    s = sub.add_parser("search", aliases=["найти"], help="найти команды по задаче: eltex search настроить транк -m MES2324")
    s.add_argument("query", nargs="+")
    model_opt(s)
    s.add_argument("-n", "--limit", type=int, default=5)
    s.add_argument("--short", action="store_true", help="только команды, без пояснений")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("show", help="показать запись по id (без -m — сравнить все семейства)")
    s.add_argument("id")
    model_opt(s)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("list", help="список всех записей справочника")
    model_opt(s)
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("ask", help="интерактивный режим вопрос-ответ")
    model_opt(s)
    s.set_defaults(func=cmd_ask)

    t = sub.add_parser("template", help="шаблоны конфигураций").add_subparsers(dest="tcmd", required=True)
    s = t.add_parser("list", help="список шаблонов и их параметров")
    s.set_defaults(func=cmd_template_list)
    s = t.add_parser("render", help="собрать конфигурацию из шаблона")
    s.add_argument("name")
    model_opt(s)
    s.add_argument("-p", "--param", action="append", help="ключ=значение (можно несколько раз)")
    s.add_argument("-o", "--output", help="сохранить в файл")
    s.set_defaults(func=cmd_template_render)

    s = sub.add_parser("check", help="проверить файл конфигурации на ошибки синтаксиса для модели")
    s.add_argument("file")
    model_opt(s)
    s.set_defaults(func=cmd_check)

    ssh = sub.add_parser("ssh", help="работа с устройствами по SSH").add_subparsers(dest="scmd", required=True)

    def target_opts(sp):
        sp.add_argument("-i", "--inventory", help="файл инвентаря YAML")
        sp.add_argument("-d", "--device", action="append", help="имя устройства из инвентаря (можно несколько)")
        sp.add_argument("--host", help="адрес одного устройства (вместо инвентаря)")
        sp.add_argument("-u", "--user", help="логин (для --host / --serial)")
        sp.add_argument("--port", type=int, help="нестандартный TCP-порт (для --host)")
        sp.add_argument("--telnet", action="store_true", help="подключаться по telnet вместо SSH (для --host)")
        sp.add_argument("--serial", metavar="ПОРТ", help="работать через консольный порт: COM3, /dev/ttyUSB0")
        sp.add_argument("--baud", type=int, default=115200, help="скорость консоли (по умолчанию 115200)")
        model_opt(sp)

    s = ssh.add_parser("run", help="выполнить show-команды")
    target_opts(s)
    s.add_argument("-c", "--command", action="append", help="команда (можно несколько раз)")
    s.add_argument("-f", "--file", help="файл со списком команд")
    s.set_defaults(func=cmd_ssh_run)

    s = ssh.add_parser("backup", help="сохранить running-config и показать изменения")
    target_opts(s)
    s.add_argument("-o", "--output", default="backups")
    s.set_defaults(func=cmd_ssh_backup)

    s = ssh.add_parser("neighbors", help="соседи по LLDP (show lldp neighbors)")
    target_opts(s)
    s.set_defaults(func=cmd_neighbors)

    s = ssh.add_parser("push", help="применить конфигурацию из файла (с проверкой и подтверждением)")
    target_opts(s)
    s.add_argument("file")
    s.add_argument("--dry-run", action="store_true", help="только показать, что будет отправлено")
    s.add_argument("--no-save", action="store_true", help="не сохранять (MES) / не делать confirm (ESR)")
    s.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение")
    s.add_argument("-v", "--verbose", action="store_true", help="показать вывод устройства")
    s.add_argument("--continue-on-error", action="store_true")
    s.set_defaults(func=cmd_ssh_push)
    s = sub.add_parser("scan", help="сканировать сеть: eltex scan 192.168.1.0/24")
    s.add_argument("targets", nargs="+", help="подсеть, диапазон или адреса: 10.0.0.0/24  10.0.0.10-50")
    s.add_argument("-c", "--community", action="append", help="SNMP community (можно несколько; по умолчанию public)")
    s.add_argument("-t", "--timeout", type=float, default=1.0)
    s.add_argument("-w", "--workers", type=int, default=64)
    s.add_argument("--save-inventory", metavar="ФАЙЛ", help="записать найденные Eltex в файл инвентаря")
    s.add_argument("-u", "--user", help="логин по умолчанию для инвентаря")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("console", help="терминал через консольный порт (без аргументов — список портов)")
    s.add_argument("port", nargs="?", help="COM3, /dev/ttyUSB0")
    s.add_argument("--baud", type=int, default=115200)
    s.set_defaults(func=cmd_console)

    s = sub.add_parser("gui", help="графический интерфейс в браузере")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")
    s.add_argument("-i", "--inventory", default="inventory.yaml", help="файл со списком устройств")
    s.set_defaults(func=cmd_gui)
    return p


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):  # корректная кириллица в Windows при выводе в файл или конвейер
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
