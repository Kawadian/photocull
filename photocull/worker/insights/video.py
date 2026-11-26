from __future__ import annotations

import heapq
import json
import math
import os
import select
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from photocull.media.cache import video_path
from photocull.media.decode import VIDEO_FORMATS
from photocull.media.walk import MediaJail, SourceChanged, source_key

FRAME = (90, 160)
FRAME_RATE = 2
WINDOW = 4
SPEECH_SECONDS = 30


@contextmanager
def command(args, fd):
    # All input is pinned to a regular jailed descriptor. Only local protocols are allowed.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=errors,
            pass_fds=(fd,),
        )
        try:
            yield process.stdout
            if process.wait(timeout=5):
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell() - 2048))
                detail = errors.read().decode(errors="replace").strip()
                raise ValueError(detail or f"{args[0]} exited with code {process.returncode}")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()


def chunks(args, fd, size, pulse):
    with command(args, fd) as output:
        buffer = bytearray()
        activity = time.monotonic()
        while True:
            pulse()
            if not select.select([output], [], [], 0.15)[0]:
                if time.monotonic() - activity > 120:
                    raise ValueError(f"{args[0]} stopped producing output for two minutes")
                continue
            data = os.read(output.fileno(), min(65536, size - len(buffer)))
            if not data:
                if buffer:
                    yield bytes(buffer)
                break
            activity = time.monotonic()
            buffer.extend(data)
            if len(buffer) == size:
                yield bytes(buffer)
                buffer.clear()


