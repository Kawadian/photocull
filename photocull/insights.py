"""Shared identities and settings. This module has no inference dependencies."""

from __future__ import annotations

from dataclasses import dataclass

from photocull.config import Settings, _fingerprint

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
        result["faces"] = ModelIdentity(
            "opencv/yunet-2023mar+sface-2021dec",
            config.face_revision,
            (1, config.face_threshold),
        )
    if config.video:
        result["video"] = ModelIdentity("photocull/ffmpeg-video", "1")
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
