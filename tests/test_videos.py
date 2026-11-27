import subprocess

import pytest
from fastapi.testclient import TestClient

from photocull.api import create_app
from tests.test_insights import BASE, URL, get, make_settings, payload, scan
from tests.worker_harness import start_worker, wait_for


def video_fixture(path, *, dark):
    source = "color=c=black:s=192x108:r=16" if dark else "testsrc2=s=192x108:r=16"
    audio = "anullsrc=channel_layout=mono:sample_rate=16000" if dark else "sine=frequency=600"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            source,
            "-f",
            "lavfi",
            "-i",
            audio,
            "-t",
            "6",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def test_video_ffmpeg_metrics_seek_and_flag_round_trip(tmp_path):
    settings = make_settings(tmp_path, video=True)
    root = settings.libraries["test"].root
    for name, dark in (("dark.mkv", True), ("motion.mov", False)):
        video_fixture(root / name, dark=dark)
    originals = {p: p.read_bytes() for p in root.iterdir()}
    with TestClient(create_app(settings)) as client, start_worker(settings):
        scan(client)
        assets = get(client, f"{BASE}/assets?kind=video")["items"]
        for item in assets:
            url = f"{URL}/assets/{item['id']}/video"

            def analyzed(url=url):
                result = get(client, url)
                assert result["video"]["status"] != "error", result
                return result if result["video"]["status"] == "done" else None

            result = wait_for(analyzed, timeout=30)
            data = result["video"]["data"]
            assert data["preview"], data
            assert data["duration"] >= 5
            preview = client.get(f"{url}/preview", headers={"Range": "bytes=0-127"})
            assert preview.status_code == 206
            assert preview.headers["content-type"] == "video/mp4"
            assert b"ftyp" in preview.content
            if item["path"] == "dark.mkv":
                assert data["dead"] is True
                assert "possible_pocket_recording" in data["reasons"]
                assert data["highlights"] == []
                assert payload(client.patch(url, json={"dead": False})) == {"override": False}
                assert get(client, url)["override"] is False
                assert payload(client.patch(url, json={"dead": None})) == {"override": None}
            else:
                assert data["dead"] is False
                assert not data["reasons"]
                assert data["highlights"] and data["highlights"][0]["end"] <= data["duration"]
        assert {p: p.read_bytes() for p in root.iterdir()} == originals


def test_webvtt_preserves_timing_and_escapes_transcript_markup():
    from photocull.api.insights import captions

    content = captions(
        [
            {"start": 61.25, "end": 64.8, "text": "A & B\n<c>caption"},
            {"start": 70, "end": 70, "text": "zero-length cue"},
        ]
    )
    assert content.startswith("WEBVTT\n\n")
    assert "00:01:01.250 --> 00:01:04.800" in content
    assert "A &amp; B &lt;c&gt;caption" in content
    assert "zero-length" not in content


def test_very_short_clip_without_audio_or_transcript_model(tmp_path):
    settings = make_settings(tmp_path, video=True, transcript=True, offline=True)
    clip = settings.libraries["test"].root / "短い映像.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=96x64:r=16",
            "-frames:v",
            "2",
            "-c:v",
            "mpeg4",
            str(clip),
        ],
        check=True,
        capture_output=True,
    )
    with TestClient(create_app(settings)) as client, start_worker(settings):
        scan(client)
        (asset,) = get(client, f"{BASE}/assets?kind=video")["items"]
        url = f"{URL}/assets/{asset['id']}/video"

        def done():
            result = get(client, url)
            for key in ("video", "transcript"):
                assert result[key]["status"] != "error", result
            return (
                result
                if all(result[key]["status"] == "done" for key in ("video", "transcript"))
                else None
            )

        result = wait_for(done)
        assert result["video"]["data"]["reasons"] == ["very_short"]
        assert result["video"]["data"]["preview"]
        assert result["transcript"]["data"] == {"segments": [], "has_audio": False}


