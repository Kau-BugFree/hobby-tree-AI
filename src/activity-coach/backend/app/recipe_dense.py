"""Offline bounded recipe embedding with immutable, atomically published generations."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
import uuid

import numpy as np

from .retrieval import document_text

LOG = logging.getLogger(__name__)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def model_fingerprint(encoder) -> str:
    """Hash actual local model/tokenizer files and inference configuration once per run."""
    root = Path(encoder.model_path)
    files = sorted(path for path in root.rglob('*') if path.is_file())
    if not files:
        raise ValueError('model contains no files')
    versions = {}
    for package in ('sentence-transformers', 'transformers', 'torch', 'numpy'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    dtype = getattr(getattr(encoder, '_model', None), 'dtype', 'float32')
    config = {'format': 1, 'name': encoder.name, 'max_length': encoder.max_length,
              'dtype': str(dtype),
              'normalize_embeddings': True, 'versions': versions}
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode())
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(bytes.fromhex(file_hash(path)))
    return digest.hexdigest()


def corpus_fingerprint(db: sqlite3.Connection) -> tuple[str, int]:
    digest = hashlib.sha256(b'recipe-corpus-v1')
    count = 0
    for row in db.execute("SELECT rowid,id,payload FROM records WHERE scope='recipes' ORDER BY rowid"):
        digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':')).encode())
        digest.update(b'\n')
        count += 1
    return digest.hexdigest(), count


def atomic_json(path: Path, value: dict | list) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _safe_file(index: Path, relative: str) -> Path:
    path = (index / relative).resolve()
    if not path.is_relative_to(index.resolve()) or not relative.startswith('recipe-generations/'):
        raise ValueError('invalid generation path')
    return path


def load_generation(index: Path, manifest: dict, encoder, *, model_hash: str | None = None):
    if manifest.get('model_fingerprint') != (model_hash or model_fingerprint(encoder)):
        raise ValueError('model fingerprint differs')
    with sqlite3.connect(f'{(index / "retrieval.sqlite").as_uri()}?mode=ro', uri=True) as db:
        fingerprint, count = corpus_fingerprint(db)
        ids = [row[0] for row in db.execute("SELECT id FROM records WHERE scope='recipes' ORDER BY rowid")]
    if fingerprint != manifest['corpus_fingerprint'] or count != manifest['dense_count']:
        raise ValueError('corpus fingerprint/count differs')
    vectors_path = _safe_file(index, manifest['vectors_file'])
    ids_path = _safe_file(index, manifest['ids_file'])
    if file_hash(vectors_path) != manifest['vectors_sha256'] or file_hash(ids_path) != manifest['ids_sha256']:
        raise ValueError('generation checksum differs')
    if json.loads(ids_path.read_text(encoding='utf-8')) != ids:
        raise ValueError('generation ID order differs')
    vectors = np.load(vectors_path, mmap_mode='r', allow_pickle=False)
    if vectors.dtype != np.float32 or vectors.shape != (count, manifest['dimensions']):
        raise ValueError('generation matrix shape/dtype differs')
    for offset in range(0, count, 2048):
        if not np.isfinite(vectors[offset:offset + 2048]).all():
            raise ValueError('nonfinite vectors')
    return vectors, ids


def build_recipe_dense(index: Path, encoder, *, chunk_size: int = 256,
                       expected_count: int = 544888) -> dict:
    if chunk_size < 1 or expected_count < 1:
        raise ValueError('chunk size and expected count must be positive')
    index = Path(index).resolve()
    lock = index / 'recipes.build.lock'
    # OS-held exclusive file creation; abrupt exits leave an explicit recoverable lock.
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(descriptor, json.dumps({'pid': os.getpid(), 'started': time.time()}).encode())
        os.close(descriptor)
        return _build(index, encoder, chunk_size, expected_count)
    finally:
        lock.unlink()


def _build(index: Path, encoder, chunk_size: int, expected_count: int) -> dict:
    started = time.monotonic()
    checkpoint_path = index / 'recipes.build.json'
    model_hash = model_fingerprint(encoder)
    # One read transaction fixes the source snapshot even if WAL writers change the live DB.
    with sqlite3.connect(f'{(index / "retrieval.sqlite").as_uri()}?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        corpus_hash, count = corpus_fingerprint(db)
        if count != expected_count:
            raise ValueError(f'recipe count {count} differs from expected count {expected_count}')
        meta = db.execute("SELECT value FROM metadata WHERE key='index:recipes'").fetchone()
        if not meta:
            raise ValueError('recipe lexical metadata missing')
        if checkpoint_path.exists():
            state = json.loads(checkpoint_path.read_text(encoding='utf-8'))
            if (state['model_fingerprint'], state['corpus_fingerprint'], state['count']) != (model_hash, corpus_hash, count):
                raise ValueError('resume fingerprint differs; archive recipes.build.json to start a new generation')
        else:
            generation = 'recipe-generations/' + uuid.uuid4().hex
            (index / generation).mkdir(parents=True)
            state = {'generation': generation, 'model_fingerprint': model_hash,
                     'corpus_fingerprint': corpus_hash, 'count': count, 'completed': 0, 'last_rowid': 0,
                     'chunks': []}
            atomic_json(checkpoint_path, state)
        vectors_path = _safe_file(index, state['generation'] + '/vectors.npy')
        matrix = None
        if state['completed']:
            matrix = np.load(vectors_path, mmap_mode='r+', allow_pickle=False)
            if matrix.shape != (count, state['dimensions']) or matrix.dtype != np.float32:
                raise ValueError('checkpoint matrix differs')
            prefix_count = db.execute("SELECT count(*) FROM records WHERE scope='recipes' AND rowid<=?", (state['last_rowid'],)).fetchone()[0]
            if prefix_count != state['completed']:
                raise ValueError('checkpoint position differs')
            offset = 0
            for length, checksum in state['chunks']:
                if hashlib.sha256(matrix[offset:offset + length].tobytes()).hexdigest() != checksum:
                    raise ValueError('checkpoint vector checksum differs')
                offset += length
            if offset != state['completed']:
                raise ValueError('checkpoint chunks differ')
        initial_completed = state['completed']
        while True:
            rows = db.execute("SELECT rowid,payload FROM records WHERE scope='recipes' AND rowid>? ORDER BY rowid LIMIT ?",
                              (state['last_rowid'], chunk_size)).fetchall()
            if not rows:
                break
            vectors = np.asarray(encoder.encode_documents([document_text(json.loads(row[1])) for row in rows]), dtype='float32')
            if vectors.ndim != 2 or vectors.shape[0] != len(rows) or not vectors.shape[1] or not np.isfinite(vectors).all():
                raise ValueError('encoder matrix shape/values invalid')
            if matrix is None:
                state['dimensions'] = int(vectors.shape[1])
                matrix = np.lib.format.open_memmap(vectors_path, mode='w+', dtype='float32', shape=(count, vectors.shape[1]))
            if vectors.shape[1] != state['dimensions']:
                raise ValueError('encoder dimensions changed')
            offset = state['completed']
            matrix[offset:offset + len(rows)] = vectors
            matrix.flush()
            with vectors_path.open('r+b') as stream:
                os.fsync(stream.fileno())
            state = {**state, 'completed': offset + len(rows), 'last_rowid': rows[-1][0],
                     'chunks': [*state['chunks'], [len(rows), hashlib.sha256(vectors.tobytes()).hexdigest()]]}
            atomic_json(checkpoint_path, state)
            LOG.info(json.dumps({'event': 'recipe_embedding_progress', 'completed': state['completed'], 'total': count,
                                 'elapsed_seconds': round(time.monotonic() - started, 3),
                                 'rows_per_second': round((state['completed'] - initial_completed) / max(time.monotonic() - started, .001), 3)}))
        if state['completed'] != count:
            raise ValueError('incomplete recipe generation')
        if matrix is not None:
            del matrix
        ids_path = _safe_file(index, state['generation'] + '/ids.json')
        atomic_json(ids_path, [row[0] for row in db.execute("SELECT id FROM records WHERE scope='recipes' ORDER BY rowid")])
        manifest = {**json.loads(meta[0]), **state, 'model': encoder.name, 'dense_count': count,
                    'vectors_file': state['generation'] + '/vectors.npy', 'ids_file': state['generation'] + '/ids.json',
                    'vectors_sha256': file_hash(vectors_path), 'ids_sha256': file_hash(ids_path),
                    'elapsed_seconds_this_run': time.monotonic() - started}
    # A new connection checks the live corpus after releasing the source snapshot.
    load_generation(index, manifest, encoder, model_hash=model_hash)
    atomic_json(index / 'recipes.dense.json', manifest)
    checkpoint_path.unlink()
    return manifest
