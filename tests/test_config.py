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


def test_tuning_defaults_and_configurable_insight_identity(tmp_path, monkeypatch):
    from photocull.config import InsightSettings, ScoringSettings, Settings
    from photocull.insights import models

    assert ScoringSettings().model_dump() == {
        "min_sharpness": 0.02,
        "min_exposure": 0.012,
        "max_exposure": 0.95,
        "max_clipped_highlights": 0.85,
        "max_clipped_shadows": 0.98,
        "min_detail_when_clipped": 0.05,
    }
    defaults = InsightSettings(faces=True, video=True)
    assert (defaults.face_detect_score, defaults.face_min_px, defaults.face_threshold) == (
        0.85,
        12,
        0.45,
    )
    assert (defaults.face_min_quality, defaults.people_min_faces) == (0.12, 2)
    assert (
        defaults.video_very_short_s,
        defaults.video_dark_fraction,
        defaults.video_pocket_fraction,
        defaults.video_blur_fraction,
        defaults.video_static_motion,
        defaults.video_static_volume,
    ) == (1.5, 0.65, 0.5, 0.8, 0.008, 0.012)
    baseline = models(Settings(insights=defaults))
    monkeypatch.chdir(tmp_path)
    for field, value in (
        ("FACES", "true"),
        ("VIDEO", "true"),
        ("FACE_DETECT_SCORE", "0.9"),
        ("FACE_MIN_PX", "16"),
        ("FACE_MIN_QUALITY", "0.2"),
        ("VIDEO_STATIC_VOLUME", "0.02"),
    ):
        monkeypatch.setenv(f"PHOTOCULL_INSIGHTS_{field}", value)
    settings = load_settings()
    config = settings.insights
    assert (config.face_detect_score, config.face_min_px, config.face_min_quality) == (0.9, 16, 0.2)
    assert config.video_static_volume == 0.02
    updated = models(settings)
    assert updated["faces"].key != baseline["faces"].key
    assert updated["video"].key != baseline["video"].key
