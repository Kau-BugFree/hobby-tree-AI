import importlib.util
import json
from pathlib import Path


def module():
    spec = importlib.util.find_spec('scripts.verify_package')
    assert spec is not None, 'The delivery must provide an integrity verifier'
    import scripts.verify_package
    return scripts.verify_package


def test_manifest_detects_changed_and_missing_files(tmp_path):
    mod = module()
    (tmp_path / 'model.bin').write_bytes(b'model')
    (tmp_path / 'config.json').write_text('{}')
    manifest = mod.build_manifest(tmp_path)
    assert mod.verify_manifest(tmp_path, manifest) == []
    (tmp_path / 'model.bin').write_bytes(b'MODEL')
    (tmp_path / 'config.json').unlink()
    problems = mod.verify_manifest(tmp_path, manifest)
    assert any('model.bin' in item for item in problems)
    assert any('config.json' in item for item in problems)


def test_manifest_does_not_accept_paths_outside_package(tmp_path):
    mod = module()
    manifest = {'files': [{'path': '../outside', 'size': 0, 'sha256': '0' * 64}]}
    assert mod.verify_manifest(tmp_path, manifest) == ['unsafe path: ../outside']


def test_manifest_excludes_local_secrets_and_personal_sessions(tmp_path):
    mod = module()
    backend = tmp_path / 'backend'
    (backend / 'var').mkdir(parents=True)
    (backend / '.env').write_text('SECRET=private')
    (backend / '.env.example').write_text('SECRET=')
    (backend / 'var' / 'coach.sqlite').write_bytes(b'private')
    (backend / '.test-temporary').mkdir()
    (backend / '.test-temporary' / 'approved.json').write_text('{}')
    manifest = mod.build_manifest(tmp_path)
    assert [item['path'] for item in manifest['files']] == ['backend/.env.example']


def test_manifest_excludes_env_copies(tmp_path):
    """키를 담은 .env 사본(.env.local, .env.bak)도 매니페스트에 넣지 않습니다."""
    mod = module()
    backend = tmp_path / 'backend'
    backend.mkdir()
    for name in ('.env.local', '.env.bak', '.env.example'):
        (backend / name).write_text('SECRET=')
    manifest = mod.build_manifest(tmp_path)
    assert [item['path'] for item in manifest['files']] == ['backend/.env.example']


def test_unlisted_reports_keys_sessions_and_files_added_after_build(tmp_path):
    """목록 밖 파일을 알려 줍니다. 키·대화 DB가 전달 폴더에 섞여 나가는 것을 막기 위해서입니다."""
    mod = module()
    backend = tmp_path / 'backend'
    (backend / 'var' / 'index').mkdir(parents=True)
    (backend / 'app.py').write_text('x = 1')
    (backend / 'var' / 'index' / 'retrieval.sqlite').write_bytes(b'index')
    manifest = mod.build_manifest(tmp_path)
    assert mod.unlisted(tmp_path, manifest) == []
    (backend / '.env').write_text('SECRET=private')
    (backend / 'var' / 'coach.sqlite').write_bytes(b'private')
    (backend / 'var' / 'test-1' / 'deep').mkdir(parents=True)
    (backend / 'var' / 'test-1' / 'deep' / 'log.txt').write_text('x')
    (backend / 'added.py').write_text('y = 2')
    (backend / '__pycache__').mkdir()
    (backend / '__pycache__' / 'app.pyc').write_bytes(b'')
    # 테스트 임시 폴더는 파일 수천 개를 담으므로 var 바로 아래 항목 하나로 묶는다. 캐시는 보고하지 않는다.
    assert mod.unlisted(tmp_path, manifest) == [
        'backend/.env', 'backend/added.py', 'backend/var/coach.sqlite', 'backend/var/test-1']
