"""data/sample/ 에 현실적인 ERP 엑셀 샘플을 생성한다.

실제 ERP 추출물처럼 파일마다 컬럼명·날짜형식·금액형식이 다르게 만든다.
(매핑 테이블이 실제로 필요한지 검증하기 위함)

거래처와 품목은 업종 단위로 일관되게 짝짓는다. 무작위로 섞으면
거래처명 기반 분류 규칙을 검증할 수 없기 때문이다.

    python scripts/generate_sample_data.py
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

SEED = 20260802
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "sample"

START = date(2026, 1, 1)
END = date(2026, 7, 31)

# ---------------------------------------------------------------
# 프로젝트 6건 (해양구조물 수주, B2G)
# ---------------------------------------------------------------
PROJECTS = [
    ("부산항 신항 부잔교 설치공사",        "부산항만공사",       3_480_000_000),
    ("여수 묘도 계류시스템 제작설치",      "여수광양항만공사",   2_150_000_000),
    ("서귀포항 부유식 방파제 보강",        "제주특별자치도",     1_260_000_000),
    ("인천 북항 계류부표 교체공사",        "인천항만공사",         880_000_000),
    ("목포 신항 요트계류장 부잔교",        "전라남도",             540_000_000),
    ("울산항 LNG터미널 계류설비 보수",     "울산항만공사",       1_720_000_000),
]
PROJECT_NAMES = [p[0] for p in PROJECTS]
CONTRACT_BY_NAME = {name: amount for name, _, amount in PROJECTS}

# 프로젝트별 매입 원가율 = 매입액 / 인식매출.
# 무작위로 매입을 뿌리면 변동비가 매출을 넘어 공헌이익이 음수가 된다(실제로 그랬다).
# 현장마다 수익성이 다르게 보이도록 원가율을 벌려 둔다 — 이 값이 1단 공헌이익률의 원천.
PURCHASE_COST_RATIO = {
    "부산항 신항 부잔교 설치공사":      0.58,
    "여수 묘도 계류시스템 제작설치":    0.66,
    # 서귀포항은 원가율이 높은 '문제 현장'으로 둔다. 1단 공헌이익만 보면
    # 남는 것처럼 보이지만 고정비·맨데이를 얹으면 경고선(10%) 아래로 내려간다.
    # 브리핑 화면의 경고 박스가 실제로 걸리는지 확인하기 위한 케이스.
    "서귀포항 부유식 방파제 보강":      0.78,
    "인천 북항 계류부표 교체공사":      0.54,
    "목포 신항 요트계류장 부잔교":      0.68,
    "울산항 LNG터미널 계류설비 보수":   0.62,
}

# ---------------------------------------------------------------
# 매입처 60곳 — 업종별로 묶는다. 품목은 같은 업종에서만 뽑는다.
# ---------------------------------------------------------------
VENDOR_GROUPS: dict[str, list[str]] = {
    "강재": [
        "대한제강(주)", "동국제강(주)", "포스코강판", "세아철강", "한국철강산업",
        "삼일강재(주)", "부산강판상사", "동양특수강", "태창스틸", "SM한국특수형강",
        "광양철강유통", "제일강재", "우성스테인리스", "남해철강", "성진강판",
    ],
    "체인": [
        "삼호앵커체인", "대양체인(주)", "한국와이어로프", "고려강선",
        "부산체인공업", "해성마린체인", "동아로프산업",
    ],
    "의장": [
        "오션부이(주)", "마린폰툰코리아", "한국부이산업", "블루마린의장",
        "해양의장기술", "대성마린테크",
    ],
    "외주": [
        "성원기계공업(주)", "우진철구", "대명중공업", "신진용접공사",
        "한백엔지니어링", "동해플랜트외주", "삼정테크외주가공", "부산절단가공",
        "경남철구제작", "해동용역(주)", "태영산업용역",
    ],
    "도장": [
        "KCC도료대리점", "삼화페인트상사", "해양방식도장(주)", "노루페인트부산",
    ],
    "운반": [
        "한진해운운송", "대영화물운송", "부산크레인중기", "삼성운반물류",
        "동광중기임대", "해상운반전문",
    ],
    "소모품": [
        "대성공구상사", "한국산업가스", "부산용접재료", "세방소모품",
    ],
    "장비임차": [
        "남해바지선", "대양예인선(주)", "해양크레인선",
    ],
    "검사": [
        "한국선급(KR)", "해양기술검사원", "동남측량설계",
    ],
}

# ---------------------------------------------------------------
# 매입 품목 — (업종, 품목명, 금액하한, 금액상한)
# ---------------------------------------------------------------
PURCHASE_ITEMS = [
    ("강재",     "일반구조용 강재 SS275 납품",   8_000_000,  85_000_000),
    ("강재",     "후판 철판 12T 절단납품",       5_000_000,  62_000_000),
    ("강재",     "H형강 300x300 자재",           6_000_000,  48_000_000),
    ("강재",     "스테인리스 STS304 판재",       3_000_000,  28_000_000),
    ("체인",     "앵커체인 76mm 납품",          12_000_000, 140_000_000),
    ("체인",     "와이어로프 및 샤클 일체",      2_000_000,  24_000_000),
    ("체인",     "계류용 앵커블록 제작",        15_000_000, 120_000_000),
    ("의장",     "폴리에틸렌 부이 제작납품",     7_000_000,  70_000_000),
    ("의장",     "폰툰 유닛 제작",              20_000_000, 180_000_000),
    ("의장",     "부잔교 의장품 일체",           4_000_000,  38_000_000),
    ("외주",     "철구조물 외주가공",           10_000_000,  95_000_000),
    ("외주",     "용접 외주용역",                3_000_000,  42_000_000),
    ("외주",     "절단가공 외주",                2_000_000,  26_000_000),
    ("외주",     "수중설치 잠수용역",            8_000_000,  64_000_000),
    ("외주",     "구조검토 설계용역",            5_000_000,  35_000_000),
    ("도장",     "방식도장 시공",                4_000_000,  46_000_000),
    ("도장",     "에폭시 도료 자재",             1_500_000,  16_000_000),
    ("운반",     "자재 운반비 (현장반입)",         800_000,  12_000_000),
    ("운반",     "중량물 운송 및 상하차",        1_200_000,  18_000_000),
    ("운반",     "크레인 운반 작업",               900_000,   9_000_000),
    ("소모품",   "용접봉 및 소모품",               300_000,   4_500_000),
    ("소모품",   "산업용 가스 (산소/아르곤)",      250_000,   3_200_000),
    ("소모품",   "절단석 및 공구 소모품",          200_000,   2_800_000),
    ("장비임차", "바지선 용선료",               10_000_000,  88_000_000),
    ("장비임차", "예인선 임차",                  6_000_000,  52_000_000),
    ("장비임차", "해상크레인 장비임차",         14_000_000, 130_000_000),
    ("검사",     "선급 검사수수료",              1_000_000,   8_000_000),
    ("검사",     "비파괴검사 용역",              1_500_000,  11_000_000),
    ("검사",     "측량 용역",                    1_200_000,   9_500_000),
    # --- 규칙에 안 걸릴 모호한 적요 (미분류 유발, 의도적) ---
    ("외주",     "기타 정산분",                    500_000,  15_000_000),
    ("운반",     "제잡비 정산",                    300_000,   6_000_000),
    ("소모품",   "현장 잡비",                      200_000,   4_000_000),
]

# ---------------------------------------------------------------
# 경비 — (적요, ERP계정(일부 공란), 지급처풀, 금액하한, 금액상한, 프로젝트귀속)
# ---------------------------------------------------------------
LESSORS = ["대한자산관리", "부산항만부동산", "신항물류창고", "해운대빌딩관리단"]
INSURERS = ["삼성화재", "DB손해보험", "근로복지공단", "현대해상"]
BANKS = ["국민은행", "기업은행", "부산은행", "산업은행"]
LEASECOS = ["롯데렌탈", "현대캐피탈", "신한리스", "AJ네트웍스"]
TELCOS = ["KT", "SK브로드밴드", "LG유플러스"]
FUEL = ["SK에너지 주유소", "GS칼텍스 충전소", "현대오일뱅크"]
TRAVEL = ["대한항공", "코레일", "부산호텔", "여수관광호텔"]
OFFICE = ["오피스디포", "문구나라", "이마트"]
SAFETY = ["세이프티코리아", "산업안전용품", "3M대리점"]
UTIL = ["한국전력공사", "한국수자원공사"]
PROS = ["세무법인 정도", "법무사 김영호", "노무법인 한결"]
MISC = ["기타거래처", "현장정산", "㈜대한종합"]

EXPENSE_ITEMS = [
    ("본사 사무실 임차료",      "임차료",     LESSORS,  4_500_000,  4_500_000, False),
    ("자재창고 임차료",         "임차료",     LESSORS,  2_800_000,  2_800_000, False),
    ("야적장 임차료",           "",           LESSORS,  1_900_000,  1_900_000, False),
    ("산재보험료 납부",         "보험료",     INSURERS,   850_000,  3_200_000, False),
    ("화재보험료",              "보험료",     INSURERS,   420_000,  1_100_000, False),
    ("근재보험 가입",           "",           INSURERS,   600_000,  2_400_000, True),
    ("운전자금 대출이자",       "이자비용",   BANKS,    3_200_000,  9_800_000, False),
    ("시설자금 이자 상환",      "",           BANKS,    2_100_000,  6_500_000, False),
    ("차량 리스료",             "리스료",     LEASECOS,   780_000,  1_650_000, False),
    ("복합기 리스료",           "",           LEASECOS,   180_000,    320_000, False),
    ("사무실 통신요금",         "통신비",     TELCOS,     210_000,    480_000, False),
    ("현장 인터넷 통신비",      "",           TELCOS,      90_000,    180_000, True),
    ("법인차량 유류대",         "차량유지비", FUEL,       180_000,    920_000, True),
    ("현장 출장 여비교통비",    "",           TRAVEL,     120_000,  1_400_000, True),
    ("출장 숙박비",             "여비교통비", TRAVEL,     180_000,  1_900_000, True),
    ("거래처 접대비",           "접대비",     MISC,       150_000,    880_000, False),
    ("사무용품 구입",           "소모품비",   OFFICE,      80_000,    620_000, False),
    ("현장 안전용품 소모품",    "",           SAFETY,     240_000,  2_100_000, True),
    ("전기요금 (현장)",         "수도광열비", UTIL,       320_000,  2_800_000, True),
    ("설비 감가상각비 계상",    "감가상각비", MISC,     2_400_000,  5_600_000, False),
    ("세무기장 지급수수료",     "지급수수료", PROS,       300_000,    700_000, False),
    ("법무 등기 수수료",        "",           PROS,       150_000,    900_000, False),
    # --- 미분류 유발 ---
    ("기타 운영비",             "",           MISC,       200_000,  3_100_000, False),
    ("잡비",                    "",           MISC,        80_000,  1_200_000, True),
    ("정산 차액",               "",           MISC,       100_000,  2_600_000, False),
]

# ---------------------------------------------------------------
# 매출 — 기성 청구
# ---------------------------------------------------------------
SALES_ITEMS = [
    "선급금 청구",
    "1차 기성금 청구",
    "2차 기성금 청구",
    "3차 기성금 청구",
    "4차 기성금 청구",
    "준공정산 청구",
    "설계변경 증액분",
]
GENERAL_CONTRACTORS = [
    "현대건설(주)", "대우건설(주)", "삼성물산 건설부문", "GS건설(주)",
    "동부건설(주)", "한국수자원공사", "해양수산부 부산지방청",
    "한국해양과학기술원", "경상남도",
]


def rand_date(rng: random.Random) -> date:
    span = (END - START).days
    return START + timedelta(days=rng.randint(0, span))


def won(rng: random.Random, lo: int, hi: int) -> int:
    """만원 단위로 떨어지는 금액."""
    if lo == hi:
        return lo
    return rng.randint(lo // 10_000, hi // 10_000) * 10_000


def build_purchases(rng: random.Random, sales: pd.DataFrame) -> pd.DataFrame:
    """매입 세금계산서 목록. 날짜=datetime, 금액=정수.

    프로젝트별 인식매출 × 원가율을 목표로 잡고, 그 금액에 닿을 때까지
    품목을 뽑아 채운다. 매출과 무관하게 뿌리면 변동비가 매출을 넘어
    공헌이익이 음수가 되어 2단 손익 시연이 성립하지 않는다.
    """
    rows = []
    revenue_by_project = sales.groupby("현장")["공급가액"].sum()

    for project, revenue in revenue_by_project.items():
        target = int(revenue * PURCHASE_COST_RATIO[project])
        spent = 0
        while spent < target:
            remaining = target - spent
            candidates = [it for it in PURCHASE_ITEMS if it[2] <= remaining]
            if not candidates:
                break  # 남은 금액이 최소 품목보다 작으면 마감
            group, item, lo, hi = rng.choice(candidates)
            supply = won(rng, lo, min(hi, remaining))
            spent += supply
            rows.append(_purchase_row(rng, group, item, supply, project))

    rng.shuffle(rows)
    return pd.DataFrame(rows)


def _purchase_row(rng: random.Random, group: str, item: str, supply: int, project: str) -> dict:
    vendor = rng.choice(VENDOR_GROUPS[group])  # 업종 일치 거래처만
    d = rand_date(rng)
    return {
        "작성일자": pd.Timestamp(d),
        "거래처명": vendor,
        "사업자번호": f"{rng.randint(100, 899)}-{rng.randint(10, 99)}-{rng.randint(10000, 99999)}",
        "품목": item,
        "공급가액": supply,
        "세액": round(supply * 0.1),
        "합계금액": supply + round(supply * 0.1),
        "프로젝트": project,
        "비고": "",
    }


def build_expenses(rng: random.Random, n: int) -> pd.DataFrame:
    """경비지출 대장. 날짜='YYYY-MM-DD' 문자열, 금액=콤마 문자열."""
    rows = []
    depts = ["관리부", "설계부", "공무부", "생산부"]
    for _ in range(n):
        desc, account, payees, lo, hi, to_project = rng.choice(EXPENSE_ITEMS)
        amt = won(rng, lo, hi)
        d = rand_date(rng)
        rows.append(
            {
                "지출일": d.strftime("%Y-%m-%d"),
                "지급처": rng.choice(payees),
                "적요": desc,
                "계정": account,
                "금액": f"{amt:,}",
                "부서": rng.choice(depts),
                "현장명": rng.choice(PROJECT_NAMES) if to_project else "",
            }
        )
    return pd.DataFrame(rows)


def build_sales(rng: random.Random) -> pd.DataFrame:
    """매출 세금계산서 목록. 날짜='YYYY/MM/DD' 문자열."""
    rows = []
    for name, client, contract in PROJECTS:
        n_claims = rng.randint(4, 7)
        total = int(contract * rng.uniform(0.70, 0.95))
        cuts = sorted(rng.uniform(0.1, 1.0) for _ in range(n_claims - 1))
        prev = 0.0
        portions = []
        for c in cuts + [1.0]:
            portions.append(c - prev)
            prev = c
        for i, p in enumerate(portions):
            supply = int(total * p / 10_000) * 10_000
            if supply <= 0:
                continue
            d = rand_date(rng)
            rows.append(
                {
                    "발행일": d.strftime("%Y/%m/%d"),
                    # 대부분 발주처 직접, 일부는 원청 경유
                    "거래처": client if rng.random() < 0.8 else rng.choice(GENERAL_CONTRACTORS),
                    "공급가액": supply,
                    "부가세": round(supply * 0.1),
                    "총액": supply + round(supply * 0.1),
                    "현장": name,
                    "품목명": SALES_ITEMS[min(i, len(SALES_ITEMS) - 1)],
                }
            )
    return pd.DataFrame(rows)


def inject_duplicates(rng: random.Random, df: pd.DataFrame, k: int) -> pd.DataFrame:
    """중복 감지 테스트용으로 기존 행을 그대로 복제해 끼워 넣는다."""
    dups = df.sample(n=k, random_state=rng.randint(0, 10_000))
    return (
        pd.concat([df, dups], ignore_index=True)
        .sample(frac=1.0, random_state=rng.randint(0, 10_000))
        .reset_index(drop=True)
    )


def build_projects_sheet() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "프로젝트명": name,
                "발주처": client,
                "착수일": "2026-01-05",
                "준공예정일": "2026-12-20",
                "계약금액": amount,
            }
            for name, client, amount in PROJECTS
        ]
    )


def build_mandays(rng: random.Random) -> pd.DataFrame:
    """설계 맨데이 투입 — ERP에 원가로 안 잡히는 부분."""
    roles = [
        ("설계팀장", 420_000),
        ("구조설계", 350_000),
        ("기본설계", 320_000),
        ("CAD 작도", 240_000),
        ("현장기술지원", 300_000),
    ]
    rows = []
    for name, _, contract in PROJECTS:
        # 큰 현장일수록 설계 투입이 많다. 무작위로 뿌리면 매출 최대 현장의
        # 맨데이가 최소로 나오는 등 앞뒤가 안 맞는 표가 만들어진다.
        scale = min(2.0, max(0.4, contract / 1_500_000_000))
        for role, rate in rng.sample(roles, k=rng.randint(3, 5)):
            rows.append(
                {
                    "현장명": name,
                    "직무": role,
                    "투입인원": rng.randint(1, 3),
                    "투입일수": round(rng.uniform(8, 65) * scale * 2) / 2,  # 0.5일 단위
                    "일단가": rate,
                }
            )
    return pd.DataFrame(rows)


def main() -> None:
    rng = random.Random(SEED)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 매입은 매출을 알아야 규모를 정할 수 있으므로 매출을 먼저 만든다.
    sales = build_sales(rng)
    purchases = inject_duplicates(rng, build_purchases(rng, sales), 6)
    expenses = inject_duplicates(rng, build_expenses(rng, 275), 4)

    targets = {
        "매입_세금계산서_2026.xlsx": purchases,
        "경비지출대장_2026.xlsx": expenses,
        "매출_세금계산서_2026.xlsx": sales,
        "프로젝트_계약현황.xlsx": build_projects_sheet(),
        "설계맨데이_투입내역.xlsx": build_mandays(rng),
    }
    for fname, df in targets.items():
        df.to_excel(OUT_DIR / fname, index=False)
        print(f"{fname:34s} {len(df):5d} rows  {list(df.columns)}")

    total = len(purchases) + len(expenses) + len(sales)
    print(f"\n거래 총계: {total} 건 (매입 {len(purchases)} / 경비 {len(expenses)} / 매출 {len(sales)})")


if __name__ == "__main__":
    main()
