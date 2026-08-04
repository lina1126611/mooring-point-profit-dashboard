"""실데이터 경로 해석 — 민감한 자료를 저장소 폴더 **밖**에 둔다.

왜 밖으로 빼는가:
    `.gitignore` 와 pre-commit 훅은 **커밋**을 막는다. 읽기는 막지 못한다.
    저장소 폴더를 AI 에이전트·코드 도구·백업 동기화에 물리면 그 안의 파일은
    그냥 읽힌다. 실데이터가 폴더 안에 있으면 규칙이 아니라 위치가 위험을 만든다.
    그래서 경로 자체를 밖으로 옮기고, 코드는 '어디서 읽을지' 만 갖는다.

    관례가 아니라 구조로 막는 방식이다. 어떤 도구를 붙여도 도달할 수 없다.

해석 순서 (먼저 잡히는 것이 이긴다):
    1) 환경변수  MOORING_RAW_DIR / MOORING_LOCAL_DIR / MOORING_PSEUDO_DIR
    2) 설정 파일 paths.local.json  (저장소 루트, .gitignore 대상)
    3) 저장소 내 기본 경로 data/raw 등 — **있으면 경고한다**

3번을 남겨 두는 이유: 팀원이 아직 옮기지 않았을 때 스크립트가 그냥 죽으면
"왜 안 되지" 로 시간을 버린다. 동작은 시키되 경고해서 옮기게 만든다.

paths.local.json 예:
    {
      "raw":    "C:/Users/이름/mooring-private/raw",
      "local":  "C:/Users/이름/mooring-private/local",
      "pseudo": "C:/Users/이름/mooring-private/pseudo"
    }
"""

from __future__ import annotations

import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "paths.local.json"

# 성격 → (환경변수, 설정 키, 저장소 내 기본 경로)
_SPECS: dict[str, tuple[str, str, Path]] = {
    "raw": ("MOORING_RAW_DIR", "raw", PROJECT_ROOT / "data" / "raw"),
    "local": ("MOORING_LOCAL_DIR", "local", PROJECT_ROOT / "data" / "local"),
    "pseudo": ("MOORING_PSEUDO_DIR", "pseudo", PROJECT_ROOT / "data" / "pseudo"),
}

# 어디서 읽었는지 스크립트가 사람에게 알려줄 수 있게 남긴다.
# 숫자를 의심할 때 "그 파일 맞나" 부터 확인해야 하므로 출처가 보여야 한다.
_ORIGIN: dict[str, str] = {}


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve(kind: str, config: dict | None = None) -> Path:
    """경로 하나를 해석한다. 존재 여부는 확인하지 않는다(호출부의 일)."""
    if kind not in _SPECS:
        raise ValueError(f"알 수 없는 경로 종류: {kind!r} (가능: {', '.join(_SPECS)})")

    env_key, cfg_key, fallback = _SPECS[kind]

    value = os.environ.get(env_key, "").strip()
    if value:
        _ORIGIN[kind] = f"환경변수 {env_key}"
        return Path(value).expanduser().resolve()

    cfg = _load_config() if config is None else config
    raw = cfg.get(cfg_key)
    if isinstance(raw, str) and raw.strip():
        _ORIGIN[kind] = f"{CONFIG_PATH.name} 의 '{cfg_key}'"
        return Path(raw.strip()).expanduser().resolve()

    _ORIGIN[kind] = "저장소 내 기본 경로(권장하지 않음)"
    return fallback


def origin(kind: str) -> str:
    """그 경로를 어디서 얻었는지. resolve 를 먼저 불러야 한다."""
    return _ORIGIN.get(kind, "미해석")


def is_inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(PROJECT_ROOT)
        return True
    except ValueError:
        return False


def warn_if_inside_repo(kind: str, path: Path) -> str | None:
    """저장소 안에 있으면 경고 문구를 돌려준다. 없으면 None.

    조용히 넘기지 않는 이유 — 위치가 안전하다고 믿는 채로 도구를 붙이는 것이
    가장 위험하다. 훅은 커밋만 막고 읽기는 막지 않는다.
    """
    if not is_inside_repo(path):
        return None
    env_key, cfg_key, _ = _SPECS[kind]
    return (
        f"★ {kind} 경로가 저장소 안에 있습니다: {path}\n"
        f"   .gitignore 와 훅은 커밋만 막습니다 — 이 폴더를 읽는 도구(AI 에이전트,\n"
        f"   백업 동기화 등)에는 그대로 노출됩니다.\n"
        f"   {CONFIG_PATH.name} 에 '{cfg_key}' 를 적거나 {env_key} 를 설정해 밖으로 옮기세요."
    )


def describe(kinds=("raw", "local", "pseudo")) -> str:
    """스크립트 첫 줄에 찍을 경로 요약. 출처까지 같이 보여준다."""
    lines = []
    for kind in kinds:
        p = resolve(kind)
        mark = "★저장소 내" if is_inside_repo(p) else "밖"
        lines.append(f"  {kind:7} {p}  [{mark} · {origin(kind)}]")
    return "\n".join(lines)


# 편의 접근자 — import 시점에 고정하지 않는다.
# 모듈 로드 순간의 환경변수로 굳으면 테스트에서 바꿀 수 없고, 한 프로세스에서
# 설정을 바꿔 다시 읽는 경우에도 옛 값이 남는다.
def raw_dir() -> Path:
    return resolve("raw")


def local_dir() -> Path:
    return resolve("local")


def pseudo_dir() -> Path:
    return resolve("pseudo")
