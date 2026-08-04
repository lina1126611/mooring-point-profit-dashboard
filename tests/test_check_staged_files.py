"""실데이터 커밋 차단 규칙 테스트.

이 규칙이 느슨해지면 공개 저장소에 실데이터가 올라가고, push 된 뒤에는
되돌릴 수 없다. 그래서 (a) 막아야 할 것을 막는지 (b) **멀쩡한 커밋을
막지 않는지** 둘 다 고정한다.

(b) 가 (a) 만큼 중요하다 — 오탐이 잦으면 사람이 `--no-verify` 를 습관으로
쓰게 되고, 그 순간부터 이 검사는 없는 것과 같아진다.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import pytest  # noqa: E402

from check_staged_files import (  # noqa: E402
    OLE2_MAGIC,
    ZIP_MAGIC,
    inspect,
    spreadsheet_magic,
    unquote_git_path,
)


def reasons(paths, read_head=None) -> dict[str, str]:
    return {h["경로"]: h["이유"] for h in inspect(paths, read_head=read_head)}


# ===============================================================
# 막아야 하는 것
# ===============================================================


@pytest.mark.parametrize("path", [
    "data/raw/매입(세금계산서)_202608031247.xls",
    "data/local/mappings.json",
    "data/local/사용처_검토표.csv",
    "data/pseudo/매출(세금계산서)_202608031241_가명.xlsx",
    "씨웨이테크 진짜 데이터/SWT/가명처리_급여대장_202608031253.xlsx",
    "피드백/전사문.pdf",
])
def test_비밀_디렉터리는_전부_막는다(path):
    """gitignore 가 뚫렸을 때의 2차 방어선."""
    assert path in reasons([path])


@pytest.mark.parametrize("path", [
    "메모/씨웨이테크_데이터요청리스트_회신.docx",   # 엑셀이 아니라 확장자 규칙에 안 걸림
    "정리/Mooring_Point_부분가명_데이터분석.xlsx",
    "temp/가명처리_매출.xlsx",
    "백업/현황정리_2026-08-03.pdf",
])
def test_회사와_주고받은_문서도_파일명으로_잡는다(path):
    """비밀 디렉터리 밖으로 복사해 둔 경우. 확장자가 엑셀이 아닌 것도 있다."""
    assert path in reasons([path])


@pytest.mark.parametrize("path", [
    "분석/매입(세금계산서).xls",          # data/ 밖 — gitignore 가 못 잡는다
    "급여대장_사본.xlsx",
    "temp/4대보험조회.xls",
    "백업/mappings.json",
    "노트/거래처_계정.csv",
])
def test_경로를_벗어나도_파일명으로_잡는다(path):
    """.gitignore 는 경로만 본다. 옮기면 통과되는 구멍을 여기서 막는다."""
    assert path in reasons([path])


@pytest.mark.parametrize("path", [
    "무제.xlsx",
    "정리중/자료.xls",
    "scratch/계산.xlsm",
])
def test_샘플_폴더_밖의_스프레드시트는_막는다(path):
    """실 ERP export 는 .xls 다. 이름을 바꿔도 확장자로 걸린다."""
    assert path in reasons([path])


def test_확장자를_바꿔_숨긴_엑셀도_내용으로_잡는다():
    """.txt 로 바꿔 두면 확장자 규칙을 빠져나간다. 매직바이트로 막는다."""
    def read_head(path):
        return OLE2_MAGIC + b"\x00" * 100

    got = reasons(["메모/자료.txt"], read_head=read_head)
    assert "메모/자료.txt" in got
    assert "내용이 엑셀" in got["메모/자료.txt"]


def test_xlsx를_확장자만_바꾼_경우도_잡는다():
    def read_head(path):
        return ZIP_MAGIC + b"........" + b"xl/workbook.xml" + b"\x00" * 50

    assert "backup/data.bin" in reasons(["backup/data.bin"], read_head=read_head)


# ===============================================================
# 막으면 안 되는 것 — 오탐 방지
# ===============================================================


@pytest.mark.parametrize("path", [
    "src/profit.py",
    "app.py",
    "tests/test_profit.py",
    "CLAUDE.md",
    "db/schema.sql",
    "scripts/load_real_data.py",
    ".gitignore",
    "requirements.txt",
])
def test_평범한_소스는_통과한다(path):
    assert inspect([path]) == []


@pytest.mark.parametrize("path", [
    "data/sample/매입_세금계산서_2026.xlsx",
    "data/sample/매출_세금계산서_2026.xlsx",
    "data/sample/경비지출대장_2026.xlsx",
    "data/sample/설계맨데이_투입내역.xlsx",
])
def test_샘플_가상데이터는_통과한다(path):
    """generate_sample_data.py 가 만든 가짜 데이터다. 저장소에 있어야 한다."""
    assert inspect([path]) == []


def test_샘플_폴더의_파일명은_수령자료_조각이_있어도_통과한다():
    """'매입_세금계산서' 는 마커에 걸리는 이름이지만 data/sample/ 이면 가상값이다."""
    assert inspect(["data/sample/매입_세금계산서_2026.xlsx"]) == []


def test_gitkeep과_gitignore는_비밀_디렉터리에서도_통과한다():
    """빈 자리표시자다. 이걸 막으면 data/raw/ 구조를 커밋할 수 없다."""
    assert inspect(["data/raw/.gitkeep"]) == []
    assert inspect(["data/pseudo/.gitkeep"]) == []


def test_샘플_폴더는_내용검사도_건너뛴다():
    """data/sample/ 의 xlsx 는 zip+xl/ 이라 매직바이트에 걸린다. 통과해야 한다."""
    def read_head(path):
        return ZIP_MAGIC + b"xl/workbook.xml"

    assert inspect(["data/sample/샘플.xlsx"], read_head=read_head) == []


def test_확장자가_이미_엑셀인_샘플밖_파일은_이유가_하나만_붙는다():
    """중복 판정으로 같은 파일이 두 번 나오면 사람이 뭘 고칠지 헷갈린다."""
    def read_head(path):
        return ZIP_MAGIC + b"xl/workbook.xml"

    hits = inspect(["임시/자료.xlsx"], read_head=read_head)
    assert len(hits) == 1


# ===============================================================
# 경계
# ===============================================================


def test_빈_목록은_통과한다():
    assert inspect([]) == []
    assert inspect(["", "  "]) == []


def test_역슬래시_경로도_정규화한다():
    """git 은 슬래시를 쓰지만 Windows 에서 인자로 넘기면 역슬래시가 온다."""
    assert "data/raw/매입(세금계산서).xls" in reasons(
        ["data\\raw\\매입(세금계산서).xls"]
    )


def test_삭제된_파일은_애초에_대상이_아니다():
    """staged_paths 가 --diff-filter=ACMR 로 D 를 빼므로 여기 안 온다.

    지우는 커밋을 막으면 실수로 커밋된 파일을 되돌릴 수 없게 된다.
    """
    from check_staged_files import staged_paths
    import inspect as _pyinspect

    src = _pyinspect.getsource(staged_paths)
    assert "ACMR" in src


def test_git_경로는_반드시_z옵션으로_읽는다():
    """★ 이 훅을 실제로 무력화시켰던 함정을 고정한다.

    core.quotepath 기본값(true) 때문에 git 은 한글 경로를 8진수로 이스케이프해
    돌려준다. 그러면 한글로 된 NAME_MARKERS 가 하나도 안 맞아서, 수령한
    실데이터(전부 한글 파일명)만 정확히 빠져나간다.
    -z 를 빼면 이 검사는 있으나 마나가 되므로 테스트로 못 박는다.
    """
    from check_staged_files import _git_paths
    import inspect as _pyinspect

    assert '"-z"' in _pyinspect.getsource(_git_paths)


def test_이스케이프된_경로도_해독해서_잡는다():
    """이중 방어. -z 를 빼거나 다른 경로로 목록을 받아도 한글이 새지 않아야 한다."""
    escaped = r'"\353\266\204\354\204\235/\352\270\211\354\227\254\353\214\200\354\236\245.xlsx"'
    assert unquote_git_path(escaped) == "분석/급여대장.xlsx"
    assert len(inspect([escaped])) == 1


def test_비밀_디렉터리도_이스케이프_상태에서_잡는다():
    escaped = r'"data/raw/\353\247\244\354\236\205.xls"'
    assert unquote_git_path(escaped) == "data/raw/매입.xls"
    assert len(inspect([escaped])) == 1


@pytest.mark.parametrize("path", [
    "src/profit.py",                 # 따옴표가 없으면 그대로
    '"src/profit.py"',               # 따옴표만 있고 이스케이프는 없는 경우
])
def test_해독이_평범한_경로를_망가뜨리지_않는다(path):
    assert unquote_git_path(path) == "src/profit.py"


def test_해독_실패하면_원본을_유지한다():
    """깨진 입력에 예외로 죽으면 커밋이 아예 불가능해진다."""
    broken = r'"\999\999"'
    assert unquote_git_path(broken) is not None


def test_read_head가_없으면_내용검사를_건너뛴다():
    """--all 이 아닌 경우처럼 파일을 못 읽는 상황에서도 죽지 않아야 한다."""
    assert inspect(["메모/자료.txt"], read_head=None) == []


def test_읽기_실패는_통과로_처리한다():
    """staged 되었지만 디스크에 없는 경우(rename 등)에 예외로 죽으면 안 된다."""
    def read_head(path):
        raise OSError("없는 파일")

    assert inspect(["메모/자료.txt"], read_head=read_head) == []


# ===============================================================
# 매직바이트 판정
# ===============================================================


def test_매직바이트_판정():
    assert spreadsheet_magic(OLE2_MAGIC + b"junk") is not None
    assert spreadsheet_magic(ZIP_MAGIC + b"xl/") is not None
    assert spreadsheet_magic(ZIP_MAGIC + b"word/document.xml") is None  # docx
    assert spreadsheet_magic(b"") is None
    assert spreadsheet_magic(b"import pandas as pd") is None