def probe(source, fd, pulse):
    args = [
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        VIDEO_FORMATS,
        "-max_streams",
        "128",
        "-show_entries",
        "stream=codec_type,duration:stream_disposition=attached_pic:format=duration",
        "-of",
        "json",
        source,
    ]
    blob = b"".join(chunks(args, fd, 1024 * 1024, pulse))
    if not blob:
        raise ValueError("ffprobe produced no metadata")
    result = json.loads(blob)
    streams = result.get("streams", [])
    video = next(
        (
            stream
            for stream in streams
            if stream["codec_type"] == "video"
            and not stream.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        raise ValueError("The file has no playable video track")
    duration = 0.0
    for value in (video.get("duration"), result.get("format", {}).get("duration")):
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            duration = number
            break
    return duration, any(stream["codec_type"] == "audio" for stream in streams)


def ffmpeg(source, threads):
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-filter_threads",
        "1",
        "-threads",
        str(threads),
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        VIDEO_FORMATS,
        "-max_streams",
        "128",
        "-i",
        source,
    ]


def audio_levels(source, fd, threads, pulse):
    import numpy as np

    rate = 8000
    args = [*ffmpeg(source, threads), "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "pipe:1"]
    for data in chunks(args, fd, rate * WINDOW * 2, pulse):
        samples = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float32)
        yield float(np.sqrt(np.mean(samples**2)) / 32768) if len(samples) else 0


def metrics(source, fd, threads, duration, has_audio, pulse):
    import numpy as np

    args = [
        *ffmpeg(source, threads),
        "-map",
        "0:V:0",
        "-an",
        "-vf",
        "fps=2,scale=160:90,format=gray",
        "-threads",
        "1",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    levels = audio_levels(source, fd, threads, pulse) if has_audio else iter(())
    count = black = blurry = pocket = cuts = 0
    motion_sum = volume_sum = 0.0
    candidates = []
    prior = None
    batch = []

    def add_window():
        nonlocal volume_sum
        volume = next(levels, 0)
        volume_sum += volume * len(batch)
        motion, darkness, blur, scene = np.mean(batch, axis=0)
        score = float(
            0.48 * min(1, motion / 0.16)
            + 0.20 * min(1, volume / 0.12)
            + 0.22 * (1 - blur)
            + 0.10 * min(1, scene * 4)
            - 0.8 * darkness
        )
        if score >= 0.22 and (motion >= 0.012 or volume >= 0.02 or scene > 0):
            item = (score, -(count - len(batch)) / FRAME_RATE, count / FRAME_RATE)
            if len(candidates) < 40:
                heapq.heappush(candidates, item)
            elif item > candidates[0]:
                heapq.heapreplace(candidates, item)
        batch.clear()

    try:
        for data in chunks(args, fd, math.prod(FRAME), pulse):
            if len(data) != math.prod(FRAME):
                break
            image = np.frombuffer(data, dtype=np.uint8).reshape(FRAME).astype(np.float32) / 255
            darkness = float(np.mean(image)) < 0.09
            edges = float(np.mean(np.abs(np.diff(image, axis=0))))
            blur = edges < 0.012
            motion = float(np.mean(np.abs(prior - image))) if prior is not None else 0.0
            scene = motion > 0.24 and not darkness
            count += 1
            black += darkness
            blurry += blur
            pocket += darkness and blur and motion < 0.05
            cuts += scene
            motion_sum += motion
            batch.append((motion, darkness, blur, scene))
            prior = image
            if len(batch) == FRAME_RATE * WINDOW:
                add_window()
        if batch:
            add_window()
    finally:
        if has_audio:
            levels.close()
    duration = duration or count / FRAME_RATE
    reasons = []
    if duration < 1.5:
        reasons.append("very_short")
    if black / max(1, count) >= 0.65:
        reasons.append("mostly_dark")
    if pocket / max(1, count) >= 0.50:
        reasons.append("possible_pocket_recording")
    if blurry / max(1, count) >= 0.80:
        reasons.append("low_detail_or_blur")
    if duration >= 4 and motion_sum / max(1, count) < 0.008 and volume_sum / max(1, count) < 0.012:
        reasons.append("static_and_quiet")
    ranked = sorted(candidates, reverse=True)[:12]
    highlights = []
    for score, start, end in sorted(ranked, key=lambda item: -item[1]):
        start = -start
        end = min(duration, end)
        if end <= start:
            continue
        if highlights and highlights[-1]["end"] == start and end - highlights[-1]["start"] <= 16:
            highlights[-1]["end"] = end
            highlights[-1]["score"] = max(score, highlights[-1]["score"])
        else:
            highlights.append({"start": start, "end": end, "score": score})
    return {
        "duration": duration,
        "highlights": highlights,
        "reasons": reasons,
        "motion": motion_sum / max(1, count),
        "volume": volume_sum / max(1, count),
        "dark_fraction": black / max(1, count),
        "blur_fraction": blurry / max(1, count),
        "scene_changes": cuts,
        "has_audio": has_audio,
    }


def proxy(source, fd, threads, target, pulse):
    target.parent.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".video-", delete=False) as tmp:
        staged = Path(tmp.name)
    try:
        args = [
            *ffmpeg(source, threads),
            "-map",
            "0:V:0",
            "-map",
            "0:a:0?",
            "-map_metadata",
            "-1",
            "-vf",
            "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease:"
            "force_divisible_by=2,format=yuv420p",
            "-c:v",
            "libx264",
            "-threads",
            str(threads),
            "-preset",
            "veryfast",
            "-crf",
            "25",
            "-maxrate",
            "2M",
            "-bufsize",
            "4M",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-f",
            "mp4",
            "-y",
            str(staged),
        ]
        for _ in chunks(args, fd, 1024, pulse):
            pass
        staged.replace(target)
    finally:
        staged.unlink(missing_ok=True)


def transcribe(worker, source, fd, audio, pulse):
    if not audio:
        return {"segments": [], "has_audio": False}
    config = worker.settings.insights
    args = [
        *ffmpeg(source, config.threads),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "s16le",
        "pipe:1",
    ]
    result = []
    inference = worker.ml.inference
    for index, data in enumerate(chunks(args, fd, SPEECH_SECONDS * 16000 * 2, pulse)):
        offset = index * SPEECH_SECONDS
        segments = inference.call(
            "transcript",
            (
                data,
                config.transcript_model,
                config.transcript_revision,
                config.offline,
                config.threads,
            ),
            worker.pulse,
            timeout=300,
        )
        result.extend(
            {"start": offset + start, "end": offset + end, "text": text}
            for start, end, text in segments
        )
    return {"segments": result, "has_audio": True}


def run_stage(worker, stage, asset, key, pulse):
    jail = MediaJail(worker.library, worker.settings.state_dir)
    target = None
    config = worker.settings.insights
    with jail.open_media(asset["path"]) as (fd, _):
        if source_key(os.fstat(fd)) != asset["source_key"]:
            raise SourceChanged("Media changed since scan; rescan the library")
        source = f"/proc/self/fd/{fd}"
        duration, audio = probe(source, fd, pulse)
        if stage == "transcript":
            results = transcribe(worker, source, fd, audio, pulse)
        else:
            results = metrics(source, fd, config.threads, duration, audio, pulse)
            target = video_path(worker.state, asset["id"], asset["content_hash"], key)
            try:
                proxy(source, fd, config.threads, target, pulse)
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                target.unlink(missing_ok=True)
                results["preview_error"] = str(error)
            results["preview"] = target.is_file()
        with jail.open_media(asset["path"]) as (current, _):
            if source_key(os.fstat(current)) != asset["source_key"]:
                if target:
                    target.unlink(missing_ok=True)
                raise SourceChanged("Media changed during video analysis; rescan the library")
    if target:
        for expired in target.parent.glob(f"{asset['id']}-*.mp4"):
            if expired != target:
                expired.unlink(missing_ok=True)
    return results
