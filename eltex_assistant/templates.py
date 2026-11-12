"""Шаблоны конфигураций."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import jinja2
import yaml

from .knowledge import KB_DIR


@dataclass
class Template:
    name: str
    title: str
    kind: str
    params: dict
    bodies: dict[str, str]


class TemplateError(ValueError):
    pass


def list_templates() -> list[Template]:
    out = []
    for p in sorted((KB_DIR / "templates").glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        out.append(Template(d["name"], d["title"], d.get("kind", ""), d.get("params", {}), d["bodies"]))
    return out


def get_template(name: str) -> Template:
    for t in list_templates():
        if t.name == name:
            return t
    raise TemplateError(f"Шаблон «{name}» не найден. Есть: {', '.join(t.name for t in list_templates())}")


def render(name: str, family: str, values: dict[str, str]) -> str:
    t = get_template(name)
    if family not in t.bodies:
        raise TemplateError(
            f"Шаблон «{name}» не поддерживает семейство {family}. Поддерживаются: {', '.join(t.bodies)}"
        )
    ctx = {}
    missing = []
    for key, spec in t.params.items():
        if key in values and values[key] is not None:
            ctx[key] = values[key]
        elif "default" in spec:
            ctx[key] = spec["default"]
        else:
            missing.append(key)
    unknown = set(values) - set(t.params)
    if unknown:
        raise TemplateError(f"Неизвестные параметры: {', '.join(sorted(unknown))}")
    if missing:
        hints = "; ".join(f"{k} — {t.params[k].get('help', '')}" for k in missing)
        raise TemplateError(f"Не заданы обязательные параметры: {hints}")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=False)
    text = env.from_string(t.bodies[family]).render(**ctx)
    # убираем строки, ставшие пустыми после условий
    return "\n".join(line for line in text.splitlines() if line.strip()) + "\n"
