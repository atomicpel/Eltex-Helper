"""Запуск: python -m eltex_assistant …

Любая непредвиденная ошибка записывается в %TEMP%/eltex-helper-error.log
(UTF-8), а на экран выводится понятное сообщение. Это важно на Windows 7,
где консоль может не суметь показать часть текста ошибки.
"""
import os
import sys
import tempfile
import traceback

LOG = os.path.join(tempfile.gettempdir(), "eltex-helper-error.log")

DEPS = {
    "yaml": "PyYAML", "jinja2": "Jinja2", "netmiko": "netmiko", "paramiko": "paramiko",
    "serial": "pyserial", "cryptography": "cryptography", "bcrypt": "bcrypt", "nacl": "pynacl",
}


def _say(text: str) -> None:
    """Печать, которая не падает на «неудобной» консоли."""
    for stream in (sys.stderr, sys.__stderr__):
        if stream is None:
            continue
        try:
            stream.write(text + "\n")
            stream.flush()
            return
        except (OSError, UnicodeError, ValueError):
            try:
                stream.buffer.write(text.encode("ascii", "backslashreplace") + b"\n")
                stream.flush()
                return
            except Exception:  # noqa: BLE001
                continue


def _run() -> None:
    try:
        from .cli import main

        main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as e:  # noqa: BLE001
        try:
            with open(LOG, "w", encoding="utf-8") as f:
                f.write(f"Python {sys.version}\n{sys.executable}\n\n")
                traceback.print_exc(file=f)
        except OSError:
            pass
        name = getattr(e, "name", None) if isinstance(e, ImportError) else None
        top = (name or "").split(".")[0]
        if top in DEPS:
            _say(f"Не установлена библиотека {DEPS[top]} (модуль {name}).")
            _say("Missing library. Install dependencies from the program folder:  pip install .")
            _say("On Windows 7 use the portable package EltexHelper-<version>-win7-x64.zip (or -x86) from Releases.")
        elif isinstance(e, ImportError) and "DLL" in str(e):
            _say("Не удалось загрузить системную библиотеку (DLL).")
            _say("DLL load failed. On Windows 7 install SP1 and KB2999226, or use the portable win7 package.")
        else:
            _say(f"Ошибка: {type(e).__name__}: {e}")
        _say(f"Details / подробности: {LOG}")
        sys.exit(1)


_run()
