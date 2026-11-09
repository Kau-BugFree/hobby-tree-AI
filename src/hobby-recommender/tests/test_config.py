"""Reject invalid rule deployments before serving recommendation traffic."""
import json
import shutil
from pathlib import Path

import pytest

from app.config import DataBundle


DATA = Path(__file__).resolve().parents[1] / "app" / "data"


def changed_bundle(tmp_path, filename, mutate):
    """원본 데이터는 유지하고 임시 복사본의 지정 JSON만 변형한다."""
    directory = tmp_path / "data"
    shutil.copytree(DATA, directory)
    target = directory / filename
    payload = json.loads(target.read_text())
    mutate(payload)
    target.write_text(json.dumps(payload, ensure_ascii=False))
    return directory


def test_invalid_total_weight_fails_startup(tmp_path):
    """가중치 합이 100이 아닌 설정을 시작 단계에서 거부하는지 확인한다."""
    directory = changed_bundle(
        tmp_path, "rules.json", lambda rules: rules["criteria"][0].update(weight=41)
    )
    with pytest.raises(ValueError, match="sum to 100"):
        DataBundle(directory)


def test_unknown_scoring_option_fails_startup(tmp_path):
    """설문에 존재하지 않는 선택지를 점수표가 참조하면 거부하는지 확인한다."""
    def mutate(rules):
        """해당 무결성 검사를 재현하기 위해 임시 테스트 데이터에 오류를 삽입한다."""
        criterion = rules["criteria"][0]
        criterion["scores"]["원본 설문에 없는 선택지"] = next(iter(criterion["scores"].values()))

    directory = changed_bundle(tmp_path, "rules.json", mutate)
    with pytest.raises(ValueError, match="Unknown scoring option"):
        DataBundle(directory)


def test_unknown_activity_resource_fails_startup(tmp_path):
    """활동이 정의되지 않은 자원을 요구하면 데이터 로딩을 거부하는지 확인한다."""
    def mutate(catalog):
        """해당 무결성 검사를 재현하기 위해 임시 테스트 데이터에 오류를 삽입한다."""
        catalog["hobbies"][0]["activities"][0]["required_resources"].append("undefined_resource")

    directory = changed_bundle(tmp_path, "catalog.json", mutate)
    with pytest.raises(ValueError, match="Unknown resources"):
        DataBundle(directory)


def test_missing_hobby_score_fails_startup(tmp_path):
    """점수표에서 특정 취미의 적합도가 빠진 설정을 거부하는지 확인한다."""
    def mutate(rules):
        """해당 무결성 검사를 재현하기 위해 임시 테스트 데이터에 오류를 삽입한다."""
        next(iter(rules["criteria"][0]["scores"].values())).pop("reading")

    directory = changed_bundle(tmp_path, "rules.json", mutate)
    with pytest.raises(ValueError, match="Every scored option must define every hobby"):
        DataBundle(directory)
