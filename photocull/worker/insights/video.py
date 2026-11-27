from __future__ import annotations

import heapq
import json
import logging
import math
import os
import re
import selectors
import stat
import subprocess
import time
from contextlib import closing, contextmanager
from pathlib import Path

from photocull.config import InsightSettings
from photocull.media.artifacts import prune_artifacts, stage_artifact
from photocull.media.cache import video_path
from photocull.media.decode import VIDEO_FORMATS
from photocull.media.walk import MediaJail, SourceChanged, open_directory, source_key

LOG = logging.getLogger(__name__)
FRAME = (90, 160)
FRAME_RATE = 2
WINDOW = 4
SPEECH_SECONDS = 30


def chunks(args, fd, size, pulse, *, output_fds=()):
    # Only local formats are allowed and FFmpeg reads through a pinned source FD.
    # Drain stderr alongside stdout and keep just its tail for an actionable error.
    with (
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(fd, *output_fds),
        ) as process,
        selectors.DefaultSelector() as streams,
    ):
        assert process.stdout is not None and process.stderr is not None
        streams.register(process.stdout, selectors.EVENT_READ, data=True)
        streams.register(process.stderr, selectors.EVENT_READ, data=False)
        buffer, errors = bytearray(), bytearray()
        activity = time.monotonic()
        try:
            while streams.get_map():
                pulse()
                for stream, _ in streams.select(0.15):
                    data = os.read(
                        stream.fd, min(65536, size - len(buffer)) if stream.data else 4096
                    )
                    if not data:
                        streams.unregister(stream.fileobj)
                    elif stream.data:
                        activity = time.monotonic()
                        buffer.extend(data)
                        if len(buffer) == size:
                            yield bytes(buffer)
                            buffer.clear()
                    else:
                        errors.extend(data)
                        del errors[:-2048]
                if time.monotonic() - activity > 120:
                    raise ValueError(f"{args[0]} stopped producing output for two minutes")
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                raise ValueError(f"{args[0]} did not exit after closing its output") from error
            if code:
                detail = errors.decode(errors="replace").strip()
                raise ValueError(detail or f"{args[0]} exited with code {code}")
            if buffer:
                yield bytes(buffer)
        finally:
            if process.poll() is None:
                process.kill()


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
            if stream.get("codec_type") == "video"
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
    return duration, any(stream.get("codec_type") == "audio" for stream in streams)


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
    args = [
        *ffmpeg(source, threads),
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        str(rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    with closing(chunks(args, fd, rate * WINDOW * 2, pulse)) as audio:
        for data in audio:
            samples = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float32)
            yield float(np.sqrt(np.mean(samples**2)) / 32768) if len(samples) else 0


def metrics(source, fd, config: InsightSettings, duration, has_audio, pulse):
    import numpy as np

    args = [
        *ffmpeg(source, config.threads),
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
    levels = audio_levels(source, fd, config.threads, pulse) if has_audio else iter(())
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

    frames = chunks(args, fd, math.prod(FRAME), pulse)
    try:
        for data in frames:
            if len(data) != math.prod(FRAME):
                break
            image = np.frombuffer(data, dtype=np.uint8).reshape(FRAME).astype(np.float32) / 255
            darkness = float(np.mean(image)) < 0.09
            edges = float(np.mean(np.abs(np.diff(image, axis=0))))
            # Scale the edge cutoff down for flat profiles while keeping the
            # original cutoff for normal contrast and a floor for near-flat noise.
            contrast = max(0.025, min(float(image.std()), 0.1))
            blur = edges < 0.12 * contrast
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
        frames.close()
        if has_audio:
            levels.close()
    duration = duration or count / FRAME_RATE
    if not duration:
        raise ValueError("The clip has no decodable video frames")
    samples = max(1, count)
    motion, volume = motion_sum / samples, volume_sum / samples
    dark_fraction, blur_fraction = black / samples, blurry / samples
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
        "motion": motion,
        "volume": volume,
        "dark_fraction": dark_fraction,
        "pocket_fraction": pocket / samples,
        "blur_fraction": blur_fraction,
        "scene_changes": cuts,
        "has_audio": has_audio,
    }


@contextmanager
def proxy(
    source,
    fd,
    threads,
    target,
    pulse,
    *,
    limits=(1280, 720, "2M", "96k"),
    max_bytes=None,
    coordinates=None,
):
    width, height, bitrate, audio = limits
    location = []
    if coordinates is not None:
        latitude, longitude = coordinates
        location = ["-metadata", f"location={latitude:+09.5f}{longitude:+010.5f}/"]
    with stage_artifact(target, ".video-") as tmp:
        output = tmp.fileno()
        args = [
            *ffmpeg(source, threads),
            "-map",
            "0:V:0",
            "-map",
            "0:a:0?",
            "-map_metadata",
            "-1",
            "-map_metadata:s",
            "-1",
            "-map_chapters",
            "-1",
            "-vf",
            f"scale='min({width},iw)':'min({height},ih)':force_original_aspect_ratio=decrease:"
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
            bitrate,
            "-bufsize",
            "4M" if bitrate == "2M" else "1700k",
            "-c:a",
            "aac",
            "-b:a",
            audio,
            *location,
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-f",
            "mp4",
            "-y",
            f"/proc/self/fd/{output}",
        ]

        def check():
            pulse()
            if max_bytes is not None and os.fstat(output).st_size > max_bytes:
                raise ValueError("Video exceeds the derivative cache limit")

        for _ in chunks(args, fd, 1024, check, output_fds=(output,)):
            pass
        yield Path(tmp.name)


def transcribe(worker, source, fd, audio, pulse):
    if not audio:
        return {"segments": [], "has_audio": False}
    config = worker.settings.insights
    args = [
        *ffmpeg(source, config.threads),
        "-map",
        "0:a:0",
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
    with closing(chunks(args, fd, SPEECH_SECONDS * 16000 * 2, pulse)) as blocks:
        for index, data in enumerate(blocks):
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
            for start, end, text in segments:
                end = min(end, len(data) / 32000)
                if 0 <= start < end:
                    result.append({"start": offset + start, "end": offset + end, "text": text})
    return {"segments": result, "has_audio": True}


def migrate_video_proxies(db, state: Path, pulse) -> None:
    directory = state / "video"
    legacy_name = re.compile(r"([1-9][0-9]{0,18})-([0-9a-f]{64})-[0-9a-f]{64}\.mp4")
    try:
        with open_directory(directory.absolute()) as parent, os.scandir(parent) as entries:
            for entry in entries:
                pulse()
                match = legacy_name.fullmatch(entry.name)
                if not match or entry.is_dir(follow_symlinks=False):
                    continue
                asset_id, digest = match.groups()
                current = db.execute(
                    """SELECT 1 FROM assets WHERE id = ? AND active = 1
                       AND kind = 'video' AND content_hash = ?""",
                    (asset_id, digest),
                ).fetchone()
                target = video_path(state, int(asset_id), digest)
                try:
                    if current and entry.is_file(follow_symlinks=False):
                        try:
                            info = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
                            has_proxy = stat.S_ISREG(info.st_mode)
                        except FileNotFoundError:
                            has_proxy = False
                        if not has_proxy:
                            os.replace(
                                entry.name, target.name, src_dir_fd=parent, dst_dir_fd=parent
                            )
                            continue
                    os.unlink(entry.name, dir_fd=parent)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    LOG.warning("Could not migrate video proxy %s: %s", entry.name, error)
    except (FileNotFoundError, NotADirectoryError):
        pass


def run_stage(worker, stage, asset, pulse):
    jail = MediaJail(worker.library, worker.settings.state_dir)
    config = worker.settings.insights

    def verify_source():
        with jail.open_media(asset["path"]) as (current, _):
            if source_key(os.fstat(current)) != asset["source_key"]:
                raise SourceChanged("Media changed during video analysis; rescan the library")

    with jail.open_media(asset["path"]) as (fd, _):
        if source_key(os.fstat(fd)) != asset["source_key"]:
            raise SourceChanged("Media changed since scan; rescan the library")
        source = f"/proc/self/fd/{fd}"
        duration, audio = probe(source, fd, pulse)
        if stage == "transcript":
            results = transcribe(worker, source, fd, audio, pulse)
            verify_source()
            return results
        results = metrics(source, fd, config, duration, audio, pulse)
        target = video_path(worker.state, asset["id"], asset["content_hash"])
        try:
            if not target.is_symlink() and target.is_file():
                verify_source()
            else:
                with proxy(source, fd, config.threads, target, pulse) as staged:
                    verify_source()
                    staged.replace(staged.with_name(target.name))
        except SourceChanged:
            raise
        except (OSError, ValueError) as error:
            verify_source()
            results["preview_error"] = str(error)
        results["preview"] = not target.is_symlink() and target.is_file()
    prune_artifacts(target.parent, f"{asset['id']}-*.mp4", keep=target.name)
    return results
