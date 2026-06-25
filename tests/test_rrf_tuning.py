import json
from pathlib import Path
from foresight_mcp.rrf_tuning import RRFConfig, save_rrf_config, get_rrf_config

def test_save_rrf_config(tmp_path):
    # Setup test file path
    config_file = tmp_path / "test_rrf_config.json"

    # Create config
    config = RRFConfig(
        rrf_k=50.0,
        keyword_weight=0.8,
        tfidf_cosine_weight=0.6,
        graph_weight=0.5,
        temporal_weight=0.4,
        entity_weight=0.2,
    )

    # Save config
    save_rrf_config(config, str(config_file))

    # Verify file exists
    assert config_file.exists()

    # Verify content
    with open(config_file) as f:
        data = json.load(f)

    assert data["rrf_k"] == 50.0
    assert data["keyword"] == 0.8
    assert data["tfidf_cosine"] == 0.6
    assert data["graph"] == 0.5
    assert data["temporal"] == 0.4
    assert data["entity"] == 0.2

def test_get_rrf_config_existing_file(tmp_path):
    config_file = tmp_path / "test_rrf_config.json"
    config = RRFConfig(rrf_k=42.0)
    save_rrf_config(config, str(config_file))

    loaded_config = get_rrf_config(str(config_file))
    assert loaded_config.rrf_k == 42.0

def test_get_rrf_config_default():
    # Should use default config if file does not exist
    config = get_rrf_config("/non_existent_path.json")
    assert config.rrf_k == 60.0
    assert config.keyword_weight == 1.0

def test_save_rrf_config_default_path(tmp_path, monkeypatch):
    # Change DEFAULT_CONFIG_PATH
    import foresight_mcp.rrf_tuning as rrf_tuning

    default_path = tmp_path / "default_rrf_config.json"
    monkeypatch.setattr(rrf_tuning, "DEFAULT_CONFIG_PATH", default_path)

    config = RRFConfig(rrf_k=99.0)
    save_rrf_config(config)

    assert default_path.exists()

    loaded_config = get_rrf_config()
    assert loaded_config.rrf_k == 99.0
