"""커밋 직전에 실데이터가 섞여 들어갔는지 검사한다 (pre-commit 훅 본체).

    python scripts/check_staged_files.py          # staged 파일 검사 (훅이 호출)
    python scripts/check_staged_files.py --all    # 추적 중인 전 파일 검사

이 저장소는 **공개**다. 한 번 push 되면 커밋을 지워도 GitHub 캐시와 포크에
남아 되돌릴 수 없다. 그래서 push 가 아니라 commit 단계에서 막는다.

.gitignore 와 역할이 다르다:
    .gitignore  지정된 **경로**만 막는다. data/ 밖에 두면 안 걸린다.
    이 검사      경로·파일명·**내용(매직바이트)** 으로 잡는다. 이름을 바꿔도 걸린다.
둘은 서로를 보완하므로 둘 다 있어야 한다.

오탐(false positive)을 최소로 두는 것이 설계 목표다. 멀쩡한 커밋이 자주
막히면 사람이 `--no-verify` 를 습관으로 쓰게 되고, 그때부터 이 검사는
없는 것과 같아진다. 그래서 규칙은 **좁고 확실한 것만** 넣는다.
"""

from __future__ import annotations

import subprocess
import sys
import warnings
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------
# 규칙
# ---------------------------------------------------------------

# 여기 안에 있는 것만 스프레드시트 커밋을 허용한다.
# data/sample/ 은 generate_sample_data.py 가 만든 가상 데이터다.
ALLOWED_DIRS: tuple[str, ...] = ("data/sample/",)

# 선언상 외부로 나가면 안 되는 디렉터리. .gitignore 가 뚫렸을 때의 2차 방어.
SECRET_DIRS: tuple[str, ...] = ("data/raw/", "data/local/", "data/pseudo/")

# 수령한 ERP export 와 그 파생물의 파일명 조각. 경로와 무관하게 잡는다.
NAME_MARKERS: tuple[str, ...] = (
    "매입(", "매출(", "급여대장", "4대보험", "간이영수증", "세금계산서",
    "국세_지방세", "mappings.json", "검토표", "거래처_계정",
)

SPREADSHEET_EXT: tuple[str, ...] = (".xls", ".xlsx", ".xlsm", ".xlsb")

# 매직바이트 — 확장자를 바꿔서 숨긴 엑셀을 잡는다.
OLE2_MAGIC = b"\xd0\xcf\x11\xe0"      # .xls (구형 OLE2 복합문서)
ZIP_MAGIC = b"PK\x03\x04"             # .xlsx (zip 컨테이너)
XLSX_HINT = b"xl/"                    # zip 안에 'xl/' 이 있으면 엑셀 통합문서

MAGIC_SCAN_BYTES = 4096

# .gitkeep 처럼 내용이 없는 자리표시자는 검사 대상이 아니다
EXEMPT_NAMES: frozenset[str] = frozenset({".gitkeep", ".gitignore"})


def unquote_git_path(path: str) -> str:
    """git 이 C 스타일로 이스케이프한 경로를 원래 문자열로 되돌린다.

    이중 방어다. _git_paths 가 -z 를 쓰므로 정상 경로에는 이스케이프가 없지만,
    누가 -z 를 빼거나 다른 경로로 목록을 받아 오면 한글 파일명이 전부
    빠져나간다. 그 실패가 조용하기 때문에 여기서 한 번 더 받아낸다.

        "\\353\\266\\204\\354\\204\\235/..." → 분석/...
    """
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        return path
    inner = path[1:-1]
    if "\\" not in inner:
        return inner
    try:
        # \353 같은 8진 이스케이프를 바이트로 되돌린 뒤 UTF-8 로 읽는다.
        # 깨진 이스케이프(\999 등)에는 DeprecationWarning 이 나는데, 상위
        # 파이썬에서 에러로 승격될 수 있어 여기서 눌러 둔다. 어차피 아래
        # except 로 원본을 유지하므로 경고를 사람에게 보일 이유가 없다.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return (
                inner.encode("latin-1")
                .decode("unicode_escape")
                .encode("latin-1")
                .decode("utf-8")
            )
    except (UnicodeDecodeError, UnicodeEncodeError, SyntaxError, ValueError):
        return path


def _norm(path: str) -> str:
    """경로를 슬래시로 통일한다. git 은 슬래시를 쓰지만 인자로는 역슬래시가 온다."""
    return unquote_git_path(str(path)).replace("\\", "/").lstrip("./")


def in_allowed_dir(path: str) -> bool:
    return any(_norm(path).startswith(d) for d in ALLOWED_DIRS)


# ---------------------------------------------------------------
# 판정 — 순수 함수라 테스트로 고정한다
# ---------------------------------------------------------------


def spreadsheet_magic(head: bytes) -> str | None:
    """파일 앞부분을 보고 엑셀인지 판정한다. 아니면 None.

    내용을 파싱하지는 않는다 — 실데이터를 열어 읽는 것 자체가 목적이 아니고,
    '엑셀이다' 만 알면 막을 수 있다.
    """
    if head.startswith(OLE2_MAGIC):
        return "OLE2(구형 .xls) 바이너리"
    if head.startswith(ZIP_MAGIC) and XLSX_HINT in head:
        return "zip 안에 'xl/' — .xlsx 통합문서"
    return None


