from __future__ import annotations

import hashlib

import numpy as np

from photocull.db import write
from photocull.db.catalog import mark_catalog_updated
from photocull.db.insights import configure_stage, register
from photocull.insights import ModelIdentity, models, taste_input
from photocull.worker.insights.model_cache import cache_bytes
from photocull.worker.ml.vectors import blob_vector

FEATURE_NAMES = (
    "Image embedding",
    "Sharpness",
    "Exposure balance",
    "Highlight detail",
    "Shadow detail",
    "Aesthetic score",
)
LIMIT_PER_CLASS = 4096
FEATURE_SQL = """
    SELECT a.id, a.content_hash, a.rating, a.rejected, e.vector, s.sharpness, s.exposure,
           s.clipped_highlights, s.clipped_shadows, aesthetic.score AS aesthetic
    FROM assets AS a JOIN asset_embeddings AS e ON a.id = e.asset_id
    LEFT JOIN asset_scores AS s ON a.id = s.asset_id
    LEFT JOIN insight_results AS aesthetic ON aesthetic.asset_id = a.id
        AND aesthetic.stage = 'aesthetic' AND aesthetic.key = :aesthetic_key
    WHERE a.active = 1 AND e.model_key = :embedding_key
"""


def features(row) -> np.ndarray:
    scalars = [
        row["sharpness"],
        1 - 2 * abs(row["exposure"] - 0.5) if row["exposure"] is not None else None,
        1 - row["clipped_highlights"] if row["clipped_highlights"] is not None else None,
        1 - row["clipped_shadows"] if row["clipped_shadows"] is not None else None,
        row["aesthetic"],
    ]
    return np.concatenate(
        (blob_vector(row["vector"]), [0 if x is None else x - 0.5 for x in scalars]),
        dtype=np.float32,
    )


def probability(logit):
    return 1 / (1 + np.exp(-np.clip(logit, -40, 40)))


def coefficients(matrix, labels, pulse):
    """L2 logistic regression with equal aggregate weight per observed class."""
    weights = np.zeros(matrix.shape[1], dtype=np.float32)
    intercept = 0.0
    scale = np.where(labels > 0, 0.5 / labels.sum(), 0.5 / (len(labels) - labels.sum()))
    for step in range(120):
        if step % 8 == 0:
            pulse()
        error = (probability(np.einsum("ij,j->i", matrix, weights) + intercept) - labels) * scale
        weights -= 3 * (np.einsum("i,ij->j", error, matrix) + 0.025 * weights)
        intercept -= 3 * error.sum()
    return np.array([intercept, *weights], dtype="<f4")


def train(db, settings, library, pulse) -> bool:
    state = db.execute("SELECT * FROM taste_state").fetchone()
    input_key = taste_input(settings)
    if state["input_key"] == input_key and state["requested"] == state["completed"]:
        return False
    aesthetic = models(settings).get("aesthetic")
    params = {
        "embedding_key": settings.ml.model_key,
        "aesthetic_key": aesthetic.key if aesthetic else None,
    }
    rows = []
    counts = []
    # A read snapshot lets ratings continue while preserving one training generation.
    db.execute("BEGIN")
    try:
        for conditions in (
            "a.rejected = 1 OR a.rating BETWEEN 1 AND 2",
            "a.rejected = 0 AND a.rating >= 4",
        ):
            query = f"{FEATURE_SQL} AND ({conditions}) ORDER BY a.id DESC LIMIT {LIMIT_PER_CLASS}"
            sample = db.execute(query, params).fetchall()
            counts.append(len(sample))
            rows.extend(sample)
    finally:
        db.rollback()
    model_key = None
    fit = None
    if min(counts) >= settings.insights.taste_min_per_class:
        matrix = np.stack([features(row) for row in rows])
        labels = np.repeat([0, 1], counts)
        fit = coefficients(matrix, labels, pulse).tobytes()
    directory = settings.model_cache / "taste" / library
    identity = None
    if fit is not None:
        revision = hashlib.sha256(fit).hexdigest()
        identity = ModelIdentity("photocull/taste-logistic", revision, (input_key,))
        cache_bytes(directory / f"{identity.key}.f32", fit)
    with write(db):
        if identity is not None:
            model_key = register(db, identity)
        configure_stage(db, "taste", model_key)
        db.execute(
            """UPDATE taste_state SET completed = ?, status = ?, positives = ?, negatives = ?,
               model_key = ?, input_key = ?, coefficients = ?, error = NULL""",
            (
                state["requested"],
                "ready" if fit is not None else "insufficient",
                counts[1],
                counts[0],
                model_key,
                input_key,
                fit,
            ),
        )
        mark_catalog_updated(db)
    for old in directory.glob("*.f32"):
        if old.stem != model_key:
            old.unlink(missing_ok=True)
    return True


def predict(db, settings, asset_id: int):
    aesthetic = models(settings).get("aesthetic")
    row = db.execute(
        f"{FEATURE_SQL} AND a.id = :asset_id",
        {
            "embedding_key": settings.ml.model_key,
            "aesthetic_key": aesthetic.key if aesthetic else None,
            "asset_id": asset_id,
        },
    ).fetchone()
    if row is None:
        return None
    weights = np.frombuffer(
        db.execute("SELECT coefficients FROM taste_state").fetchone()[0], dtype="<f4"
    )
    contributions = features(row) * weights[1:]
    score = float(probability(weights[0] + contributions.sum()))
    grouped = [float(contributions[:-5].sum()), *map(float, contributions[-5:])]
    ranked = sorted(zip(FEATURE_NAMES, grouped, strict=True), key=lambda item: -abs(item[1]))
    return score, [{"feature": name, "contribution": value} for name, value in ranked[:3]]
