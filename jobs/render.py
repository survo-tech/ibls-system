"""Render invitation / reminder emails from templates/ (Jinja2)."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

_text_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=False, undefined=StrictUndefined,
                        keep_trailing_newline=False, trim_blocks=False)
_html_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html", "j2"]),
                        undefined=StrictUndefined)

REQUIRED = ("salutation", "company", "peer_tier", "survey_link", "optout_link")


def template_base(kind: str) -> str:
    """invite -> invite templates; reminder and reminder2 share the reminder templates."""
    return "invite" if kind == "invite" else "reminder"


def render(kind: str, ctx: dict) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body)."""
    missing = [k for k in REQUIRED if not ctx.get(k)]
    if missing:
        raise ValueError(f"missing template variables: {missing}")
    c = {"postal_address": "", **ctx}
    base = template_base(kind)
    subject = _text_env.get_template(f"{base}_subject.txt.j2").render(**c).strip()
    subject = " ".join(subject.split())  # no newlines in headers
    text = _text_env.get_template(f"{base}.txt.j2").render(**c).strip() + "\n"
    html = _html_env.get_template(f"{base}.html.j2").render(subject=subject, **c)
    return subject, text, html
