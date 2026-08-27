import json

import pytest

from api.server_config import DEFAULT_HOST, DEFAULT_PORT, load_server_config


def test_load_server_config_reads_host_and_port(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"host": "192.168.1.20", "port": 8123}),
        encoding="utf-8",
    )

    config = load_server_config(config_path)

    assert config.host == "192.168.1.20"
    assert config.port == 8123
    assert config.public_url == "http://192.168.1.20:8123"


def test_load_server_config_uses_defaults_when_file_is_missing(tmp_path):
    config = load_server_config(tmp_path / "missing.json")

    assert config.host == DEFAULT_HOST
    assert config.port == DEFAULT_PORT
    assert config.public_url == "http://localhost:8000"


@pytest.mark.parametrize(
    "values",
    [
        {"host": "", "port": 8000},
        {"host": "0.0.0.0", "port": 0},
        {"host": "0.0.0.0", "port": 65536},
        {"host": "0.0.0.0", "port": "8000"},
    ],
)
def test_load_server_config_rejects_invalid_values(tmp_path, values):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(values), encoding="utf-8")

    with pytest.raises(ValueError):
        load_server_config(config_path)
