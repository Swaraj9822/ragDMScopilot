"""Verify seeded accounts authenticate via the app's AuthService.authenticate."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
sys.path.insert(0, str(ROOT / "src"))

CHECKS = [
    ("swaraj.bagal23@gmail.com", "Swaraj7709"),
    ("admin@email.com", "admin123"),
]


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def main() -> int:
    load_env(ENV_PATH)

    from rag_system.auth.service import AuthService
    from rag_system.config import get_settings

    settings = get_settings()
    svc = AuthService(settings)

    for email, password in CHECKS:
        try:
            record = svc.authenticate(email, password)
            print(f"AUTH OK   {email}  (id={record.id[:8]}..., operator={record.is_operator})")
        except Exception as exc:  # noqa: BLE001
            print(f"AUTH FAIL {email}  -> {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
