"""git 훅을 이 PC 에 설치한다. **클론한 사람이 각자 한 번 실행한다.**

    python scripts/install_hooks.py            # 설치
    python scripts/install_hooks.py --check     # 설치됐는지만 확인
    python scripts/install_hooks.py --uninstall # 제거

왜 각자 실행해야 하는가:
    훅은 .git/hooks/ 에 들어가는데 이 폴더는 **커밋되지 않는다**. git 이
    저장소 내부 설정으로 취급해 이력에 담지 않기 때문이다. 그래서 훅 본체를
    scripts/hooks/ 에 커밋해 두고, 이 스크립트가 복사해 넣는다.

설치되는 훅:
    pre-commit — 실데이터로 보이는 파일이 staged 되면 커밋을 막는다.
                 (판정 로직은 scripts/check_staged_files.py)
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = PROJECT_ROOT / "scripts" / "hooks"
HOOKS = ("pre-commit",)


def hooks_dir() -> Path:
    """훅이 들어갈 실제 경로.

    core.hooksPath 를 설정한 저장소도 있고(팀 공용 훅 관리 도구를 쓰는 경우),
    worktree 를 쓰면 .git 이 폴더가 아니라 파일이다. git 에 직접 물어보는 편이
    맞다 — 경로를 추측하면 설치했다고 믿는 채로 안 돌아간다.
    """
    custom = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()
    if custom:
        p = Path(custom)
        return p if p.is_absolute() else PROJECT_ROOT / p

    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()
    if not common:
        raise SystemExit("git 저장소가 아닙니다.")
    base = Path(common)
    if not base.is_absolute():
        base = PROJECT_ROOT / base
    return base / "hooks"


def make_executable(path: Path) -> None:
    """실행 권한을 준다.

    Windows 에서는 의미가 없지만 Git for Windows 는 sh 로 훅을 돌리므로
    동작한다. macOS·Linux 에서 클론한 팀원에게는 이 비트가 없으면 훅이
    조용히 무시된다.
    """
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install() -> int:
    target_dir = hooks_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    for name in HOOKS:
        src = SOURCE_DIR / name
        if not src.exists():
            print(f"  ✗ {name}: 원본이 없습니다 ({src.relative_to(PROJECT_ROOT)})")
            return 1

        dst = target_dir / name
        # 이미 다른 훅이 있으면 덮어쓰기 전에 백업한다. 남이 만든 훅을
        # 조용히 지우면 그 사람의 검사가 사라진다.
        if dst.exists() and dst.read_bytes() != src.read_bytes():
            backup = dst.with_suffix(".backup")
            shutil.copy2(dst, backup)
            print(f"  기존 {name} 을 {backup.name} 으로 백업했습니다")

        shutil.copy2(src, dst)
        make_executable(dst)
        print(f"  ✓ {name} → {dst}")

    print("\n실데이터 커밋 차단이 켜졌습니다.")
    print("확인: python scripts/check_staged_files.py --all")
    return 0


def check() -> int:
    target_dir = hooks_dir()
    missing = []
    for name in HOOKS:
        dst = target_dir / name
        src = SOURCE_DIR / name
        if not dst.exists():
            missing.append(f"{name}: 설치 안 됨")
        elif src.exists() and dst.read_bytes() != src.read_bytes():
            missing.append(f"{name}: 설치돼 있지만 내용이 최신이 아님")
        else:
            print(f"  ✓ {name} 설치됨")

    for line in missing:
        print(f"  ✗ {line}")
    if missing:
        print("\n설치: python scripts/install_hooks.py")
        return 1
    return 0


def uninstall() -> int:
    target_dir = hooks_dir()
    for name in HOOKS:
        dst = target_dir / name
        if dst.exists():
            dst.unlink()
            print(f"  제거: {name}")
        backup = dst.with_suffix(".backup")
        if backup.exists():
            print(f"  참고: 백업이 남아 있습니다 — {backup}")
    return 0


def main() -> int:
    print(f"훅 디렉터리: {hooks_dir()}\n")
    if "--check" in sys.argv:
        return check()
    if "--uninstall" in sys.argv:
        return uninstall()
    return install()


if __name__ == "__main__":
    raise SystemExit(main())
