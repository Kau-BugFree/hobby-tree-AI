"""Verify delivery files before first use; local sessions and keys are excluded."""
import argparse
import hashlib
import json
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]
MANIFEST = 'package_manifest.json'


def local_only(relative: Path) -> bool:
    """Keys, sessions and run leftovers: never listed, and reported so they are not shipped by accident."""
    parts = relative.parts
    return (relative.name.startswith('.env') and relative.name != '.env.example' or relative.name == '.coverage'
            or parts[:2] == ('backend', 'var') and parts[:3] != ('backend', 'var', 'index'))


def cache(relative: Path) -> bool:
    return (relative.name == MANIFEST or relative.name.endswith(('.pyc', '-wal', '-shm'))
            or any(p in {'__pycache__', '.pytest_cache', '.git'} or p.startswith('.test-') for p in relative.parts))


def included(relative: Path) -> bool:
    return not local_only(relative) and not cache(relative)


def unlisted(root: Path, manifest: dict) -> list[str]:
    """Files the manifest does not cover: keys and sessions that must not be shipped, or files added after it was built."""
    root = root.resolve()
    listed = {item['path'] for item in manifest['files']}
    found = set()
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if not path.is_file() or cache(relative) or relative.as_posix() in listed:
            continue
        # One entry per item directly under backend/var: a test folder holds thousands of files.
        in_var = relative.parts[:2] == ('backend', 'var') and local_only(relative)
        found.add(Path(*relative.parts[:3]).as_posix() if in_var else relative.as_posix())
    return sorted(found)


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def build_manifest(root: Path) -> dict:
    root = root.resolve()
    files = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if path.is_file() and included(relative):
            if not path.resolve().is_relative_to(root):
                raise ValueError(f'External link: {relative}')
            files.append({'path': relative.as_posix(), 'size': path.stat().st_size, 'sha256': digest(path)})
    return {'version': 1, 'algorithm': 'sha256', 'files': files,
            'total_bytes': sum(row['size'] for row in files)}


def verify_manifest(root: Path, manifest: dict, *, quick: bool = False) -> list[str]:
    root = root.resolve()
    problems = []
    seen = set()
    for item in manifest['files']:
        relative = Path(item['path'])
        path = (root / relative).resolve()
        if (relative.is_absolute() or '..' in relative.parts or not path.is_relative_to(root)
                or item['path'] in seen):
            problems.append(f'unsafe path: {item["path"]}')
            continue
        seen.add(item['path'])
        if not path.is_file():
            problems.append(f'missing: {item["path"]}')
        elif path.stat().st_size != item['size'] or not quick and digest(path) != item['sha256']:
            problems.append(f'changed: {item["path"]}')
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true', help='Create manifest for a prepared delivery')
    parser.add_argument('--quick', action='store_true', help='Check sizes only, not content integrity')
    args = parser.parse_args(argv)
    target = PACKAGE / MANIFEST
    if args.build:
        manifest = build_manifest(PACKAGE)
        target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        # After a build only local files remain unlisted. They travel with a copied folder: delete them before delivery.
        print(json.dumps({'files': len(manifest['files']), 'bytes': manifest['total_bytes'],
                          'remove_before_delivery': unlisted(PACKAGE, manifest)}, ensure_ascii=False))
        return 0
    manifest = json.loads(target.read_text(encoding='utf-8'))
    problems = verify_manifest(PACKAGE, manifest, quick=args.quick)
    print(json.dumps({'mode': 'size_only' if args.quick else 'sha256', 'checked': len(manifest['files']),
                      'problems': problems, 'unlisted': unlisted(PACKAGE, manifest)}, ensure_ascii=False))
    return int(bool(problems))


if __name__ == '__main__':
    raise SystemExit(main())
