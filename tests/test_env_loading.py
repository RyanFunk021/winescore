import os

from app.main import load_local_env


def test_load_local_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        'DATABASE_URL="postgresql://example:pass@db.example.com:6543/postgres?sslmode=require"\n'
        'HOST_PIN="1234"\n'
    )

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HOST_PIN", raising=False)

    load_local_env(env_file)

    assert os.environ["DATABASE_URL"] == "postgresql://example:pass@db.example.com:6543/postgres?sslmode=require"
    assert os.environ["HOST_PIN"] == "1234"
