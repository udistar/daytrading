"""API 키는 환경변수 또는 홈의 secrets.env 에서만 읽는다."""

from __future__ import annotations

import os
from pathlib import Path

SECRET_KEYS = ("KIWOOM_APP_KEY", "KIWOOM_APP_SECRET", "KIWOOM_ACCOUNT_NO")


def load_secrets(home: Path) -> dict[str, str]:
    found = {key: os.environ.get(key, "").strip() for key in SECRET_KEYS}
    path = home / "secrets.env"
    if not path.exists():
        return found
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in found and not os.environ.get(key, "").strip():
            found[key] = value.strip().strip('"').strip("'")
    return found


def missing_secret_names(secrets: dict[str, str]) -> list[str]:
    return [key for key in SECRET_KEYS if not secrets.get(key)]
