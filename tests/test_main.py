from daytrading.__main__ import main
from daytrading.settings import SettingsStore, default_settings


def test_check_cli_does_not_place_orders(tmp_path, capsys, monkeypatch):
    from daytrading.paper import CheckResult

    def fake_check(home, settings, **kwargs):
        return CheckResult(True, "모의투자 접속 확인. 주문은 내지 않았습니다.", orders_sent=0)

    monkeypatch.setattr("daytrading.paper.check_from_home", fake_check)
    assert main(["--check", "--home", str(tmp_path)]) == 0
    assert "주문은 내지 않았습니다" in capsys.readouterr().out
    assert main(["--dry-run", "--home", str(tmp_path)]) == 0


def test_sim_cli_prints_a_report(tmp_path, capsys):
    code = main(["--sim", "--home", str(tmp_path)])
    assert code == 0
    text = capsys.readouterr().out
    assert "morning" in text
    assert "daily_loss" in text
    assert "vi" in text
    assert list((tmp_path / "logs").iterdir())
    assert SettingsStore(tmp_path).load().to_dict() == default_settings().to_dict()
