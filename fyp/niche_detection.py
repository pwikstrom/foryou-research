"""Data-driven micro-genre ("niche") detection from video annotation text.

The 22 macro content categories are too coarse to represent the fine-grained
interest niches users actually experience (e.g. "drum solos", "Baldur's Gate
fan content", "Malaysian food memes"). This module discovers those niches
bottom-up by clustering a rich text representation of each video — assembled
from the Gemini annotation fields (``video_story``, ``main_activity``,
``objects``, ``text_overlays``, transcript, hashtags, sound) — so that
niche-level feed dynamics (the "rabbit hole" amplification) can be studied.

First-cut pipeline (no heavy dependencies): TF-IDF → LSA (TruncatedSVD) →
MiniBatchKMeans. Interpretable via per-cluster top terms. Fit on *unique*
videos (by ``item_id``) so repeated impressions don't dominate, then assigned
back to every impression.
"""

import numpy as np
import pandas as pd
from sklearn.cluster import MiniBatchKMeans
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import Normalizer

# Annotation fields combined into each video's document, with integer weights
# (a field's text is repeated ``weight`` times to upweight it for TF-IDF).
# video_story is the richest semantic summary, so it dominates.
TEXT_FIELDS: dict[str, int] = {
    "video_story": 3,
    "main_activity": 2,
    "objects": 1,
    "text_overlays": 1,
    "symbols_and_brands": 1,
    "desc_not_hashtags": 1,
    "transcript_no_repetitions": 1,
    "desc_hashtags": 1,
    "music_title": 1,
}

ITEM_ID_COL = "item_id"
NICHE_COL = "niche"





def _cell_to_text(value) -> str:
    """Flatten a scalar / list / array annotation cell to a plain string."""
    if value is None or value is pd.NA:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, np.ndarray)):
        return " ".join(str(v) for v in value if v is not None)
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)





def assemble_documents(df: pd.DataFrame, fields: dict[str, int] | None = None) -> pd.Series:
    """Build one weighted text document per row from annotation fields.

    Args:
        df: Recoded event/video dataframe.
        fields: Mapping of column name → repeat weight; defaults to
            :data:`TEXT_FIELDS`. Missing columns are skipped.

    Returns:
        A string Series (aligned to ``df.index``), lowercased.
    """
    fields = fields or TEXT_FIELDS
    present = {c: w for c, w in fields.items() if c in df.columns}
    parts = []
    for col, weight in present.items():
        text = df[col].map(_cell_to_text)
        parts.append((text + " ") * weight)
    if not parts:
        return pd.Series([""] * len(df), index=df.index)
    doc = parts[0]
    for p in parts[1:]:
        doc = doc + p
    return doc.str.lower().str.strip()





def fit_niche_model(
    documents: pd.Series,
    n_niches: int = 150,
    svd_dim: int = 100,
    max_features: int = 20000,
    random_state: int = 0,
) -> dict:
    """Fit the TF-IDF → LSA → KMeans niche model on a document corpus.

    Args:
        documents: One text document per (unique) video.
        n_niches: Number of micro-genre clusters (K).
        svd_dim: LSA dimensionality.
        max_features: TF-IDF vocabulary cap.
        random_state: Seed for SVD/KMeans.

    Returns:
        Dict with the fitted ``vectorizer``, ``lsa`` pipeline, ``kmeans``, the
        training ``labels``, and ``params``.
    """
    vectorizer = TfidfVectorizer(
        ngram_range=(1, 2), min_df=5, max_df=0.5,
        max_features=max_features, stop_words="english",
    )
    tfidf = vectorizer.fit_transform(documents)

    svd_dim = min(svd_dim, tfidf.shape[1] - 1) if tfidf.shape[1] > 1 else 1
    lsa = make_pipeline(TruncatedSVD(n_components=svd_dim, random_state=random_state), Normalizer(copy=False))
    reduced = lsa.fit_transform(tfidf)

    n_niches = min(n_niches, len(documents))
    kmeans = MiniBatchKMeans(n_clusters=n_niches, random_state=random_state, n_init=3, batch_size=2048)
    labels = kmeans.fit_predict(reduced)

    return {
        "vectorizer": vectorizer,
        "lsa": lsa,
        "kmeans": kmeans,
        "labels": labels,
        "tfidf": tfidf,
        "params": {"n_niches": n_niches, "svd_dim": svd_dim, "max_features": max_features},
    }





def assign_niches(model: dict, documents: pd.Series) -> np.ndarray:
    """Assign niche labels to new documents using a fitted model."""
    tfidf = model["vectorizer"].transform(documents)
    reduced = model["lsa"].transform(tfidf)
    return model["kmeans"].predict(reduced)





def top_terms_per_niche(model: dict, top_n: int = 8) -> dict[int, list[str]]:
    """Return the most distinctive TF-IDF terms for each niche (centroid terms).

    Args:
        model: Output of :func:`fit_niche_model` (must retain ``tfidf`` and ``labels``).
        top_n: Terms per niche.

    Returns:
        Dict niche_id → list of top terms.
    """
    tfidf = model["tfidf"]
    labels = model["labels"]
    vocab = np.array(model["vectorizer"].get_feature_names_out())
    out: dict[int, list[str]] = {}
    for niche in np.unique(labels):
        rows = np.where(labels == niche)[0]
        if len(rows) == 0:
            out[int(niche)] = []
            continue
        mean_tfidf = np.asarray(tfidf[rows].mean(axis=0)).ravel()
        top_idx = mean_tfidf.argsort()[::-1][:top_n]
        out[int(niche)] = vocab[top_idx].tolist()
    return out





def detect_niches(
    df: pd.DataFrame,
    n_niches: int = 150,
    item_id_col: str = ITEM_ID_COL,
    **fit_kwargs,
) -> tuple[pd.Series, dict, dict[int, list[str]]]:
    """End-to-end: fit niches on unique videos and assign to every row.

    Args:
        df: Recoded event/video dataframe (one row per impression).
        n_niches: Number of micro-genres.
        item_id_col: Column identifying a unique video.
        **fit_kwargs: Passed to :func:`fit_niche_model`.

    Returns:
        ``(niche_labels, model, top_terms)`` where ``niche_labels`` is a Series
        aligned to ``df.index`` (niche id per impression).
    """
    unique = df.drop_duplicates(subset=item_id_col).copy()
    docs_unique = assemble_documents(unique)
    model = fit_niche_model(docs_unique, n_niches=n_niches, **fit_kwargs)
    unique[NICHE_COL] = model["labels"]
    top_terms = top_terms_per_niche(model)

    niche_by_item = unique.set_index(item_id_col)[NICHE_COL]
    niche_labels = df[item_id_col].map(niche_by_item)
    return niche_labels, model, top_terms
