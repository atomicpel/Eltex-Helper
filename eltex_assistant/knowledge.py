"""База знаний: модели, семейства, команды, поиск."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

KB_DIR = Path(__file__).parent / "kb"


@dataclass
class Command:
    id: str
    family: str
    title: str
    commands: str
    mode: str = ""
    tags: list[str] = field(default_factory=list)
    example: str = ""
    explain: str = ""
    firmware: str = ""
    confidence: str = "medium"


@dataclass
class Model:
    name: str
    family: str
    kind: str
    description: str = ""
    unverified: bool = False


class UnknownModel(ValueError):
    pass


def _norm_model(name: str) -> str:
    """'mes 2324' -> 'MES2324', 'esr12v' -> 'ESR-12V', 'ESR 200' -> 'ESR-200'."""
    s = re.sub(r"[\s_]+", "", name.strip().upper())
    m = re.fullmatch(r"ESR-?(\d+[A-Z]*)", s)
    if m:
        return f"ESR-{m.group(1)}"
    return s


@lru_cache(maxsize=None)
def _models_file() -> dict:
    return yaml.safe_load((KB_DIR / "models.yaml").read_text(encoding="utf-8"))


def families() -> dict:
    return _models_file()["families"]


def models() -> dict[str, Model]:
    out = {}
    for name, d in _models_file()["models"].items():
        out[name] = Model(
            name=name,
            family=d["family"],
            kind=d.get("kind", ""),
            description=d.get("description", ""),
            unverified=bool(d.get("unverified")),
        )
    return out


def get_model(name: str) -> Model:
    all_models = models()
    key = _norm_model(name)
    if key in all_models:
        return all_models[key]
    # Префиксное совпадение: MES2324P -> нет в базе? попробуем MES2324
    for n in sorted(all_models, key=len, reverse=True):
        if key.startswith(n):
            return all_models[n]
    raise UnknownModel(
        f"Модель «{name}» не найдена. Известные: {', '.join(sorted(all_models))}. "
        "Добавьте её в kb/models.yaml."
    )


def resolve_family(model_or_family: str | None) -> str | None:
    if not model_or_family:
        return None
    if model_or_family.lower() in families():
        return model_or_family.lower()
    return get_model(model_or_family).family


@lru_cache(maxsize=None)
def _load_commands() -> tuple[Command, ...]:
    out = []
    for path in sorted((KB_DIR / "commands").glob("*.yaml")):
        family = path.stem
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for c in data.get("commands", []):
            out.append(
                Command(
                    id=c["id"],
                    family=family,
                    title=c["title"],
                    commands=c["commands"].rstrip(),
                    mode=c.get("mode", ""),
                    tags=[str(t).lower() for t in c.get("tags", [])],
                    example=(c.get("example") or "").rstrip(),
                    explain=c.get("explain", ""),
                    firmware=c.get("firmware", ""),
                    confidence=c.get("confidence", "medium"),
                )
            )
    return tuple(out)


def commands(family: str | None = None) -> list[Command]:
    return [c for c in _load_commands() if family is None or c.family == family]


def get_command(cmd_id: str, family: str) -> Command | None:
    for c in commands(family):
        if c.id == cmd_id:
            return c
    return None


_WORD = re.compile(r"[\w\-]+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text)]


def _stem(t: str) -> str:
    # Очень грубый «стеммер» для русского: отрезаем типичные окончания,
    # чтобы «порты», «порта», «порт» совпадали.
    if len(t) > 4 and re.search(r"[а-я]", t):
        return re.sub(r"(ами|ями|ов|ев|ей|ах|ях|ом|ем|ы|и|а|я|у|ю|е|о)$", "", t)
    return t


def search(query: str, family: str | None = None, limit: int = 8) -> list[tuple[int, Command]]:
    """Ищет команды по задаче на русском или по фрагменту команды."""
    q_tokens = [_stem(t) for t in _tokens(query)]
    if not q_tokens:
        return []
    results = []
    for c in commands(family):
        tags = {_stem(t) for t in c.tags}
        title = {_stem(t) for t in _tokens(c.title)}
        body = c.commands.lower() + "\n" + c.example.lower()
        score = 0
        for t in q_tokens:
            if t in tags:
                score += 5
            elif any(tag.startswith(t) or t.startswith(tag) for tag in tags if len(tag) > 2 and len(t) > 2):
                score += 3
            if t in title:
                score += 3
            elif len(t) > 3 and any(w.startswith(t) for w in title):
                score += 2
            if t == c.id or t in c.id.split("-"):
                score += 2
            if len(t) > 2 and t in body:
                score += 2
        if query.lower().strip() and query.lower().strip() in body:
            score += 6
        if score:
            results.append((score, c))
    results.sort(key=lambda x: (-x[0], x[1].family, x[1].id))
    return results[:limit]
