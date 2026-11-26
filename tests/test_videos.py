import subprocess

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
                assert "possible_pocket_recording" in data["reasons"]
                assert data["highlights"] == []
                assert payload(client.patch(url, json={"dead": False})) == {"override": False}
                assert get(client, url)["override"] is False
                assert payload(client.patch(url, json={"dead": None})) == {"override": None}
            else:
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
