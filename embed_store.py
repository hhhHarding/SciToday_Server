"""Tenant-local digest embedding index.

This module keeps the embedding feature optional.  Importing it never imports
numpy, torch, or sentence-transformers; those packages are loaded only when a
sync/search call actually needs vectors.

Per tenant files:
  - digest_embeddings.npy
  - digest_embeddings_manifest.json

Optional synonym file:
  - search_synonyms.json

Synonym format:
{
  "canonical term": ["alias one", "alias two"]
}
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MODEL_NAME = "intfloat/multilingual-e5-small"
MODEL_ENV = "RSSAI_EMBED_MODEL"
ENABLED_ENV = "RSSAI_EMBEDDING_ENABLED"
BATCH_SIZE_ENV = "RSSAI_EMBED_BATCH_SIZE"
SYNONYMS_PATH_ENV = "RSSAI_SEARCH_SYNONYMS_PATH"

VECTOR_FILENAME = "digest_embeddings.npy"
MANIFEST_FILENAME = "digest_embeddings_manifest.json"
SYNONYMS_FILENAME = "search_synonyms.json"
MANIFEST_VERSION = 1

_MODEL_LOCK = threading.Lock()
_MODEL_CACHE = {}
_INDEX_LOCK = threading.RLock()
_INDEX_CACHE = {}
_WARNED = set()
_FAILURE_BACKOFF_SECONDS = 300
_FAILURES = {}


@dataclass(frozen=True)
class _StorePaths:
    tenant_dir: Path
    vector_path: Path
    manifest_path: Path
    synonyms_path: Path


@dataclass
class _LoadedIndex:
    tenant_id: str
    model_name: str
    vector_path: Path
    manifest_path: Path
    vector_mtime: float
    manifest_mtime: float
    filenames: list[str]
    matrix: object


def model_name() -> str:
    return (os.environ.get(MODEL_ENV) or DEFAULT_MODEL_NAME).strip() or DEFAULT_MODEL_NAME


def store_paths(tenant_dir: str | os.PathLike[str]) -> _StorePaths:
    root = Path(tenant_dir)
    synonym_override = (os.environ.get(SYNONYMS_PATH_ENV) or "").strip()
    synonyms_path = Path(synonym_override).expanduser() if synonym_override else root / SYNONYMS_FILENAME
    return _StorePaths(
        tenant_dir=root,
        vector_path=root / VECTOR_FILENAME,
        manifest_path=root / MANIFEST_FILENAME,
        synonyms_path=synonyms_path,
    )


def _log_warning_once(key: str, logger: logging.Logger | None, message: str, *args) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    (logger or logging.getLogger(__name__)).warning(message, *args)


def _env_enabled() -> bool:
    value = (os.environ.get(ENABLED_ENV) or "auto").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def _optional_dependencies_available(logger: logging.Logger | None = None) -> bool:
    if not _env_enabled():
        return False
    missing = [
        name
        for name in ("numpy", "sentence_transformers")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        _log_warning_once(
            "missing-deps",
            logger,
            "Embedding search disabled; missing optional dependencies: %s",
            ", ".join(missing),
        )
        return False
    return True


def _failure_active(key: str) -> bool:
    failed_at = _FAILURES.get(key)
    return bool(failed_at and time.time() - failed_at < _FAILURE_BACKOFF_SECONDS)


def _remember_failure(key: str) -> None:
    _FAILURES[key] = time.time()


def _clear_failure(key: str) -> None:
    _FAILURES.pop(key, None)


def _read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _atomic_write_npy(path: Path, matrix) -> None:
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("wb") as fh:
        np.save(fh, np.asarray(matrix, dtype=np.float32))
    os.replace(tmp, path)


def _normalise_rows(rows) -> list[dict]:
    result = []
    seen = set()
    for row in rows or []:
        if isinstance(row, dict):
            item = row
        else:
            item = {
                "filename": row[0] if len(row) > 0 else "",
                "title": row[1] if len(row) > 1 else "",
                "cn_title": row[2] if len(row) > 2 else "",
                "keywords": row[3] if len(row) > 3 else "",
                "journal": row[4] if len(row) > 4 else "",
                "preview": row[5] if len(row) > 5 else "",
            }
        filename = str(item.get("filename") or "").strip()
        if not filename or filename in seen:
            continue
        seen.add(filename)
        result.append({
            "filename": filename,
            "title": str(item.get("title") or ""),
            "cn_title": str(item.get("cn_title") or ""),
            "keywords": str(item.get("keywords") or ""),
            "journal": str(item.get("journal") or ""),
            "preview": str(item.get("preview") or ""),
        })
    result.sort(key=lambda value: value["filename"])
    return result


def digest_text(row: dict) -> str:
    parts = (
        row.get("title") or "",
        row.get("cn_title") or "",
        row.get("keywords") or "",
        row.get("journal") or "",
        row.get("preview") or "",
    )
    return "\n".join(str(part).strip() for part in parts if str(part or "").strip())


def digest_text_hash(row: dict) -> str:
    return hashlib.sha256(digest_text(row).encode("utf-8")).hexdigest()


def _load_model(logger: logging.Logger | None = None):
    name = model_name()
    if _failure_active(f"model:{name}"):
        return None
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(name)
        if cached is not None:
            return cached
        if not _optional_dependencies_available(logger):
            return None
        try:
            from sentence_transformers import SentenceTransformer

            loaded = SentenceTransformer(name, device="cpu")
        except Exception as exc:  # pragma: no cover - depends on deployment env.
            _remember_failure(f"model:{name}")
            _log_warning_once(
                f"model-load:{name}",
                logger,
                "Embedding model unavailable; semantic recall disabled: %s",
                exc,
            )
            return None
        _MODEL_CACHE[name] = loaded
        _clear_failure(f"model:{name}")
        return loaded


def _encode(texts: list[str], *, query: bool, logger: logging.Logger | None = None):
    import numpy as np

    model = _load_model(logger)
    if model is None:
        return None
    prefix = "query: " if query else "passage: "
    batch_size = max(1, int(os.environ.get(BATCH_SIZE_ENV) or 32))
    values = [prefix + (text or "") for text in texts]
    vectors = model.encode(
        values,
        batch_size=batch_size,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32, copy=False)


def _sentinel_payload(inbox_sentinel) -> dict:
    if not inbox_sentinel:
        return {}
    try:
        return {"count": int(inbox_sentinel[0]), "max_mtime": float(inbox_sentinel[1])}
    except (TypeError, ValueError, IndexError):
        return {}


def needs_sync(
    tenant_id: str,
    tenant_dir: str | os.PathLike[str],
    *,
    inbox_sentinel=None,
    logger: logging.Logger | None = None,
) -> bool:
    if not _optional_dependencies_available(logger):
        return False
    paths = store_paths(tenant_dir)
    if not paths.vector_path.exists() or not paths.manifest_path.exists():
        return True
    manifest = _read_json(paths.manifest_path)
    if manifest.get("version") != MANIFEST_VERSION:
        return True
    if manifest.get("model_name") != model_name():
        return True
    if not isinstance(manifest.get("filenames"), list):
        return True
    try:
        int(manifest.get("dim") or 0)
    except (TypeError, ValueError):
        return True
    sentinel = _sentinel_payload(inbox_sentinel)
    if sentinel and manifest.get("inbox_sentinel") != sentinel:
        return True
    return False


def sync_from_digest_rows(
    tenant_id: str,
    tenant_dir: str | os.PathLike[str],
    rows,
    *,
    inbox_sentinel=None,
    logger: logging.Logger | None = None,
) -> bool:
    """Synchronize one tenant's embedding files from digest rows.

    Returns True when the vector files are current or were written.  Returns
    False when optional dependencies/model are unavailable and lexical search
    should continue alone.
    """

    if not _optional_dependencies_available(logger):
        return False

    import numpy as np

    paths = store_paths(tenant_dir)
    rows = _normalise_rows(rows)
    target_model = model_name()
    filenames = [row["filename"] for row in rows]
    text_hashes = {row["filename"]: digest_text_hash(row) for row in rows}
    manifest = _read_json(paths.manifest_path)
    compatible_manifest = (
        manifest.get("version") == MANIFEST_VERSION
        and manifest.get("model_name") == target_model
        and isinstance(manifest.get("filenames"), list)
        and isinstance(manifest.get("text_hashes"), dict)
    )

    existing_matrix = None
    existing_by_filename = {}
    full_rebuild = not compatible_manifest
    if compatible_manifest and paths.vector_path.exists():
        try:
            existing_matrix = np.load(paths.vector_path, mmap_mode=None)
            old_filenames = [str(name) for name in manifest.get("filenames") or []]
            try:
                manifest_dim = int(manifest.get("dim") or 0)
            except (TypeError, ValueError):
                manifest_dim = -1
            if (
                existing_matrix.ndim != 2
                or existing_matrix.shape[0] != len(old_filenames)
                or manifest_dim < 0
                or (manifest_dim > 0 and existing_matrix.shape[1] != manifest_dim)
            ):
                full_rebuild = True
            else:
                old_hashes = manifest.get("text_hashes") or {}
                for idx, filename in enumerate(old_filenames):
                    if old_hashes.get(filename) == text_hashes.get(filename):
                        existing_by_filename[filename] = existing_matrix[idx]
        except Exception as exc:
            full_rebuild = True
            _log_warning_once(
                f"index-load:{tenant_id}",
                logger,
                "Embedding index will be rebuilt because it could not be loaded: %s",
                exc,
            )
    else:
        full_rebuild = True

    if full_rebuild:
        existing_by_filename = {}

    changed_rows = [
        row for row in rows
        if row["filename"] not in existing_by_filename
    ]

    encoded_by_filename = {}
    if changed_rows:
        vectors = _encode([digest_text(row) for row in changed_rows], query=False, logger=logger)
        if vectors is None:
            return False
        for row, vector in zip(changed_rows, vectors):
            encoded_by_filename[row["filename"]] = vector

    dim = 0
    if encoded_by_filename:
        dim = int(next(iter(encoded_by_filename.values())).shape[0])
    elif existing_by_filename:
        dim = int(next(iter(existing_by_filename.values())).shape[0])
    elif compatible_manifest:
        dim = int(manifest.get("dim") or 0)

    if filenames and dim <= 0:
        return False

    matrix_rows = []
    for filename in filenames:
        vector = existing_by_filename.get(filename)
        if vector is None:
            vector = encoded_by_filename.get(filename)
        if vector is not None:
            matrix_rows.append(vector)

    if len(matrix_rows) != len(filenames):
        return False

    matrix = (
        np.vstack(matrix_rows).astype(np.float32, copy=False)
        if matrix_rows else np.zeros((0, max(0, dim)), dtype=np.float32)
    )
    if matrix.size:
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix = (matrix / norms).astype(np.float32, copy=False)

    now = int(time.time())
    next_manifest = {
        "version": MANIFEST_VERSION,
        "model_name": target_model,
        "dim": int(matrix.shape[1]) if matrix.ndim == 2 else 0,
        "filenames": filenames,
        "filename_to_row": {filename: index for index, filename in enumerate(filenames)},
        "text_hashes": text_hashes,
        "updated_at": now,
        "inbox_sentinel": _sentinel_payload(inbox_sentinel),
    }

    _atomic_write_npy(paths.vector_path, matrix)
    _atomic_write_json(paths.manifest_path, next_manifest)
    _put_loaded_index(tenant_id, target_model, paths, matrix, filenames)
    return True


def _put_loaded_index(
    tenant_id: str,
    target_model: str,
    paths: _StorePaths,
    matrix,
    filenames: list[str],
) -> None:
    try:
        vector_mtime = paths.vector_path.stat().st_mtime
        manifest_mtime = paths.manifest_path.stat().st_mtime
    except OSError:
        return
    key = _index_cache_key(tenant_id, paths)
    with _INDEX_LOCK:
        _INDEX_CACHE[key] = _LoadedIndex(
            tenant_id=tenant_id,
            model_name=target_model,
            vector_path=paths.vector_path,
            manifest_path=paths.manifest_path,
            vector_mtime=vector_mtime,
            manifest_mtime=manifest_mtime,
            filenames=list(filenames),
            matrix=matrix,
        )


def _index_cache_key(tenant_id: str, paths: _StorePaths) -> tuple[str, str, str]:
    return (str(tenant_id), str(paths.vector_path), str(paths.manifest_path))


def _load_index(
    tenant_id: str,
    tenant_dir: str | os.PathLike[str],
    *,
    logger: logging.Logger | None = None,
):
    if not _optional_dependencies_available(logger):
        return None
    import numpy as np

    paths = store_paths(tenant_dir)
    try:
        vector_mtime = paths.vector_path.stat().st_mtime
        manifest_mtime = paths.manifest_path.stat().st_mtime
    except OSError:
        return None

    key = _index_cache_key(tenant_id, paths)
    target_model = model_name()
    with _INDEX_LOCK:
        cached = _INDEX_CACHE.get(key)
        if (
            cached is not None
            and cached.model_name == target_model
            and cached.vector_mtime == vector_mtime
            and cached.manifest_mtime == manifest_mtime
        ):
            return cached

    manifest = _read_json(paths.manifest_path)
    if manifest.get("model_name") != target_model:
        return None
    filenames = [str(name) for name in manifest.get("filenames") or []]
    try:
        matrix = np.load(paths.vector_path, mmap_mode=None)
    except Exception as exc:
        _log_warning_once(
            f"index-search-load:{tenant_id}",
            logger,
            "Embedding search disabled because index could not be loaded: %s",
            exc,
        )
        return None
    if matrix.ndim != 2 or matrix.shape[0] != len(filenames):
        _log_warning_once(
            f"index-shape:{tenant_id}",
            logger,
            "Embedding search disabled because index shape does not match manifest",
        )
        return None
    loaded = _LoadedIndex(
        tenant_id=tenant_id,
        model_name=target_model,
        vector_path=paths.vector_path,
        manifest_path=paths.manifest_path,
        vector_mtime=vector_mtime,
        manifest_mtime=manifest_mtime,
        filenames=filenames,
        matrix=matrix.astype(np.float32, copy=False),
    )
    with _INDEX_LOCK:
        _INDEX_CACHE[key] = loaded
    return loaded


def _load_synonyms(path: Path) -> dict[str, list[str]]:
    payload = _read_json(path)
    result = {}
    for canonical, aliases in payload.items():
        canonical_text = str(canonical or "").strip()
        if not canonical_text:
            continue
        if isinstance(aliases, str):
            values = [aliases]
        elif isinstance(aliases, list):
            values = aliases
        else:
            continue
        clean = []
        for alias in values:
            alias_text = str(alias or "").strip()
            if alias_text and alias_text not in clean:
                clean.append(alias_text)
        result[canonical_text] = clean
    return result


def expand_query(query: str, tenant_dir: str | os.PathLike[str]) -> str:
    base = str(query or "").strip()
    if not base:
        return ""
    paths = store_paths(tenant_dir)
    synonyms = _load_synonyms(paths.synonyms_path)
    if not synonyms:
        return base

    normalized = base.casefold()
    additions = []
    seen = {normalized}
    for canonical, aliases in synonyms.items():
        candidates = [canonical, *aliases]
        if not any(candidate.casefold() in normalized for candidate in candidates):
            continue
        for candidate in candidates:
            key = candidate.casefold()
            if key not in seen:
                seen.add(key)
                additions.append(candidate)
    return " ".join([base, *additions])


def search(
    query: str,
    tenant_id: str,
    tenant_dir: str | os.PathLike[str],
    *,
    limit: int = 100,
    logger: logging.Logger | None = None,
) -> list[str]:
    if not str(query or "").strip():
        return []
    index = _load_index(tenant_id, tenant_dir, logger=logger)
    if index is None or not index.filenames:
        return []
    if _failure_active(f"query:{model_name()}"):
        return []

    import numpy as np

    expanded = expand_query(query, tenant_dir)
    vector = _encode([expanded], query=True, logger=logger)
    if vector is None:
        _remember_failure(f"query:{model_name()}")
        return []
    _clear_failure(f"query:{model_name()}")

    matrix = index.matrix
    if matrix.ndim != 2 or matrix.shape[1] != vector.shape[1]:
        _log_warning_once(
            f"index-dim:{tenant_id}",
            logger,
            "Embedding search disabled because query/index dimensions differ",
        )
        return []

    scores = matrix @ vector[0]
    if scores.size == 0:
        return []
    count = max(1, min(int(limit or 100), len(index.filenames)))
    if count < len(index.filenames):
        top_idx = np.argpartition(scores, -count)[-count:]
        top_idx = top_idx[np.argsort(scores[top_idx])[::-1]]
    else:
        top_idx = np.argsort(scores)[::-1]
    return [index.filenames[int(idx)] for idx in top_idx]