@pytest.mark.parametrize(
    ("pattern", "duration", "volume", "moving", "expected", "dead"),
    [
        ("flat_profile", 6, None, True, [], False),
        ("night", 6, 0.1, True, ["mostly_dark"], False),
        ("dark_blur", 6, 0.1, True, ["mostly_dark", "low_detail_or_blur"], False),
        ("dark_blur", 6, 0.0, True, ["mostly_dark", "low_detail_or_blur"], True),
        (
            "black",
            6,
            0.0,
            False,
            ["mostly_dark", "possible_pocket_recording", "low_detail_or_blur", "static_and_quiet"],
            True,
        ),
        (
            "black",
            6,
            0.1,
            False,
            ["mostly_dark", "possible_pocket_recording", "low_detail_or_blur"],
            True,
        ),
        ("flat", 6, 0.1, False, ["low_detail_or_blur"], False),
        ("flat", 6, None, False, ["low_detail_or_blur", "static_and_quiet"], True),
        ("flat_profile", 6, None, False, ["static_and_quiet"], False),
        ("night", 2, None, False, ["mostly_dark"], False),
        ("night", 6, None, False, ["mostly_dark", "static_and_quiet"], True),
        ("flat_profile", 0.5, None, False, ["very_short"], True),
    ],
)
def test_clip_flags_and_review_hints_on_synthetic_frames(
    monkeypatch, pattern, duration, volume, moving, expected, dead
):
    import numpy as np

    from photocull.config import InsightSettings
    from photocull.insights import classify_video
    from photocull.worker.insights import video

    def frames(*_):
        for sample in range(int(duration * video.FRAME_RATE)):
            image = np.empty(video.FRAME, dtype=np.uint8)
            if pattern == "black":
                image.fill(0)
            elif pattern == "dark_blur":
                image.fill(22 if sample % 2 else 0)
            elif pattern == "flat":
                image.fill(100)
            else:
                low, high = (101, 104) if pattern == "flat_profile" else (2, 35)
                if moving and sample % 2:
                    low, high = high, low
                image[::2, :] = low
                image[1::2, :] = high
            yield image.tobytes()

    def levels(*_):
        while True:
            yield volume

    monkeypatch.setattr(video, "chunks", frames)
    monkeypatch.setattr(video, "audio_levels", levels)
    settings = InsightSettings()
    data = video.metrics("", -1, settings, duration, volume is not None, lambda: None)
    assert "reasons" not in data and "dead" not in data
    assert classify_video(data, settings) == (expected, dead)


def test_threshold_changes_reuse_metrics_and_proxy_across_restarts(tmp_path):
    import json

    from photocull.db import connect, write
    from photocull.db.insights import register
    from photocull.db.transfers import Selection, select_assets
    from photocull.insights import ModelIdentity, models
    from photocull.media.cache import video_path

    settings = make_settings(tmp_path, video=True)
    root = settings.libraries["test"].root
    video_fixture(root / "motion.mov", dark=False)
    state = settings.library_state("test")
    with TestClient(create_app(settings)) as client, start_worker(settings):
        scan(client)
        (asset,) = get(client, f"{BASE}/assets?kind=video")["items"]
        url = f"{URL}/assets/{asset['id']}/video"

        def done(client):
            result = get(client, url)["video"]
            assert result["status"] != "error", result
            return result["data"] if result["status"] == "done" else None

        assert not wait_for(lambda: done(client))["dead"]

    proxy = video_path(state, asset["id"], asset["content_hash"])
    inode = proxy.stat().st_ino
    keys = {"video": models(settings)["video"].key}
    with connect(state / "index.db") as db:
        original = db.execute("SELECT payload FROM insight_results WHERE stage='video'").fetchone()
        metrics = json.loads(original[0])
        assert "pocket_fraction" in metrics and "reasons" not in metrics and "dead" not in metrics

    changed = settings.model_copy(
        update={"insights": settings.insights.model_copy(update={"video_very_short_s": 8})}
    )
    with (
        TestClient(create_app(changed)) as client,
        start_worker(changed),
        connect(state / "index.db") as db,
    ):
        wait_for(lambda: db.execute("SELECT 1 FROM worker_status").fetchone())
        result = done(client)
        assert result["dead"] and result["reasons"] == ["very_short"]
        assert tuple(
            db.execute("SELECT payload FROM insight_results WHERE stage='video'").fetchone()
        ) == tuple(original)
        assert not db.execute("SELECT 1 FROM insight_work WHERE stage='video'").fetchone()

        def flagged(settings):
            query, args = select_assets(
                db, Selection(dead=True, minimum_rating=0), keys, settings.insights
            )
            return [row["id"] for row in db.execute(query, args)]

        assert flagged(settings) == []
        assert flagged(changed) == [asset["id"]]
        payload(client.patch(url, json={"dead": False}))
        assert flagged(changed) == []

    assert proxy.stat().st_ino == inode
    with connect(state / "index.db") as db, write(db):
        legacy = register(db, ModelIdentity("photocull/ffmpeg-video", "1"))
        db.execute("UPDATE insight_stages SET key=? WHERE stage='video'", (legacy,))
        db.execute("UPDATE insight_results SET key=? WHERE stage='video'", (legacy,))
    legacy_proxy = proxy.with_name(f"{asset['id']}-{asset['content_hash']}-{legacy}.mp4")
    stale_proxy = proxy.with_name(f"{asset['id']}-{'f' * 64}-{legacy}.mp4")
    proxy.rename(legacy_proxy)
    stale_proxy.write_bytes(b"obsolete proxy")
    with TestClient(create_app(changed)) as client, start_worker(changed):
        assert wait_for(lambda: done(client))["preview"]
        assert client.get(f"{url}/preview", headers={"Range": "bytes=0-15"}).status_code == 206
        assert proxy.stat().st_ino == inode
        assert not legacy_proxy.exists()
        assert not stale_proxy.exists()
