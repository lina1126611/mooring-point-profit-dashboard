"""실데이터 경로 해석 테스트.

경로가 조용히 틀리면 두 방향으로 위험하다.
  - 저장소 안을 가리키는데 밖이라고 믿으면 → 도구에 실데이터가 노출된다
  - 옛 경로를 읽으면 → "고쳤는데 왜 안 바뀌지" 로 시간을 버린다
그래서 해석 우선순위와 경고 조건을 고정한다.
"""

from __future__ import annotations

import json

import pytest

from src import paths


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """환경변수와 해석 이력을 매 테스트마다 비운다."""
    for key in ("MOORING_RAW_DIR", "MOORING_LOCAL_DIR", "MOORING_PSEUDO_DIR"):
        monkeypatch.delenv(key, raising=False)
    paths._ORIGIN.clear()
    yield


# ===============================================================
# 해석 우선순위
# ===============================================================


def test_환경변수가_최우선(monkeypatch, tmp_path):
    monkeypatch.setenv("MOORING_RAW_DIR", str(tmp_path / "env-raw"))
    got = paths.resolve("raw", config={"raw": str(tmp_path / "cfg-raw")})
    assert got == (tmp_path / "env-raw").resolve()
    assert "MOORING_RAW_DIR" in paths.origin("raw")


def test_환경변수가_없으면_설정파일(tmp_path):
    got = paths.resolve("raw", config={"raw": str(tmp_path / "cfg-raw")})
    assert got == (tmp_path / "cfg-raw").resolve()
    assert "raw" in paths.origin("raw")


def test_둘_다_없으면_저장소_내_기본경로(tmp_path):
    got = paths.resolve("raw", config={})
    assert got == paths.PROJECT_ROOT / "data" / "raw"
    assert "권장하지 않음" in paths.origin("raw")


def test_빈_문자열은_설정되지_않은_것으로_본다(monkeypatch):
    """환경변수를 빈 값으로 두고 "설정했다"고 착각하는 경우를 막는다."""
    monkeypatch.setenv("MOORING_RAW_DIR", "   ")
    assert paths.resolve("raw", config={}) == paths.PROJECT_ROOT / "data" / "raw"


def test_세_종류_모두_해석된다(tmp_path):
    cfg = {
        "raw": str(tmp_path / "r"),
        "local": str(tmp_path / "l"),
        "pseudo": str(tmp_path / "p"),
    }
    assert paths.resolve("raw", cfg) == (tmp_path / "r").resolve()
    assert paths.resolve("local", cfg) == (tmp_path / "l").resolve()
    assert paths.resolve("pseudo", cfg) == (tmp_path / "p").resolve()


def test_모르는_종류는_거부한다():
    with pytest.raises(ValueError, match="알 수 없는 경로 종류"):
        paths.resolve("secret")


def test_물결표를_홈으로_확장한다(tmp_path):
    got = paths.resolve("raw", config={"raw": "~/mooring-private/raw"})
    assert "~" not in str(got)
    assert got.is_absolute()


# ===============================================================
# 저장소 안/밖 판정 — 이게 틀리면 경고가 안 뜬다
# ===============================================================


def test_저장소_안을_안이라고_판정한다():
    assert paths.is_inside_repo(paths.PROJECT_ROOT / "data" / "raw")
    assert paths.is_inside_repo(paths.PROJECT_ROOT)


def test_저장소_밖을_밖이라고_판정한다(tmp_path):
    assert not paths.is_inside_repo(tmp_path)


def test_저장소_안이면_경고한다():
    msg = paths.warn_if_inside_repo("raw", paths.PROJECT_ROOT / "data" / "raw")
    assert msg is not None
    assert "커밋만 막습니다" in msg
    assert "MOORING_RAW_DIR" in msg     # 어떻게 고치는지도 알려줘야 한다


def test_저장소_밖이면_경고하지_않는다(tmp_path):
    assert paths.warn_if_inside_repo("raw", tmp_path) is None


# ===============================================================
# 설정 파일 읽기 — 깨져도 죽지 않아야 한다
# ===============================================================


def test_설정파일이_깨져_있으면_기본경로로_떨어진다(monkeypatch, tmp_path):
    """JSON 오타 하나로 전 스크립트가 죽으면 안 된다."""
    broken = tmp_path / "paths.local.json"
    broken.write_text("{ 이건 JSON 이 아니다", encoding="utf-8")
    monkeypatch.setattr(paths, "CONFIG_PATH", broken)
    assert paths.resolve("raw") == paths.PROJECT_ROOT / "data" / "raw"


def test_설정파일이_객체가_아니면_무시한다(monkeypatch, tmp_path):
    odd = tmp_path / "paths.local.json"
    odd.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(paths, "CONFIG_PATH", odd)
    assert paths.resolve("raw") == paths.PROJECT_ROOT / "data" / "raw"


def test_설정파일이_없으면_기본경로(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "CONFIG_PATH", tmp_path / "없는파일.json")
    assert paths.resolve("raw") == paths.PROJECT_ROOT / "data" / "raw"


def test_설정파일을_읽는다(monkeypatch, tmp_path):
    cfg = tmp_path / "paths.local.json"
    cfg.write_text(
        json.dumps({"raw": str(tmp_path / "밖에있는raw")}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "CONFIG_PATH", cfg)
    assert paths.resolve("raw") == (tmp_path / "밖에있는raw").resolve()


# ===============================================================
# describe — 사람이 출처를 확인하는 경로
# ===============================================================


def test_describe가_경로와_출처를_함께_보여준다(monkeypatch, tmp_path):
    monkeypatch.setenv("MOORING_RAW_DIR", str(tmp_path / "r"))
    text = paths.describe(("raw",))
    assert str((tmp_path / "r").resolve()) in text
    assert "밖" in text
    assert "MOORING_RAW_DIR" in text


def test_describe가_저장소_내_경로를_표시한다(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "CONFIG_PATH", tmp_path / "없음.json")
    text = paths.describe(("raw",))
    assert "★저장소 내" in text


# ===============================================================
# 편의 접근자가 매번 다시 해석하는지
# ===============================================================


def test_접근자는_호출마다_다시_해석한다(monkeypatch, tmp_path):
    """import 시점에 굳으면 설정을 고쳐도 옛 경로가 남는다."""
    monkeypatch.setenv("MOORING_RAW_DIR", str(tmp_path / "first"))
    assert paths.raw_dir() == (tmp_path / "first").resolve()

    monkeypatch.setenv("MOORING_RAW_DIR", str(tmp_path / "second"))
    assert paths.raw_dir() == (tmp_path / "second").resolve()
