#!/usr/bin/env python3
"""Собирает переносной пакет для Windows 7: внутри Python 3.8.10 и все библиотеки.

Пакет не требует установки и интернета: распаковать и запустить EltexHelper.bat.
Сборка идёт на любой ОС (ничего не компилируется, только скачивается и раскладывается):

    python packaging/build_win7.py            # обе разрядности
    python packaging/build_win7.py x64        # только 64 бита

Результат: dist/EltexHelper-<версия>-win7-x64.zip и ...-x86.zip
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
PY_VERSION = "3.8.10"  # последний выпуск 3.8 с официальной сборкой для Windows
ARCH = {
    "x64": {"nuget": "python", "platform": "win_amd64"},
    "x86": {"nuget": "pythonx86", "platform": "win32"},
}
# Что в комплекте Python программе не нужно (экономит ~2/3 размера)
TRIM = ["Lib/test", "Lib/tkinter", "Lib/idlelib", "Lib/turtledemo", "Lib/lib2to3", "Lib/ensurepip",
        "Lib/site-packages", "Lib/venv", "Lib/pydoc_data", "Lib/distutils/command/*.exe",
        "tcl", "Scripts", "include", "libs", "Tools", "DLLs/tcl*.dll", "DLLs/tk*.dll", "DLLs/_tkinter.pyd",
        "DLLs/_test*.pyd", "DLLs/_msi.pyd"]

GUI_BAT = """@echo off\r
rem Eltex Helper: graphical interface (opens in the browser)\r
set "PYTHONPATH=%~dp0lib"\r
set PYTHONNOUSERSITE=1\r
cd /d "%~dp0data"\r
"%~dp0python\\python.exe" -m eltex_assistant gui\r
if errorlevel 1 pause\r
"""
CLI_BAT = """@echo off\r
rem Eltex Helper: command line. Example: eltex search trunk -m MES2324\r
set "PYTHONPATH=%~dp0lib"\r
set PYTHONNOUSERSITE=1\r
"%~dp0python\\python.exe" -m eltex_assistant %*\r
"""
README = """Помощник Eltex {version} — переносной пакет для Windows 7 ({arch})

Запуск: дважды щёлкните EltexHelper.bat. Откроется чёрное окно (не закрывайте его,
пока работаете) и страница программы в браузере.

Нужен современный браузер: Chrome 109, Firefox 115 ESR или новее из тех, что
работают на Windows 7. Internet Explorer не подойдёт. Если страница открылась в
Internet Explorer, скопируйте ссылку из чёрного окна в Chrome или Firefox.

Командная строка: eltex.bat (например: eltex.bat search транк -m MES2324).

Установка не нужна, интернет не нужен. Список устройств и бэкапы хранятся
в папке data рядом с программой.

Если при запуске Windows сообщает, что не найден api-ms-win-crt-runtime-l1-1-0.dll,
установите обновления Windows 7: Service Pack 1 и KB2999226 (Universal C Runtime).
"""


def version() -> str:
    text = (ROOT / "eltex_assistant" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def fetch(url: str, dest: Path) -> Path:
    if not dest.exists():
        print("  скачиваю", url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    return dest


def build(arch: str) -> Path:
    cfg = ARCH[arch]
    name = f"EltexHelper-{version()}-win7-{arch}"
    work = DIST / "build" / name
    cache = DIST / "cache"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    print(f"[{arch}] Python {PY_VERSION}")
    pkg = cfg["nuget"]
    nupkg = fetch(f"https://api.nuget.org/v3-flatcontainer/{pkg}/{PY_VERSION}/{pkg}.{PY_VERSION}.nupkg",
                  cache / f"{pkg}.{PY_VERSION}.nupkg")
    with zipfile.ZipFile(nupkg) as z:
        for info in z.infolist():
            if info.filename.startswith("tools/") and not info.is_dir():
                target = work / "python" / info.filename[len("tools/"):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(info))
    for pattern in TRIM:
        for p in (work / "python").glob(pattern):
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    for p in (work / "python").rglob("__pycache__"):
        shutil.rmtree(p)

    print(f"[{arch}] библиотеки")
    wheels = cache / f"wheels-{arch}"
    subprocess.run(
        [sys.executable, "-m", "pip", "download", "-q", "-r", str(ROOT / "packaging" / "requirements-win7.txt"),
         "-d", str(wheels), "--platform", cfg["platform"], "--python-version", "3.8", "--implementation", "cp",
         "--abi", "cp38", "--abi", "abi3", "--abi", "none", "--only-binary=:all:"], check=True)
    lib = work / "lib"
    for whl in sorted(wheels.glob("*.whl")):
        if whl.name.startswith("setuptools-"):
            continue
        with zipfile.ZipFile(whl) as z:
            z.extractall(lib)
    shutil.copytree(ROOT / "eltex_assistant", lib / "eltex_assistant",
                    ignore=shutil.ignore_patterns("__pycache__"))

    (work / "data").mkdir()
    (work / "data" / "inventory-example.yaml").write_bytes((ROOT / "examples" / "inventory.yaml").read_bytes())
    (work / "EltexHelper.bat").write_bytes(GUI_BAT.encode("ascii"))
    (work / "eltex.bat").write_bytes(CLI_BAT.encode("ascii"))
    # Блокнот Windows 7 понимает UTF-8 только с BOM и переводы строк CRLF
    (work / "README.txt").write_bytes(
        README.format(version=version(), arch=arch).replace("\n", "\r\n").encode("utf-8-sig"))

    out = DIST / f"{name}.zip"
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(work.rglob("*")):
            rel = p.relative_to(work.parent).as_posix()
            if not rel.isascii():  # встроенный распаковщик Windows 7 портит не-ASCII имена
                raise SystemExit(f"Не-ASCII имя файла в пакете: {rel}")
            if p.is_dir():
                if not any(p.iterdir()):
                    z.writestr(rel + "/", "")
            else:
                z.write(p, rel)
    print(f"[{arch}] готово: {out} ({out.stat().st_size / 1e6:.1f} МБ)")
    return out


if __name__ == "__main__":
    for a in (sys.argv[1:] or list(ARCH)):
        if a not in ARCH:
            sys.exit(f"Неизвестная разрядность {a!r}: x64 или x86")
        build(a)
