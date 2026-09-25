"""실행 진입점. python -m daytrading 은 화면, --sim 은 키 없는 시뮬레이션."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from daytrading.settings import SettingsError, SettingsStore, default_home


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="초단타 데이트레이딩 v0 (모의투자)")
    parser.add_argument("--sim", action="store_true", help="내장 시뮬레이션을 돌리고 손익 요약을 출력합니다.")
    parser.add_argument("--home", type=Path, default=None, help="설정·로그 폴더. 비우면 사용자 홈의 .daytrading")
    args = parser.parse_args(argv)
    home = args.home or default_home()
    try:
        settings = SettingsStore(home).load()
    except SettingsError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.sim:
        from daytrading.sim import render_report, run_builtin

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_dir = home / "logs" / stamp
        summaries = run_builtin(log_dir, settings)
        print(render_report(summaries), end="")
        print(f"로그: {log_dir}")
        return 0
    from daytrading.ui import launch

    return launch(home)


if __name__ == "__main__":
    raise SystemExit(main())
