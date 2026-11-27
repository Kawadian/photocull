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
  scan_batch_size: 128
scoring:
  min_sharpness: 0.04
burst:
  max_hamming_distance: 12
""")
    (tmp_path / ".env").write_text(
        "PHOTOCULL_CONFIG=config/settings.yaml\nPHOTOCULL_BIND_PORT=9999\n"
    )
    monkeypatch.setenv("PHOTOCULL_BIND_PORT", "8001")
    monkeypatch.setenv("PHOTOCULL_WORKER_STAGES", "exif")
    monkeypatch.setenv("PHOTOCULL_WORKER_SCAN_PARALLELISM", "2")
    monkeypatch.setenv("PHOTOCULL_WORKER_HASH_READ_MIB", "8")
    monkeypatch.setenv("PHOTOCULL_WORKER_SCAN_OVERLAP", "true")
    monkeypatch.setenv("PHOTOCULL_BURST_WINDOW_SECONDS", "2.5")
    monkeypatch.setenv("PHOTOCULL_SCORING_MAX_EXPOSURE", "0.85")
    settings = load_settings()
    assert settings.libraries["sony"].roots == (config_dir / "links", config_dir / "originals")
    assert settings.library_state("sony") == config_dir / "state/libraries/sony"
    assert settings.bind_port == 8001
    assert settings.worker.batch_size == 4
    assert settings.worker.scan_batch_size == 128
    assert settings.worker.scan_parallelism == 2
    assert settings.worker.hash_read_mib == 8
    assert settings.worker.scan_overlap
    assert settings.worker.stages == ("exif",)
    assert settings.scoring.min_sharpness == 0.04
    assert settings.scoring.max_exposure == 0.85
    assert settings.burst.max_hamming_distance == 12
    assert settings.burst.window_seconds == 2.5
