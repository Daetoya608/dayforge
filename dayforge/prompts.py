"""Шаблоны из каталога prompts/ в корне репозитория (Jinja2, разметка — Markdown)."""

from functools import cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@cache
def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(PROMPTS_DIR),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,  # Markdown для агентов, не HTML
    )


def render(template: str, **context: Any) -> str:
    return _environment().get_template(template).render(**context)
