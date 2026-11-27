"""Shared identities and settings. This module has no inference dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from photocull.config import InsightSettings, Settings, _fingerprint

AESTHETIC_PROMPTS = (
    "an aesthetically pleasing photograph with beautiful composition",
    "a polished professional photograph with expressive lighting",
    "a poorly composed photograph with distracting clutter",
    "an unattractive snapshot with harsh lighting",
)


@dataclass(frozen=True)
class ModelIdentity:
    model_id: str
    revision: str
    parameters: tuple = ()

    @property
    def key(self) -> str:
        return _fingerprint((self.model_id, self.revision, self.parameters))


def models(settings: Settings) -> dict[str, ModelIdentity]:
    config = settings.insights
    result = {}
    if settings.ml.enabled and config.aesthetic:
        result["aesthetic"] = ModelIdentity(
            "photocull/siglip-aesthetic-prompt-head",
            "1",
            (settings.ml.model_key, AESTHETIC_PROMPTS),
        )
    if config.faces:
        parameters = (1, config.face_threshold)
        # Default detection is unchanged, and the migration handles default-quality assignments.
        defaults = InsightSettings()
        fields = ("face_detect_score", "face_min_px", "face_min_quality")
        custom = tuple(getattr(config, field) for field in fields)
        if custom != tuple(getattr(defaults, field) for field in fields):
            parameters += custom
        result["faces"] = ModelIdentity(
            "opencv/yunet-2023mar+sface-2021dec",
            config.face_revision,
            parameters,
        )
    if config.video:
        result["video"] = ModelIdentity("photocull/ffmpeg-video", "2")
        if config.transcript:
            result["transcript"] = ModelIdentity(
                config.transcript_model, config.transcript_revision, (1,)
            )
    return result


def taste_input(settings: Settings) -> str:
    aesthetic = models(settings).get("aesthetic")
    return _fingerprint(
        (
            1,
            settings.ml.model_key,
            aesthetic.key if aesthetic else None,
            settings.insights.taste_min_per_class,
        )
    )


def classify_video(metrics: Mapping[str, float], config: InsightSettings) -> tuple[list[str], bool]:
    quiet = not metrics["has_audio"] or metrics["volume"] < config.video_static_volume
    very_short = metrics["duration"] < config.video_very_short_s
    mostly_dark = metrics["dark_fraction"] >= config.video_dark_fraction
    possible_pocket = metrics["pocket_fraction"] >= config.video_pocket_fraction
    low_detail = metrics["blur_fraction"] >= config.video_blur_fraction
    static = metrics["duration"] >= 4 and metrics["motion"] < config.video_static_motion and quiet
    reasons = [
        reason
        for reason, present in (
            ("very_short", very_short),
            ("mostly_dark", mostly_dark),
            ("possible_pocket_recording", possible_pocket),
            ("low_detail_or_blur", low_detail),
            ("static_and_quiet", static),
        )
        if present
    ]
    # Quiet corroborates darkness. A dead-clip flag usually needs two reasons.
    dead = very_short or possible_pocket or sum((mostly_dark and quiet, low_detail, static)) >= 2
    return reasons, dead
