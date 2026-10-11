import stat
from pathlib import Path

import pytest

from dayforge.core.secrets import EnvFile


def test_set_creates_file_with_private_permissions(tmp_path: Path) -> None:
    env = EnvFile(tmp_path / ".env")

    env.set("STRIPE_API_KEY", "sk_test_123")

    assert (tmp_path / ".env").read_text() == 'STRIPE_API_KEY="sk_test_123"\n'
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    assert env.has("STRIPE_API_KEY")


def test_set_replaces_value_and_keeps_other_lines(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# comment\nDEBUG=1\nexport TOKEN='old'\n")
    env = EnvFile(path)

    env.set("TOKEN", "new")
    env.set("OTHER", "x")

    assert path.read_text() == '# comment\nDEBUG=1\nTOKEN="new"\nOTHER="x"\n'
    assert env.names() == {"DEBUG", "TOKEN", "OTHER"}


@pytest.mark.parametrize("value", ['with "quotes"', "multi\nline", "back\\slash\\n", "плюс="])
def test_values_roundtrip(tmp_path: Path, value: str) -> None:
    env = EnvFile(tmp_path / ".env")

    env.set("SECRET_VALUE", value)

    assert env.values() == [value]


def test_invalid_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        EnvFile(tmp_path / ".env").set("lower_case", "x")


def test_missing_file_is_empty(tmp_path: Path) -> None:
    env = EnvFile(tmp_path / ".env")

    assert env.names() == set()
    assert env.values() == []
