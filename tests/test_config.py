from photocull.config import load_settings


def test_settings_yaml_dotenv_and_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.yaml").write_text("""
libraries:
  sony:
    root: links
    allowed_roots: [originals]
state_dir: state
worker:
  batch_size: 4
""")
    (tmp_path / ".env").write_text(
        "PHOTOCULL_CONFIG=config/settings.yaml\nPHOTOCULL_BIND_PORT=9999\n"
    )
    monkeypatch.setenv("PHOTOCULL_BIND_PORT", "8001")
    monkeypatch.setenv("PHOTOCULL_WORKER_STAGES", "exif")
    settings = load_settings()
    assert settings.libraries["sony"].roots == (config_dir / "links", config_dir / "originals")
    assert settings.library_state("sony") == config_dir / "state/libraries/sony"
    assert settings.bind_port == 8001
    assert settings.worker.batch_size == 4
    assert settings.worker.stages == ("exif",)
