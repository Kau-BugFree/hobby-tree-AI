from pathlib import Path

from app.config import ROOT, Settings


def test_default_model_paths_are_inside_delivery_folder():
    package = ROOT.parent.resolve()
    settings = Settings()
    assert settings.embedding_model_path.resolve().is_relative_to(package)
    assert settings.reranker_model_path.resolve().is_relative_to(package)
    assert settings.embedding_model_path.name == 'BGE-m3-ko'
    assert settings.reranker_model_path.name == 'KURE-Reranker-nano'


def test_bundled_python_can_find_its_standard_library():
    import sys

    # The shipped runtime is exercised by run_tests.bat; other OS installs are supported separately.
    if 'runtime' in Path(sys.executable).parts:
        assert Path(sys.prefix).resolve().is_relative_to(ROOT.parent.resolve())
        assert (Path(sys.prefix) / 'Lib' / 'site-packages' / 'fastapi').is_dir()


def test_importing_main_does_not_build_the_app(monkeypatch):
    """app.main을 import하는 것만으로 실제 .env·운영 DB·모델을 읽지 않는다. 앱은 create_app()을 부를 때 만든다."""
    import importlib
    import sys

    def fail(cls):
        raise AssertionError('importing app.main must not read the real settings')

    monkeypatch.setattr(Settings, 'from_env', classmethod(fail))
    monkeypatch.delitem(sys.modules, 'app.main', raising=False)
    module = importlib.import_module('app.main')
    assert not hasattr(module, 'app')


def smoke_health(scopes, index_errors):
    return {'version': '3.0.0', 'llm_configured': False,
            'retrieval': {'documents': {'activities': 162, 'knowledge': 486, 'recipes': 544888},
                          'embedding_error': None, 'index_errors': index_errors, 'dense_index_scopes': scopes}}


def test_smoke_accepts_recipe_vectors_loaded_on_gpu():
    from scripts.smoke_package import check_health

    check_health(smoke_health(['activities', 'knowledge', 'recipes'], {}))


def test_smoke_accepts_recipe_bm25_fallback_on_cpu():
    """레시피 벡터는 만든 환경(fp16)에서만 연결된다. CPU에서는 BM25로 내려가도 정상이다."""
    from scripts.smoke_package import check_health

    check_health(smoke_health(['activities', 'knowledge'], {'recipes': 'dense_index_unavailable'}))


def test_smoke_rejects_missing_core_vectors():
    import pytest
    from scripts.smoke_package import check_health

    with pytest.raises(AssertionError):
        check_health(smoke_health(['activities'], {'knowledge': 'dense_index_unavailable'}))
