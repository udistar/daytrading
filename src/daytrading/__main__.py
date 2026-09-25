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
    parser.add_argument("--check", action="store_true", help="모의투자 토큰과 시세 등록만 확인하고 주문은 내지 않습니다.")
    parser.add_argument("--paper", action="store_true", help="키움 모의투자 서버에 접속해 같은 매매 엔진을 돌립니다.")
    parser.add_argument("--dry-run", action="store_true", help="--paper 와 함께 쓰면 주문 API를 호출하지 않습니다. 단독이면 --check 와 같습니다.")
    parser.add_argument("--home", type=Path, default=None, help="설정·로그 폴더. 비우면 사용자 홈의 .daytrading")
    args = parser.parse_args(argv)
    home = args.home or default_home()
    try:
        settings = SettingsStore(home).load()
    except SettingsError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.check or (args.dry_run and not args.paper):
        return _run_check(home, settings)
    if args.paper:
        return _run_paper(home, settings, dry_run=args.dry_run)
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


def _run_check(home: Path, settings) -> int:
    from daytrading.paper import check_from_home

    result = check_from_home(home, settings)
    print(result.message)
    if result.orders_sent:
        print("연결 확인 중 주문이 나갔습니다.", file=sys.stderr)
        return 1
    return 0 if result.ok else 1


def _run_paper(home: Path, settings, dry_run: bool) -> int:
    from datetime import datetime

    from daytrading.journal import Journal
    from daytrading.kiwoom import KiwoomError
    from daytrading.paper import run_paper
    from daytrading.secrets import load_secrets, missing_secret_names

    secrets = load_secrets(home)
    missing = [name for name in missing_secret_names(secrets) if name != "KIWOOM_ACCOUNT_NO"]
    if missing:
        print("모의투자 키가 없습니다. " + ", ".join(missing), file=sys.stderr)
        return 1
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = home / "logs" / f"paper-{stamp}"
    journal = Journal(log_dir)
    try:
        run_paper(
            settings,
            journal,
            secrets["KIWOOM_APP_KEY"],
            secrets["KIWOOM_APP_SECRET"],
            dry_run=dry_run,
            meta_path=home / "symbols_meta.csv",
        )
    except KiwoomError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("모의투자를 멈췄습니다.")
    finally:
        journal.close()
    print(f"로그: {log_dir}")
    if dry_run:
        print("모의투자 시세만 반영했습니다. 주문 API는 호출하지 않았습니다.")
    else:
        print("주문은 모의투자 서버(mockapi.kiwoom.com)로만 나갑니다. 실전 주소는 잠겨 있습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
