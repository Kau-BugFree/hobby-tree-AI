import json
import sqlite3

import numpy as np
import pytest

from app.recipe_dense import build_recipe_dense, model_fingerprint, load_generation
from app.retrieval import build_lexical_index


class Encoder:
    name = 'fake'
    max_length = 1024
    batch_size = 2
    device = 'cpu'

    def __init__(self, path, fail=0):
        self.model_path = path
        self.calls = 0
        self.fail = fail

    def encode_documents(self, texts):
        self.calls += 1
        if self.calls == self.fail:
            raise RuntimeError('interrupted')
        return np.array([[len(text), 1] for text in texts], dtype='float32')


@pytest.fixture
def setup(tmp_path):
    model = tmp_path / 'model'
    model.mkdir()
    (model / 'weights').write_bytes(b'weights')
    source = tmp_path / 'recipes.jsonl'
    source.write_text('\n'.join(json.dumps({'activity_id': str(i), 'title': 'soup ' + str(i),
                                          'steps': ['cook']}) for i in range(5)))
    index = tmp_path / 'index'
    build_lexical_index([source], index, 'recipes')
    return index, model


def test_resume_order_and_no_partial_publication(setup):
    index, model = setup
    with pytest.raises(RuntimeError, match='interrupted'):
        build_recipe_dense(index, Encoder(model, fail=2), chunk_size=2, expected_count=5)
    assert not (index / 'recipes.dense.json').exists()
    encoder = Encoder(model)
    manifest = build_recipe_dense(index, encoder, chunk_size=2, expected_count=5)
    assert encoder.calls == 2
    vectors, ids = load_generation(index, manifest, encoder)
    assert ids == ['0', '1', '2', '3', '4']
    assert vectors.shape == (5, 2)


def test_changed_model_or_corpus_rejects_resume(setup):
    index, model = setup
    with pytest.raises(RuntimeError):
        build_recipe_dense(index, Encoder(model, fail=2), chunk_size=2, expected_count=5)
    (model / 'weights').write_bytes(b'changed')
    with pytest.raises(ValueError, match='fingerprint'):
        build_recipe_dense(index, Encoder(model), chunk_size=2, expected_count=5)


def test_lock_and_count_validation(setup):
    index, model = setup
    with pytest.raises(ValueError, match='count'):
        build_recipe_dense(index, Encoder(model), expected_count=6)
    (index / 'recipes.build.lock').write_text('busy')
    with pytest.raises(FileExistsError):
        build_recipe_dense(index, Encoder(model), expected_count=5)


def test_corrupt_generation_and_model_config_rejected(setup):
    index, model = setup
    encoder = Encoder(model)
    manifest = build_recipe_dense(index, encoder, expected_count=5)
    encoder.max_length = 12
    with pytest.raises(ValueError, match='model'):
        load_generation(index, manifest, encoder)
    encoder.max_length = 1024
    (index / manifest['vectors_file']).write_bytes(b'bad')
    with pytest.raises(ValueError):
        load_generation(index, manifest, encoder)


def test_corpus_change_rejects_resume_and_loading(setup):
    index, model = setup
    encoder = Encoder(model)
    manifest = build_recipe_dense(index, encoder, expected_count=5)
    with sqlite3.connect(index / 'retrieval.sqlite') as db:
        db.execute("UPDATE records SET payload=replace(payload,'soup','stew') WHERE scope='recipes'")
    with pytest.raises(ValueError, match='corpus'):
        load_generation(index, manifest, encoder)


def test_publication_failure_preserves_previous_generation(setup, monkeypatch):
    import app.recipe_dense as module
    index, model = setup
    first = build_recipe_dense(index, Encoder(model), expected_count=5)
    original = module.atomic_json

    def fail_publication(path, value):
        if path.name == 'recipes.dense.json':
            raise OSError('publication interrupted')
        original(path, value)

    monkeypatch.setattr(module, 'atomic_json', fail_publication)
    with pytest.raises(OSError, match='publication'):
        build_recipe_dense(index, Encoder(model), expected_count=5)
    assert json.loads((index / 'recipes.dense.json').read_text()) == first
    monkeypatch.setattr(module, 'atomic_json', original)
    encoder = Encoder(model)
    second = build_recipe_dense(index, encoder, expected_count=5)
    assert encoder.calls == 0
    assert second['generation'] != first['generation']
    assert (index / first['vectors_file']).exists()


@pytest.mark.parametrize('bad', [np.zeros((1, 2)), np.array([[np.nan, 1]] * 5), np.zeros((5, 0))])
def test_invalid_encoder_output_not_published(setup, bad):
    index, model = setup
    encoder = Encoder(model)
    encoder.encode_documents = lambda texts: bad
    with pytest.raises(ValueError, match='matrix'):
        build_recipe_dense(index, encoder, expected_count=5)
    assert not (index / 'recipes.dense.json').exists()


def test_loader_uses_immutable_ids_without_dense_positions(setup):
    from app.retrieval import Retrieval
    from types import SimpleNamespace
    index, model = setup
    encoder = Encoder(model)
    build_recipe_dense(index, encoder, expected_count=5)
    retrieval = Retrieval.__new__(Retrieval)
    retrieval.index_dir = index
    retrieval.database = index / 'retrieval.sqlite'
    retrieval._encoder = encoder
    retrieval._dense = {}
    retrieval._dense_rowids = {}
    retrieval._index_errors = {}
    retrieval._load_dense()
    assert retrieval._dense_rowids['recipes'].tolist() == [1, 2, 3, 4, 5]
    assert 'recipes' in retrieval._dense
    with sqlite3.connect(retrieval.database) as db:
        assert db.execute('SELECT count(*) FROM records WHERE dense_pos IS NOT NULL').fetchone()[0] == 0


def test_device_placement_and_batch_size_do_not_invalidate_same_dtype(setup):
    from types import SimpleNamespace
    index, model = setup
    encoder = Encoder(model)
    encoder._model = SimpleNamespace(dtype='float16')
    encoder.device = 'cuda'
    original = model_fingerprint(encoder)
    encoder.device = 'auto'
    encoder.batch_size = 64
    assert model_fingerprint(encoder) == original
