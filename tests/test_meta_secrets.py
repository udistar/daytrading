import os
from datetime import date

from daytrading.meta import load_symbol_meta
from daytrading.secrets import load_secrets, missing_secret_names


def test_symbol_meta_csv(tmp_path):
    path = tmp_path / "symbols_meta.csv"
    path.write_text(
        "code,name,listed_on,is_admin,is_caution,is_warning,is_risk,is_overheat,is_cleanup,is_etf,is_etn,is_spac,is_preferred\n"
        "005930,삼성전자,20200102,0,예,0,0,0,0,0,0,0,0\n",
        encoding="utf-8",
    )
    rows = load_symbol_meta(path)
    assert rows["005930"]["listed_on"] == date(2020, 1, 2)
    assert rows["005930"]["is_caution"] is True
    assert rows["005930"]["is_etf"] is False


def test_secrets_file_and_env_override(tmp_path, monkeypatch):
    for key in ("KIWOOM_APP_KEY", "KIWOOM_APP_SECRET", "KIWOOM_ACCOUNT_NO"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "secrets.env").write_text(
        "KIWOOM_APP_KEY=paper-key\nKIWOOM_APP_SECRET='paper-secret'\n# ignore\n",
        encoding="utf-8",
    )
    found = load_secrets(tmp_path)
    assert found["KIWOOM_APP_KEY"] == "paper-key"
    assert found["KIWOOM_APP_SECRET"] == "paper-secret"
    assert "KIWOOM_ACCOUNT_NO" in missing_secret_names(found)
    monkeypatch.setenv("KIWOOM_APP_KEY", "from-env")
    assert load_secrets(tmp_path)["KIWOOM_APP_KEY"] == "from-env"
    assert "paper-key" not in os.environ["KIWOOM_APP_KEY"]