def inspect(paths: list[str], read_head=None) -> list[dict]:
    """막아야 할 파일 목록을 돌려준다. 각 항목은 {"경로", "이유"}.

    read_head(path) -> bytes 를 주면 매직바이트까지 검사한다. 테스트에서
    실제 파일 없이 주입할 수 있도록 인자로 뺐다.
    """
    hits: list[dict] = []

    for raw in paths:
        path = _norm(raw)
        if not path:
            continue
        name = path.rsplit("/", 1)[-1]
        if name in EXEMPT_NAMES:
            continue

        lower = path.lower()
        allowed = in_allowed_dir(path)

        # ① 비밀 디렉터리 — 허용 디렉터리보다 우선한다
        secret = next((d for d in SECRET_DIRS if path.startswith(d)), None)
        if secret:
            hits.append({"경로": path, "이유": f"{secret} 는 외부 공개 금지 디렉터리"})
            continue

        # ② 파일명 조각 — 경로 무관
        marker = next((m for m in NAME_MARKERS if m in name), None)
        if marker and not allowed:
            hits.append({"경로": path, "이유": f"파일명에 '{marker}' — 수령 자료로 보임"})
            continue

        # ③ 스프레드시트 확장자
        if lower.endswith(SPREADSHEET_EXT) and not allowed:
            hits.append({
                "경로": path,
                "이유": f"스프레드시트가 {ALLOWED_DIRS[0]} 밖에 있음",
            })
            continue

        # ④ 내용 검사 — 확장자를 바꿔 숨긴 경우
        if read_head is not None and not allowed:
            try:
                head = read_head(path)
            except OSError:
                head = b""
            kind = spreadsheet_magic(head or b"")
            if kind and not lower.endswith(SPREADSHEET_EXT):
                hits.append({
                    "경로": path,
                    "이유": f"확장자는 엑셀이 아닌데 내용이 엑셀 — {kind}",
                })

    return hits


# ---------------------------------------------------------------
# git 연동
# ---------------------------------------------------------------


def _git_paths(args: list[str]) -> list[str]:
    """git 이 돌려준 경로 목록을 읽는다. **반드시 -z 를 쓴다.**

    ★ 실제로 이 훅을 무력화시켰던 함정이다.
    git 의 core.quotepath 기본값이 true 라서, 한글 경로를 그대로 주지 않고
    8진수로 이스케이프한 뒤 따옴표로 감싼다:

        분석/급여대장_사본.xlsx
        → "\\353\\266\\204\\354\\204\\235/\\352\\270\\211\\354\\227\\254..."

    NAME_MARKERS 가 전부 한글이므로 이 형태에는 **하나도 매칭되지 않는다.**
    즉 수령한 실데이터(전부 한글 파일명)만 정확히 빠져나가고, 영문 파일명은
    걸리는 상태가 된다 — 검사가 있다고 믿는 채로 가장 위험한 것만 통과한다.

    -z 는 NUL 구분 출력이라 이스케이프를 하지 않는다. core.quotepath 설정에
    의존하지 않으므로 사람의 git 설정이 어떻든 같게 동작한다.
    """
    out = subprocess.run(
        ["git", *args, "-z"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return [p for p in out.stdout.split("\0") if p.strip()]


def staged_paths() -> list[str]:
    """커밋에 담길 파일. 삭제(D)는 뺀다 — 지우는 커밋을 막을 이유가 없다."""
    return _git_paths(["diff", "--cached", "--name-only", "--diff-filter=ACMR"])


def tracked_paths() -> list[str]:
    return _git_paths(["ls-files"])


def read_head_from_disk(path: str) -> bytes:
    p = Path(path)
    if not p.is_file():
        return b""
    with p.open("rb") as fh:
        return fh.read(MAGIC_SCAN_BYTES)


def main() -> int:
    check_all = "--all" in sys.argv
    paths = tracked_paths() if check_all else staged_paths()
    if not paths:
        return 0

    hits = inspect(paths, read_head=read_head_from_disk)
    if not hits:
        return 0

    print()
    print("✗ 커밋을 중단했습니다 — 실데이터로 보이는 파일이 포함돼 있습니다.")
    print()
    for h in hits:
        print(f"    {h['경로']}")
        print(f"      → {h['이유']}")
    print()
    print("  이 저장소는 공개입니다. push 되면 커밋을 지워도 GitHub 캐시와")
    print("  포크에 남아 되돌릴 수 없습니다.")
    print()
    print("  실데이터라면    : git restore --staged <파일>  후 data/raw/ 로 옮기세요")
    print("  가상 데이터라면 : data/sample/ 에 두면 통과합니다")
    print("  오탐이 확실하면 : git commit --no-verify  (규칙 조정은")
    print("                    scripts/check_staged_files.py 의 NAME_MARKERS)")
    print()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
