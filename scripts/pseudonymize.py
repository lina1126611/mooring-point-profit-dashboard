"""가명화 파이프라인 — 실데이터를 두 갈래로 나눈다.

    python scripts/pseudonymize.py            # 가명화 + 검토표 (LLM 없음)
    python scripts/pseudonymize.py --llm      # 규칙에 안 걸린 것만 로컬 LLM에 물어봄
    python scripts/pseudonymize.py --review   # 검토표만 (가명 데이터 안 만듦)

산출물:
    data/local/   원본 그대로. 가명 매핑표 + 사람이 볼 검토표.  ← 절대 외부 금지
    data/pseudo/  가명화된 거래 데이터.                          ← 외부 공유 가능

왜 두 갈래인가 (src/pseudonym.py 의 주석과 같은 이야기):
    가명화 데이터로는 분류를 할 수 없다. '사용처'를 가맹점_017 로 바꾸면
    "주유소니까 차량유지비" 라는 근거가 사라진다. 그래서 분류 판단은 원본을
    쥔 로컬(data/local 검토표 + 로컬 LLM)에서 하고, 밖으로 내보내는 것은
    구조 확인용 가명 데이터뿐이다.

검토표 쓰는 법:
    금액 내림차순 + 누적비중이 붙어 있다. 위에서부터 훑다가 누적 80~90%
    지점에서 멈추면 된다. 남은 꼬리는 미분류로 두고 총액만 UI에 노출한다
    (CLAUDE.md: 억지로 끼워 맞추면 손익이 조용히 틀어진다).
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src import local_llm, paths, pseudonym  # noqa: E402
from src.classify import classify_account  # noqa: E402
from src.rules import UNCLASSIFIED  # noqa: E402

# 경로는 src/paths.py 가 해석한다 — 세 폴더 모두 저장소 밖에 둔다.
# 모듈 로드 시점에 굳히지 않는다(설정을 고친 뒤 다시 돌릴 때를 위해).
MAPPING_NAME = "mappings.json"

# ---------------------------------------------------------------
# ERP 양식별 설정
#
# 헤더가 1행에 있다 (0행은 '매입(세금계산서)' 같은 제목).
# 가명화 대상 = 회사·현장·개인을 특정하는 컬럼.
# 보존 = 금액·날짜(손익 정확성) + 용도·증빙·구분(일반 회계용어. 바꾸면 분류가 죽는다).
#
# TODO: load_real_data.py 를 만들 때 이 표를 rules.py 로 옮긴다
#       (CLAUDE.md: 양식이 바뀌면 코드가 아니라 rules.py 만 고친다)
# ---------------------------------------------------------------
FORMS: list[dict] = [
    {
        "file": "매출(세금계산서)_202608031241.xls",
        "vendor": "공급받는자상호",
        "amount": "합계금액",
        "pseudo": {"공급받는자상호": "vendor", "프로젝트/현장": "project",
                   "내용": "description", "비고": "description", "메모": "description"},
    },
    {
        "file": "매입(세금계산서)_202608031247.xls",
        "vendor": "공급자상호",
        "amount": "합계금액",
        "pseudo": {"공급자상호": "vendor", "프로젝트/현장": "project",
                   "내용": "description", "비고": "description", "메모": "description"},
    },
    {
        "file": "매입(간이영수증)_202608031250.xls",
        "vendor": "사용처",
        "amount": "사용금액",
        "pseudo": {"사용처": "merchant", "프로젝트/현장": "project", "사용자": "user",
                   "내용": "description", "비고": "description", "메모": "description"},
    },
    {
        "file": "매입(국세_지방세)_202608031254.xls",
        "vendor": None,
        "amount": "총납부세액",
        "pseudo": {"프로젝트/현장": "project", "내용": "description",
                   "비고": "description", "메모": "description"},
    },
    {
        "file": "급여대장_202608031253.xls",
        "vendor": None,
        "amount": "지급액",
        "pseudo": {"프로젝트/현장": "project", "제목": "description",
                   "비고": "description"},
    },
    {
        "file": "4대보험조회_202608031253.xls",
        "vendor": None,
        "amount": "납부할금액",
        "pseudo": {"비고": "description"},
    },
]

# 카드 영수증 검토표를 만들 대상 (건수가 압도적이라 여기가 핵심 작업)
MERCHANT_FORM = "매입(간이영수증)_202608031250.xls"


def read_form(name: str, raw_dir: Path) -> pd.DataFrame | None:
    path = raw_dir / name
    if not path.exists():
        print(f"  [건너뜀] {name} 없음")
        return None
    df = pd.read_excel(path, sheet_name=0, header=1)
    df.columns = [str(c).strip() for c in df.columns]
    return df.dropna(how="all").reset_index(drop=True)


def won(n) -> str:
    return f"{int(n or 0):,}"


# ===============================================================
# ① 가명 매핑표 만들기
# ===============================================================


def build_mappings(
    frames: dict[str, pd.DataFrame], mapping_path: Path
) -> dict[str, dict[str, str]]:
    """전 파일을 훑어 성격별 매핑표를 만든다.

    파일별로 따로 만들면 같은 거래처가 파일마다 다른 가명을 받아 대조가
    불가능해진다. 그래서 성격(vendor/project/...) 단위로 하나만 만든다.
    기존 매핑표가 있으면 이어 붙인다(이미 검토한 가명이 안 바뀌도록).
    """
    mappings = pseudonym.load_mapping(mapping_path)

    for kind, prefix in pseudonym.DEFAULT_PREFIXES.items():
        values: list = []
        for form in FORMS:
            df = frames.get(form["file"])
            if df is None:
                continue
            for column, col_kind in form["pseudo"].items():
                if col_kind == kind and column in df.columns:
                    values.extend(df[column].tolist())
        if values:
            mappings[kind] = pseudonym.build_mapping(
                values, prefix, existing=mappings.get(kind)
            )
    return mappings


# ===============================================================
# ② 사용처 검토표 (원본 — 로컬 전용)
# ===============================================================


def merchant_review(df: pd.DataFrame, use_llm: bool) -> list[dict]:
    """사용처별 건수·금액 집계 + 규칙 판정 + (선택) LLM 후보."""
    agg = (
        df.assign(_amt=pd.to_numeric(df["사용금액"], errors="coerce").fillna(0))
        .groupby(df["사용처"].astype(str).str.strip(), dropna=True)
        .agg(건수=("_amt", "size"), 금액=("_amt", "sum"))
        .reset_index()
        .rename(columns={"사용처": "사용처"})
        .sort_values("금액", ascending=False)
        .reset_index(drop=True)
    )
    agg = agg[agg["사용처"].str.len() > 0]

    total = agg["금액"].sum()
    running = 0.0
    rows: list[dict] = []

    # 규칙 1차 — 이미 테스트된 코드다. 여기서 걸리면 LLM 을 안 쓴다.
    llm_targets = 0
    for r in agg.itertuples(index=False):
        rule = classify_account(vendor=r.사용처, description=None)
        running += r.금액
        rows.append({
            "사용처": r.사용처,
            "건수": int(r.건수),
            "금액": int(r.금액),
            "누적비중%": round(running / total * 100, 1) if total else 0.0,
            "규칙판정": rule,
            "LLM후보": "",
            "확신도": "",
            "근거": "",
            "사람확정": "",   # 여기를 사람이 채운다
        })
        if rule == UNCLASSIFIED:
            llm_targets += 1

    print(f"  규칙으로 판정: {len(rows) - llm_targets}종 / 미분류: {llm_targets}종")

    if not use_llm:
        return rows
    if not local_llm.available():
        print("  [LLM 건너뜀] Ollama 가 응답하지 않습니다. 아래 설치 안내 참고.")
        return rows

    models = local_llm.installed_models()
    model = next((m for m in models if m.startswith("qwen2.5")), None) or (
        models[0] if models else None
    )
    if model is None:
        print("  [LLM 건너뜀] 설치된 모델이 없습니다. `ollama pull qwen2.5:3b`")
        return rows

    print(f"  LLM({model})으로 미분류 {llm_targets}종 조회 — 몇 분 걸립니다")
    allowed = local_llm.allowed_accounts()
    done = 0
    for row in rows:
        if row["규칙판정"] != UNCLASSIFIED:
            continue
        got = local_llm.classify_merchant(row["사용처"], model=model, allowed=allowed)
        row["LLM후보"] = got["account"]
        row["확신도"] = got["confidence"]
        row["근거"] = got["reason"]
        done += 1
        if done % 20 == 0:
            print(f"    {done}/{llm_targets}")
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig — 안 붙이면 엑셀에서 한글이 깨진다
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


# ===============================================================
# ③ 가명화 데이터 내보내기
# ===============================================================


def export_pseudo(
    frames: dict[str, pd.DataFrame], mappings: dict, pseudo_dir: Path
) -> list[str]:
    pseudo_dir.mkdir(parents=True, exist_ok=True)
    leaks: list[str] = []

    for form in FORMS:
        df = frames.get(form["file"])
        if df is None:
            continue
        out = df.copy()
        for column, kind in form["pseudo"].items():
            if column not in out.columns:
                continue
            mapping = mappings.get(kind, {})
            report = pseudonym.coverage(out[column], mapping)
            if not report["안전"]:
                leaks.append(f"{form['file']} · {column}: {report['미등록값'][:3]}")
            out[column] = pseudonym.apply_mapping(out[column], mapping)

        target = pseudo_dir / (Path(form["file"]).stem + "_가명.xlsx")
        out.to_excel(target, index=False)
        print(f"  {target.name}  ({len(out)}행)")
    return leaks


# ===============================================================
def main() -> None:
    use_llm = "--llm" in sys.argv
    review_only = "--review" in sys.argv

    raw_dir = paths.raw_dir()
    local_dir = paths.local_dir()
    pseudo_dir = paths.pseudo_dir()
    mapping_path = local_dir / MAPPING_NAME

    print("=== 경로 ===")
    print(paths.describe())
    for kind, p in (("raw", raw_dir), ("local", local_dir), ("pseudo", pseudo_dir)):
        warning = paths.warn_if_inside_repo(kind, p)
        if warning:
            print(warning)
    print()

    if not raw_dir.exists() or not any(raw_dir.glob("*.xls")):
        sys.exit(
            f"{raw_dir} 에 실데이터가 없습니다.\n"
            f"paths.local.json 의 'raw' 경로를 확인하세요."
        )

    print("=== 원본 읽기 ===")
    frames = {f["file"]: read_form(f["file"], raw_dir) for f in FORMS}
    loaded = {k: v for k, v in frames.items() if v is not None}
    print(f"  {len(loaded)}개 파일, 총 {sum(len(v) for v in loaded.values())}행")

    print("\n=== ① 가명 매핑표 ===")
    mappings = build_mappings(frames, mapping_path)
    for kind, m in sorted(mappings.items()):
        print(f"  {pseudonym.DEFAULT_PREFIXES.get(kind, kind):6} {len(m):4d}종")
    pseudonym.save_mapping(mapping_path, mappings)
    # 절대경로로 찍는다 — 저장소 밖이라 relative_to 가 예외를 낸다
    print(f"  저장: {mapping_path}  ← 외부 유출 금지")

    print("\n=== ② 사용처 검토표 ===")
    card = frames.get(MERCHANT_FORM)
    if card is not None:
        rows = merchant_review(card, use_llm)
        out = local_dir / "사용처_검토표.csv"
        write_csv(out, rows)
        print(f"  저장: {out}  ({len(rows)}종)")
        # 파레토 안내 — 어디까지 보면 되는지
        for cut in (80, 90):
            n = sum(1 for r in rows if r["누적비중%"] <= cut)
            amt = sum(r["금액"] for r in rows[:n])
            print(f"    상위 {n:3d}종이 금액의 {cut}% ({won(amt)}원)")

    if review_only:
        print("\n--review 이므로 가명 데이터는 만들지 않았습니다.")
        return

    print("\n=== ③ 가명화 데이터 ===")
    leaks = export_pseudo(frames, mappings, pseudo_dir)
    if leaks:
        print("\n  ★ 미등록 값이 있어 외부로 내보내면 안 됩니다:")
        for line in leaks:
            print(f"    - {line}")
    else:
        print(f"  전 컬럼 가명화 확인 완료 → {pseudo_dir}")

    if not use_llm and not local_llm.available():
        print(
            "\n--- 로컬 LLM 을 붙이려면 ---\n"
            "  1) https://ollama.com/download 에서 Windows 설치\n"
            "  2) ollama pull qwen2.5:3b     (약 2GB — 지금 여유 RAM 기준 권장)\n"
            "     여유 RAM 이 8GB 이상이면 qwen2.5:7b 가 더 정확합니다\n"
            "  3) python scripts/pseudonymize.py --llm"
        )


if __name__ == "__main__":
    main()
