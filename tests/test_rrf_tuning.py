from pathlib import Path

from foresight.rrf_tuning import RRFConfig, get_rrf_config, save_rrf_config


def test_get_rrf_config_default(tmp_path):
    """Test getting default config when file doesn't exist."""
    defaults = RRFConfig()
    config = get_rrf_config(str(tmp_path / "nonexistent.json"))
    assert config.rrf_k == defaults.rrf_k
    assert config.keyword_weight == defaults.keyword_weight


def test_save_and_get_rrf_config(tmp_path):
    """Test saving a config to a specific path and loading it back."""
    config_path = tmp_path / "test_config.json"

    rrf_k = 50.0
    keyword_weight = 2.0
    config = RRFConfig(rrf_k=rrf_k, keyword_weight=keyword_weight)
    save_rrf_config(config, str(config_path))

    assert config_path.exists()

    loaded_config = get_rrf_config(str(config_path))
    assert loaded_config.rrf_k == rrf_k
    assert loaded_config.keyword_weight == keyword_weight
    assert loaded_config.tfidf_cosine_weight == RRFConfig().tfidf_cosine_weight  # Defaults maintained


def test_get_rrf_config_corrupted_file(tmp_path):
    """Test that a corrupted JSON file falls back to defaults."""
    config_path = tmp_path / "corrupted.json"
    config_path.write_text("{invalid json")

    config = get_rrf_config(str(config_path))
    assert config.rrf_k == RRFConfig().rrf_k  # Returns default on error


def test_save_rrf_config_default_path(monkeypatch, tmp_path):
    """Test saving to the default path when no path is provided."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home_dir)

    # We need to patch DEFAULT_CONFIG_PATH since it is defined at the module level
    expected_path = home_dir / ".foresight" / "rrf_config.json"
    import foresight.rrf_tuning

    monkeypatch.setattr(foresight.rrf_tuning, "DEFAULT_CONFIG_PATH", expected_path)

    rrf_k = 42.0
    config = RRFConfig(rrf_k=rrf_k)
    save_rrf_config(config)

    assert expected_path.exists()

    loaded_config = get_rrf_config()
    assert loaded_config.rrf_k == rrf_k
