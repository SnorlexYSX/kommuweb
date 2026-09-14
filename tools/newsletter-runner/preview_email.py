#!/usr/bin/env python3
"""Render newsletter HTML locally for browser preview (no SMTP, no sheet)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"
PREVIEW_DIR = ROOT / "preview"
REPO_ROOT = ROOT.parent.parent
NEWSLETTER_ASSET_BASE = "https://alexanderyeohsx.github.io/kommuweb/img/newsletter"
NEWSLETTER_ASSETS = REPO_ROOT / "img" / "newsletter"


def local_preview_assets(html: str) -> str:
    if not NEWSLETTER_ASSETS.is_dir():
        return html
    for path in sorted(NEWSLETTER_ASSETS.iterdir()):
        if not path.is_file():
            continue
        url = f"{NEWSLETTER_ASSET_BASE}/{path.name}"
        rel = Path(os.path.relpath(path, PREVIEW_DIR)).as_posix()
        html = html.replace(url, rel)
    return html


def wrap_email_html(body: str, email: str = "preview@example.com") -> str:
    shell_path = TEMPLATES / "_email_shell.html"
    if not shell_path.exists():
        return body
    shell = shell_path.read_text().replace("{{content}}", body)
    return shell.replace("{{unsubscribe_url}}", "#preview-unsubscribe")


def render(step_id: str, name: str = "there") -> str:
    html_path = TEMPLATES / f"{step_id}.html"
    if not html_path.exists():
        raise FileNotFoundError(html_path)
    body = html_path.read_text().replace("{{name}}", name)
    return local_preview_assets(wrap_email_html(body))


def main() -> None:
    default_steps = [
        "kommunity",
        "product_features",
        "use_cases",
        "kommu_app",
        "hardware_specs",
        "compatibility",
    ]
    steps = sys.argv[1:] or default_steps
    PREVIEW_DIR.mkdir(exist_ok=True)

    for step_id in steps:
        out = PREVIEW_DIR / f"{step_id}.html"
        out.write_text(render(step_id, "Kean"))
        print(out)

    print(f"\nOpen any file in {PREVIEW_DIR} in your browser.")


if __name__ == "__main__":
    main()
