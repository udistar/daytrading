from daytrading.__main__ import main
from daytrading.settings import SettingsStore, default_settings


def test_sim_cli_prints_a_report(tmp_path, capsys):
    code = main(["--sim", "--home", str(tmp_path)])
    assert code == 0
    text = capsys.readouterr().out
    assert "morning" in text
    assert "daily_loss" in text
    assert "vi" in text
    assert list((tmp_path / "logs").iterdir())
    assert SettingsStore(tmp_path).load().to_dict() == default_settings().to_dict()
