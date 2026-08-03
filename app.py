"""Mooring Point 경영지원 대시보드 — Streamlit 진입점.

이 파일은 얇은 UI 레이어로만 유지한다.
금액 계산식은 전부 src/ 아래에 두고 여기서는 호출만 한다. (테스트 가능성 확보)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import db as db_module  # noqa: E402
from src import finance, ingest, masters, report  # noqa: E402
from src.classify import (  # noqa: E402
    classification_stats,
    classify_dataframe,
    reclassify_all,
    set_override,
    set_override_bulk,
)
from src.rules import (  # noqa: E402
    AMBIGUOUS_ACCOUNTS,
    COST_BEHAVIOR,
    FIXED,
    NOT_APPLICABLE,
    UNCLASSIFIED,
    VARIABLE,
)

st.set_page_config(
    page_title="Mooring Point 경영지원 대시보드",
    page_icon="⚓",
    layout="wide",
)

ACCOUNT_CHOICES = sorted(COST_BEHAVIOR.keys())
BEHAVIOR_CHOICES = [VARIABLE, FIXED, NOT_APPLICABLE]
TX_TYPES = ["매입", "경비", "매출"]

# ---------------------------------------------------------------
# 색 — 기본 회색조, 이익은 파랑 계열, 경고만 빨강.
# 맨데이는 색을 하나 더 쓰지 않고 '빗금'으로 구분한다.
# 이 화면의 목적이 "안 잡히던 인건비가 여기 있다"를 보여 주는 것이므로
# 눈에 걸려야 하지만, 경고 빨강과 섞이면 안 된다.
# ---------------------------------------------------------------
C_INK = "#1F2933"
C_GRAY = "#9AA5B1"      # 변동비
C_GRAY_D = "#5A6875"    # 고정비 · 맨데이
C_BLUE = "#4A87B8"      # 1단 공헌이익
C_BLUE_D = "#17527D"    # 2단 진짜 영업이익
C_RED = "#B4342A"       # 경고
C_GRID = "#E4E9ED"

# 한글이 깨지지 않도록 차트 폰트를 OS별 한글 서체로 지정한다.
KO_FONT = "Malgun Gothic, Apple SD Gothic Neo, Noto Sans KR, NanumGothic, sans-serif"
EOK = 100_000_000


@st.cache_resource
def get_conn():
    return db_module.open_app_db()


def won(value) -> str:
    return f"{int(value or 0):,}"


def won_kr(value) -> str:
    """금액 표기 — 천 단위 콤마 + '원'."""
    return f"{int(round(value or 0)):,}원"


def pct(value) -> str:
    return f"{value * 100:.1f}%"


def eok(value) -> str:
    """억 단위 요약 표기.

    `1,860,320,000원` 은 열 자리라 한눈에 안 읽힌다. 30초 안에 파악하는 것이
    목적인 자리에서는 `18.6억원` 이 훨씬 빠르다. 정확한 원 단위 금액은
    바로 아래에 작게 병기하므로 정보가 사라지지는 않는다.

    1억 미만이면 억 표기가 오히려 부정확해지므로 원 단위 그대로 둔다.
    """
    n = int(round(value or 0))
    if abs(n) < EOK:
        return f"{n:,}원"
    return f"{n / EOK:.1f}억원"


def money_row(values: list[int]) -> list[str]:
    """한 줄에 나란히 놓을 금액들을 **같은 단위로** 맞춘다.

    `5,839만원 · 6,821만원 · 8.1억원` 처럼 단위가 섞이면 어느 쪽이 큰지
    즉시 안 읽힌다 — 나란히 놓는 이유가 크기 비교인데 그게 안 되면 의미가 없다.
    줄 안의 최대값 기준으로 단위를 한 번만 정한다.
    """
    if any(abs(int(v or 0)) >= EOK for v in values):
        return [f"{int(v or 0) / EOK:.1f}억원" for v in values]
    return [won_kr(v) for v in values]


def _hero_exact(value, rate) -> str:
    """히어로 아래 보조 줄.

    히어로가 억 단위로 요약됐을 때만 정확한 원 단위 금액을 병기한다.
    1억 미만이면 히어로 자체가 이미 원 단위라 같은 숫자를 두 번 찍게 된다.
    """
    if abs(int(value or 0)) >= EOK:
        return f"{won_kr(value)} · 이익률 {pct(rate)}"
    return f"이익률 {pct(rate)}"


def style_chart(fig: go.Figure, height: int = 340) -> go.Figure:
    """차트 공통 서식. 한글 폰트·여백·격자를 여기서 한 번에 잡는다."""
    fig.update_layout(
        height=height,
        font=dict(family=KO_FONT, size=13, color=C_INK),
        plot_bgcolor="white",
        paper_bgcolor="white",
        # 여백을 좁히면 x축 라벨(현장명·단계명)과 y축 눈금이 잘려 나간다.
        # 어느 현장인지 안 보이는 차트는 쓸모가 없으므로 넉넉히 준다.
        margin=dict(l=78, r=44, t=64, b=92),
        hoverlabel=dict(font_family=KO_FONT, font_size=13),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None),
        bargap=0.28,
    )
    fig.update_xaxes(showgrid=False, linecolor=C_GRID, ticks="outside", tickcolor=C_GRID)
    fig.update_yaxes(gridcolor=C_GRID, zerolinecolor=C_GRID, showline=False)
    return fig


# ===============================================================
# 데이터 페이지
# ===============================================================


def _upload_section(conn) -> None:
    st.subheader("1. 엑셀 업로드")
    st.caption(
        "매입·경비·매출 원장을 올립니다. 컬럼명이 파일마다 달라도 "
        "`src/rules.py`의 별칭 테이블이 흡수합니다."
    )

    uploaded = st.file_uploader(
        "엑셀 파일 (여러 개 선택 가능)",
        type=["xlsx", "xls"],
        accept_multiple_files=True,
    )
    if not uploaded:
        return

    for upload in uploaded:
        with st.expander(f"📄 {upload.name}", expanded=True):
            _preview_and_save(conn, upload)


def _preview_and_save(conn, upload) -> None:
    try:
        raw = pd.read_excel(upload)
    except Exception as exc:  # noqa: BLE001 - 사용자에게 그대로 보여준다
        st.error(f"엑셀을 읽지 못했습니다: {exc}")
        return

    st.write(f"원본 {len(raw)}행 · 컬럼: {', '.join(map(str, raw.columns))}")

    guessed = ingest.guess_tx_type(upload.name)
    if guessed is None:
        st.warning("파일명으로 거래유형을 판단하지 못했습니다. 직접 골라주세요.")
    tx_type = st.selectbox(
        "거래유형",
        TX_TYPES,
        index=TX_TYPES.index(guessed) if guessed else 0,
        key=f"txtype_{upload.name}",
    )

    # 컬럼 매핑 결과를 먼저 보여준다 — 잘못 붙었는지 눈으로 확인하라고
    mapping = ingest.resolve_columns(raw)
    st.write("**컬럼 매핑**")
    st.dataframe(
        pd.DataFrame(
            [{"표준 컬럼": k, "엑셀 컬럼": v} for k, v in mapping.items()]
        ),
        hide_index=True,
        width="stretch",
    )
    unmapped = [c for c in raw.columns if c not in mapping.values()]
    if unmapped:
        st.caption(f"사용하지 않는 컬럼: {', '.join(map(str, unmapped))}")

    try:
        normalized = ingest.normalize(raw, upload.name, tx_type)
    except ValueError as exc:
        st.error(str(exc))
        return

    classified = classify_dataframe(normalized)

    n_unclass = int((classified["account"] == UNCLASSIFIED).sum())
    n_dup = int(classified["is_duplicate_suspect"].sum())
    dropped = len(raw) - len(normalized)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("적재 대상", f"{len(classified)}건")
    c2.metric("미분류", f"{n_unclass}건",
              f"{n_unclass / len(classified) * 100:.1f}%" if len(classified) else "0%")
    c3.metric("중복 의심", f"{n_dup}건")
    c4.metric("날짜 불량(제외)", f"{dropped}건")

    st.write("**미리보기 (상위 20행)**")
    st.dataframe(classified.head(20), hide_index=True, width="stretch")

    if st.button("DB에 저장", key=f"save_{upload.name}", type="primary"):
        n = ingest.load_transactions(conn, classified)
        st.success(f"{n}건 저장했습니다.")
        st.cache_data.clear()


def _status_section(conn) -> None:
    st.subheader("2. 적재 현황")

    total = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
    if total == 0:
        st.info("아직 적재된 거래가 없습니다. 위에서 엑셀을 업로드하세요.")
        return

    stats = classification_stats(conn)
    unmapped = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE project_id IS NULL"
    ).fetchone()["n"]
    dup = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE is_duplicate_suspect = 1"
    ).fetchone()["n"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("전체 거래", f"{total:,}건")
    c2.metric("미분류", f"{stats['unclassified']}건", f"{stats['unclassified_pct']:.1f}%")
    c3.metric("공통비(프로젝트 미귀속)", f"{unmapped}건")
    c4.metric("중복 의심", f"{dup}건")

    if stats["unclassified_pct"] > 20:
        st.error(
            f"미분류가 {stats['unclassified_pct']:.1f}% 입니다. "
            "`src/rules.py`의 규칙을 보강하거나 아래에서 직접 지정하세요."
        )

    by_account = pd.read_sql_query(
        "SELECT account AS 계정과목, cost_behavior AS 원가행태, "
        "       COUNT(*) AS 건수, SUM(amount) AS 금액 "
        "FROM transactions GROUP BY account, cost_behavior ORDER BY 금액 DESC",
        conn,
    )
    st.write("**계정과목별 분포**")
    st.dataframe(by_account, hide_index=True, width="stretch")

    ambiguous = by_account[by_account["계정과목"].isin(AMBIGUOUS_ACCOUNTS)]
    if not ambiguous.empty:
        st.caption(
            "⚠ 변동/고정 판단이 갈릴 수 있는 계정: "
            + ", ".join(ambiguous["계정과목"])
            + " — 대표·경리와 합의 후 `src/rules.py`의 COST_BEHAVIOR 를 확정하세요."
        )


def _bulk_assign(conn, df: pd.DataFrame) -> None:
    """같은 적요가 수십 건씩 쌓인 경우를 한 번에 처리한다.

    '정산 차액' 처럼 한 가지 적요가 반복되면 한 건씩 고치다 빠뜨리기 쉽고,
    빠뜨린 행은 '해당없음' 으로 남아 원가에서 조용히 빠진다.
    """
    with st.expander(f"적요로 일괄 지정 ({len(df)}건 중 선택)"):
        descriptions = sorted({d for d in df["적요"].dropna() if str(d).strip()})
        if not descriptions:
            st.caption("적요가 비어 있어 일괄 지정할 대상이 없습니다.")
            return

        target = st.selectbox("적요", descriptions, key="bulk_desc")
        hit = df[df["적요"] == target]
        st.caption(f"**{target}** — {len(hit)}건 · {won(hit['금액'].sum())}")

        c1, c2 = st.columns(2)
        account = c1.selectbox("계정과목", ACCOUNT_CHOICES, key="bulk_acct")
        behavior = c2.selectbox("원가행태", BEHAVIOR_CHOICES, key="bulk_behav")

        if behavior == "변동" and hit["프로젝트"].isna().all():
            st.warning(
                "이 건들은 현장이 비어 있습니다. 변동으로 지정하면 변동비 합계가 "
                "현장별로 묶이는 탓에 어느 현장 원가에도 안 잡혀, 미분류로 두는 것과 "
                "결과가 같아집니다."
            )

        if st.button(f"{len(hit)}건 일괄 지정", type="primary"):
            n = set_override_bulk(
                conn,
                [int(i) for i in hit["id"]],
                account=account,
                cost_behavior=behavior,
            )
            st.success(f"{n}건을 {account}/{behavior} 로 지정했습니다. (재분류에도 보존)")
            st.rerun()


def _edit_section(conn) -> None:
    st.subheader("3. 분류 수정")
    st.caption(
        "여기서 고친 값은 `is_manual_override=1`로 저장되어, "
        "재업로드하거나 규칙을 바꿔 재분류해도 덮어쓰이지 않습니다."
    )

    filter_choice = st.radio(
        "대상",
        ["미분류만", "현장 미귀속 변동비", "중복 의심만", "전체"],
        horizontal=True,
    )
    where = {
        "미분류만": "WHERE t.account = '미분류'",
        # 분류는 됐지만 현장이 비어 원가에서 빠지는 행 — 미분류만큼 조용히 샌다
        "현장 미귀속 변동비": "WHERE t.cost_behavior = '변동' AND t.project_id IS NULL",
        "중복 의심만": "WHERE t.is_duplicate_suspect = 1",
        "전체": "",
    }[filter_choice]

    df = pd.read_sql_query(
        f"""
        SELECT t.id, t.date AS 일자, p.name AS 프로젝트, t.vendor AS 거래처,
               t.description AS 적요, t.tx_type AS 거래유형, t.amount AS 금액,
               t.account AS 계정과목, t.cost_behavior AS 원가행태,
               t.is_manual_override AS 수정됨, t.is_duplicate_suspect AS 중복의심,
               t.source_file AS 원본파일
        FROM transactions t
        LEFT JOIN projects p ON p.id = t.project_id
        {where}
        ORDER BY t.amount DESC
        LIMIT 300
        """,
        conn,
    )

    if df.empty:
        st.success("해당하는 건이 없습니다.")
        return

    st.write(f"{len(df)}건 (금액 큰 순 · 최대 300건)")

    _bulk_assign(conn, df)

    edited = st.data_editor(
        df,
        hide_index=True,
        width="stretch",
        disabled=[
            "id", "일자", "프로젝트", "거래처", "적요", "거래유형",
            "금액", "수정됨", "중복의심", "원본파일",
        ],
        column_config={
            "계정과목": st.column_config.SelectboxColumn(options=ACCOUNT_CHOICES),
            "원가행태": st.column_config.SelectboxColumn(options=BEHAVIOR_CHOICES),
            "금액": st.column_config.NumberColumn(format="%d"),
        },
        key=f"editor_{filter_choice}",
    )

    if st.button("수정사항 저장", type="primary"):
        changed = 0
        for orig, new in zip(df.to_dict("records"), edited.to_dict("records")):
            if orig["계정과목"] != new["계정과목"] or orig["원가행태"] != new["원가행태"]:
                set_override(
                    conn,
                    int(orig["id"]),
                    account=new["계정과목"],
                    cost_behavior=new["원가행태"],
                )
                changed += 1
        if changed:
            st.success(f"{changed}건 수정했습니다. (재분류에도 보존됩니다)")
            st.rerun()
        else:
            st.info("변경된 내용이 없습니다.")


def _maintenance_section(conn) -> None:
    with st.expander("유지보수"):
        st.caption(
            "`src/rules.py`의 규칙을 고친 뒤 재분류를 돌리세요. "
            "사람이 수정한 행은 건너뜁니다."
        )
        if st.button("규칙으로 전체 재분류"):
            n = reclassify_all(conn)
            st.success(f"{n}건 재분류했습니다. (수동 수정 행은 보존)")
            st.rerun()

        st.divider()
        st.caption("프로젝트 마스터 / 맨데이 엑셀은 아래로 올립니다.")
        master = st.file_uploader("프로젝트 계약현황", type=["xlsx"], key="up_proj")
        if master and st.button("프로젝트 마스터 반영"):
            n = ingest.load_projects(conn, pd.read_excel(master))
            st.success(f"프로젝트 {n}건 반영했습니다.")

        md = st.file_uploader("설계 맨데이 투입내역", type=["xlsx"], key="up_md")
        if md and st.button("맨데이 반영"):
            n = ingest.load_mandays(conn, pd.read_excel(md))
            st.success(f"맨데이 {n}건 적재했습니다.")


def _quality_warnings(conn) -> None:
    """원가에서 조용히 빠지는 금액을 알린다.

    브리핑이 아니라 여기 두는 이유: 둘 다 '데이터를 고쳐야 해결되는' 문제라
    조치할 수 있는 화면에 있어야 한다. 브리핑에 노란 경고를 띄우면 정작 봐야 할
    손익 경고와 경쟁하고, 대표가 할 수 있는 일도 없다.
    """
    summary = report.company_summary(conn)

    if summary["미분류건수"]:
        st.warning(
            f"**미분류 {summary['미분류건수']}건 · {won_kr(summary['미분류금액'])}** "
            f"(전체의 {summary['미분류비율']:.1f}%) — 변동비에도 고정비에도 들어가 있지 "
            "않습니다. 분류하면 진짜 영업이익은 그만큼 더 줄어듭니다. "
            "아래 **분류 수정**에서 `미분류만` 필터로 처리하세요."
        )

    if summary["미귀속변동비건수"]:
        st.warning(
            f"**현장 미귀속 변동비 {summary['미귀속변동비건수']}건 · "
            f"{won_kr(summary['미귀속변동비금액'])}** — 변동으로 분류됐지만 현장이 "
            "비어 있습니다. 변동비 합계는 현장별로 묶이므로 이 금액은 어느 현장 "
            "원가에도 안 잡힙니다 (미분류와 결과가 같습니다). "
            "아래 `현장 미귀속 변동비` 필터에서 현장을 채우거나 고정으로 다시 판단하세요."
        )


def render_data() -> None:
    st.header("데이터")
    conn = get_conn()
    _quality_warnings(conn)
    _upload_section(conn)
    st.divider()
    _status_section(conn)
    st.divider()
    _edit_section(conn)
    _maintenance_section(conn)


# ===============================================================
# 페이지 1 — 아침 브리핑
#   대표이사가 아침에 30초 안에 상황을 파악하는 화면.
#   위에서부터 '지금 문제 → 전체 그림 → 곧 나갈 돈' 순으로 읽힌다.
# ===============================================================


def _no_data_guide() -> None:
    st.info(
        "적재된 거래가 없습니다. 왼쪽 메뉴의 **데이터** 에서 엑셀을 올리거나, "
        "터미널에서 `python scripts/load_sample_data.py` 로 샘플을 넣어 보세요."
    )


def _inject_css() -> None:
    """브리핑 화면 전용 서식.

    목적이 '아침에 30초 안에 파악'이므로, 읽는 순서가 크기로 정해지게 한다.
    히어로 숫자 하나 → 보조 수치 → 근거. 테두리와 색은 최소로 둔다.
    """
    st.markdown(
        f"""
        <style>
        .hero {{ margin: 0.5rem 0 0.25rem; }}
        .hero__label {{
            font-size: 0.95rem; color: {C_GRAY_D}; margin-bottom: 0.35rem;
        }}
        .hero__value {{
            /* 화면이 이끄는 단 하나의 숫자. 폭 정렬(tabular)은 표에서만 쓴다 */
            font-size: 3.4rem; font-weight: 700; line-height: 1.1;
            color: {C_BLUE_D}; letter-spacing: -0.02em;
        }}
        .hero__exact {{
            font-size: 0.9rem; color: {C_GRAY_D}; margin-top: 0.3rem;
        }}
        .hero__sub {{
            font-size: 1.05rem; color: {C_INK}; margin-top: 0.6rem; line-height: 1.6;
        }}
        .statrow {{ display: flex; gap: 2.75rem; flex-wrap: wrap; margin: 1.1rem 0 0.25rem; }}
        .stat__label {{ font-size: 0.85rem; color: {C_GRAY_D}; margin-bottom: 0.2rem; }}
        .stat__value {{ font-size: 1.45rem; font-weight: 600; color: {C_INK}; }}
        .stat__note  {{ font-size: 0.8rem; color: {C_GRAY_D}; margin-top: 0.15rem; }}
        .quiet {{ font-size: 0.85rem; color: {C_GRAY_D}; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _briefing_hero(summary: dict, gap: dict) -> None:
    """히어로 = '회사에 남은 돈' 하나.

    이 화면을 보는 사람은 시간을 아까워하고, 회계 용어를 새로 배울 생각이 없다.
    그래서 두 가지를 지킨다.

    1. 라벨을 평범한 말로 쓴다. '진짜 영업이익'은 우리끼리 쓰는 용어지
       대표가 배워야 할 말이 아니다. 정확한 회계 명칭은 아래 각주로 내린다.
    2. 화면이 답을 말한다. 숫자 여러 개를 늘어놓고 '맨데이가 제일 크네'를
       대표가 직접 알아채게 하지 않는다. 가장 큰 원인을 문장으로 적는다.
    """
    # 차감 항목 중 최대 항목 = 이번 달 '왜 안 남았는지'의 답
    deductions = [
        ("설계 인건비", gap["맨데이인건비"]),
        ("고정비", gap["고정비"] + gap["직접고정비"]),
        ("이자", gap["이자"]),
    ]
    top_label, top_value = max(deductions, key=lambda x: x[1])
    share = top_value / gap["차감합계"] * 100 if gap["차감합계"] else 0

    cause = (
        f"가장 큰 원인은 <b>{top_label} {eok(top_value)}</b> 입니다"
        f" (빠져나간 돈의 {share:.0f}%)."
        if gap["차감합계"]
        else "차감된 비용이 없습니다."
    )

    st.markdown(
        f"""
        <div class="hero">
          <div class="hero__label">{summary['기준월표기']} 기준 · 회사에 남은 돈</div>
          <div class="hero__value">{eok(summary['진짜영업이익'])}</div>
          <div class="hero__exact">{_hero_exact(summary['진짜영업이익'], summary['진짜이익률'])}</div>
          <div class="hero__sub">
            현장에서 <b>{eok(summary['공헌이익'])}</b> 을 벌었지만
            <b>{eok(gap['차감합계'])}</b> 이 빠져나갔습니다.<br>{cause}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 보조 수치는 3개까지만. 차이의 90%를 설명하는 두 항목과, 현금 감각 하나.
    # 이자(6%)·현장 직접 고정비(3.5%)는 접힌 상세로 내렸다 — 30초 화면에서
    # 3%짜리 항목은 노이즈다.
    items = [
        ("설계 인건비", gap["맨데이인건비"], "ERP에 안 잡히던 원가"),
        ("고정비 · 이자", gap["고정비"] + gap["직접고정비"] + gap["이자"],
         "임차료 · 관리직 급여 · 대출이자"),
        ("다음 달 나갈 돈", summary["다음달지출예정"], "수주가 없어도 나가는 돈"),
    ]
    shown = money_row([v for _, v, _ in items])
    cells = "".join(
        f'<div><div class="stat__label">{label}</div>'
        f'<div class="stat__value">{amount}</div>'
        f'<div class="stat__note">{note}</div></div>'
        for (label, _, note), amount in zip(items, shown)
    )
    st.markdown(f'<div class="statrow">{cells}</div>', unsafe_allow_html=True)

    # 회계 기준으로 '영업이익'은 이자비용을 빼지 않으므로(이자는 영업외비용),
    # 이자를 뺀 이 값은 영업이익보다 세전이익에 가깝다. 법인세는 발생이 당기,
    # 납부가 이듬해라 이 금액이 곧 쓸 수 있는 현금도 아니다.
    st.markdown(
        '<div class="quiet">회계상 명칭은 <b>진짜 영업이익(세전)</b> 입니다. '
        "이자비용은 뺐고 법인세는 빼지 않았습니다 — 법인세는 올해 발생해도 "
        "납부가 이듬해라, 이 금액이 곧 지금 쓸 수 있는 현금은 아닙니다.</div>",
        unsafe_allow_html=True,
    )


def _briefing_alerts(conn) -> None:
    risk = report.risk_projects(conn)
    if risk.empty:
        st.success(
            f"진짜이익률이 {int(report.RISK_THRESHOLD * 100)}% 아래로 떨어진 현장이 없습니다."
        )
        return

    losing = risk[risk["적자"]]
    head = f"주의 — {len(risk)}개 현장이 기준 미달"
    if not losing.empty:
        head += f" (그중 {len(losing)}개는 적자)"
    st.error(f"**{head}**\n\n" + "\n".join(
        f"- **{r['프로젝트']}** — 공헌이익률 {pct(r['공헌이익률'])} 로는 남는 것처럼 보이지만, "
        f"고정비·맨데이를 얹으면 **진짜이익률 {pct(r['진짜이익률'])}** "
        f"({won_kr(r['진짜영업이익'])})"
        for _, r in risk.iterrows()
    ))


def _margin_comparison_chart(table: pd.DataFrame) -> go.Figure:
    """현장별 공헌이익률 → 진짜이익률 낙폭 (덤벨).

    막대 2개를 나란히 세우면 현장 6곳에 막대 12개가 서고, 정작 요점인 '낙폭'은
    두 막대 사이의 빈 공간이라 눈에 안 잡힌다. 덤벨은 그 낙폭을 선 하나로
    직접 그리므로 잉크가 줄면서 메시지는 더 선명해진다.
    가로 배치라 현장명이 기울지 않고 그대로 읽힌다.
    """
    df = table.sort_values("진짜이익률", ascending=True)
    names = [n if len(n) <= 16 else n[:15] + "…" for n in df["프로젝트"]]
    cm = (df["공헌이익률"] * 100).round(1)
    op = (df["진짜이익률"] * 100).round(1)

    fig = go.Figure()

    # ① 낙폭 = 두 점을 잇는 선. 이 길이가 '새는 돈'이다.
    for y, (c, o) in enumerate(zip(cm, op)):
        fig.add_shape(
            type="line", x0=o, x1=c, y0=y, y1=y,
            line=dict(color=C_GRAY, width=3),
        )

    # ② 1단(옅은 파랑) → ③ 2단(진한 파랑). 같은 색 두 단계 = 같은 지표의 전후.
    fig.add_scatter(
        x=cm, y=names, mode="markers", name="1단 공헌이익률",
        marker=dict(size=13, color=C_BLUE, line=dict(color="white", width=2)),
        hovertemplate="<b>%{y}</b><br>공헌이익률 %{x:.1f}%<extra></extra>",
    )
    fig.add_scatter(
        x=op, y=names, mode="markers", name="2단 진짜이익률",
        marker=dict(size=13, color=C_BLUE_D, line=dict(color="white", width=2)),
        hovertemplate="<b>%{y}</b><br>진짜이익률 %{x:.1f}%<extra></extra>",
    )

    # 낙폭 수치는 선 오른쪽 끝 바깥에 한 번만 적는다 (점마다 숫자를 붙이지 않는다)
    for y, (c, o) in enumerate(zip(cm, op)):
        fig.add_annotation(
            x=c, y=y, text=f"−{c - o:.1f}%p", showarrow=False,
            xanchor="left", xshift=12, font=dict(size=11, color=C_GRAY_D),
        )

    fig.add_vline(
        x=report.RISK_THRESHOLD * 100, line_dash="dot", line_color=C_RED, line_width=1,
        annotation_text=f"경고선 {int(report.RISK_THRESHOLD * 100)}%",
        annotation_position="bottom right",
        annotation_font_color=C_RED, annotation_font_size=11,
    )
    fig.update_layout(title="현장별 이익률 낙폭 — 선 길이가 빠져나간 폭")
    fig.update_xaxes(range=[min(0, op.min() - 6), cm.max() + 16], ticksuffix="%")
    fig.update_yaxes(showgrid=False)

    row_h, top, bottom = 46, 78, 54
    fig = style_chart(fig, top + bottom + row_h * len(names))
    # 공통 여백은 세로 막대 기준(현장명이 x축에 기울어 들어감)이라 가로 차트에는
    # 안 맞는다. 현장명이 y축 라벨로 들어가므로 왼쪽을 넓히고, 기운 라벨이
    # 없으니 아래는 줄인다.
    fig.update_layout(margin=dict(l=185, r=44, t=top, b=bottom))
    return fig


def render_briefing() -> None:
    st.header("아침 브리핑")
    conn = get_conn()

    if conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"] == 0:
        _no_data_guide()
        return

    _inject_css()
    summary = report.company_summary(conn)
    gap = report.profit_gap(conn)

    _briefing_hero(summary, gap)
    st.divider()
    _briefing_alerts(conn)

    table = report.profit_table(conn)
    st.plotly_chart(_margin_comparison_chart(table), width="stretch")

    # 근거 자료는 접어 둔다. 30초 화면에서 9칼럼 표는 방해가 되고,
    # 필요한 사람은 한 번만 누르면 되므로 정보를 지우는 것이 아니다.
    with st.expander("현장별 상세 (매출 · 변동비 · 고정비 · 맨데이)"):
        st.dataframe(
            table.assign(
                공헌이익률=lambda d: d["공헌이익률"].map(pct),
                진짜이익률=lambda d: d["진짜이익률"].map(pct),
            ),
            hide_index=True,
            width="stretch",
            column_config={
                col: st.column_config.NumberColumn(format="%,d")
                for col in ("매출", "변동비", "공헌이익", "배부고정비", "맨데이", "진짜영업이익")
            },
        )

    upcoming = report.upcoming_fixed_costs(conn, months=3)
    with st.expander(f"향후 3개월 지출 예정 — 합계 {won_kr(upcoming['합계'].sum())}"):
        st.caption("수주가 없어도 나가는 돈 (고정비 + 차입금 이자)")
        st.dataframe(
            upcoming,
            hide_index=True,
            width="stretch",
            column_config={
                c: st.column_config.NumberColumn(format="%,d")
                for c in ("고정비", "이자", "합계")
            },
        )
        st.caption(f"BEP 손익분기 매출 {won_kr(summary['BEP매출'])}")

    # 데이터 품질 문제는 '데이터' 페이지에서 조치한다. 브리핑에 노란 경고를
    # 두 개 띄우면 정작 봐야 할 손익 경고와 경쟁한다. 여기서는 한 줄로만 알린다.
    issues = []
    if summary["미분류건수"]:
        issues.append(f"미분류 {summary['미분류건수']}건")
    if summary["미귀속변동비건수"]:
        issues.append(f"현장 미귀속 변동비 {summary['미귀속변동비건수']}건")
    if issues:
        st.markdown(
            f'<div class="quiet">확인 필요 · {" · ".join(issues)} '
            "— 왼쪽 <b>데이터</b> 페이지에서 처리하세요</div>",
            unsafe_allow_html=True,
        )


# ===============================================================
# 페이지 2 — 프로젝트 상세
# ===============================================================


def _waterfall_chart(bridge: pd.DataFrame, title: str) -> go.Figure:
    """매출 → 변동비 → 공헌이익 → 고정비 → 맨데이 → 진짜 영업이익.

    맨데이 막대만 빗금으로 채운다. ERP에 안 잡히던 원가라는 것이
    이 차트의 요점이므로 색을 늘리지 않고 질감으로 구분한다.
    """
    colors, patterns = [], []
    for _, r in bridge.iterrows():
        if r["단계"] == "매출":
            colors.append(C_GRAY_D); patterns.append("")
        elif r["단계"] == "공헌이익":
            colors.append(C_BLUE); patterns.append("")
        elif r["단계"] == "진짜 영업이익":
            colors.append(C_BLUE_D); patterns.append("")
        elif r["단계"] == "맨데이 인건비":
            colors.append(C_GRAY_D); patterns.append("/")
        else:
            colors.append(C_GRAY); patterns.append("")

    labels = [("−" if v < 0 else "") + f"{abs(v):,}원" for v in bridge["금액"]]

    fig = go.Figure()
    fig.add_bar(
        x=bridge["단계"],
        y=(bridge["끝"] - bridge["시작"]) / EOK,
        base=bridge["시작"] / EOK,
        marker=dict(
            color=colors,
            pattern=dict(shape=patterns, fgcolor="white", size=6, solidity=0.35),
            line=dict(width=0),
        ),
        text=labels,
        # textposition='outside' 는 막대에 라벨이 바짝 붙는다. 낮은 위치의
        # 맨데이 막대에서 특히 답답해서, 라벨은 주석으로 그리고 yshift 로 띄운다.
        # (text 는 hovertemplate 이 쓰므로 남겨 두고 표시만 끈다)
        textposition="none",
        customdata=bridge[["단계"]].values,
        hovertemplate="<b>%{customdata[0]}</b><br>%{text}<extra></extra>",
        showlegend=False,
    )

    for i, (label, top) in enumerate(zip(labels, bridge["끝"])):
        fig.add_annotation(
            x=i, y=top / EOK, text=label, showarrow=False, yshift=14,
            font=dict(size=11.5, color=C_INK),
        )

    # 막대끼리 이어 주는 점선 — 끊겨 보이면 워터폴이 아니다
    for i in range(len(bridge) - 1):
        level = bridge.iloc[i]["끝"] if bridge.iloc[i]["유형"] == "합계" else bridge.iloc[i]["시작"]
        if bridge.iloc[i]["단계"] == "매출":
            level = bridge.iloc[i]["끝"]
        fig.add_shape(
            type="line", x0=i + 0.36, x1=i + 1 - 0.36,
            y0=level / EOK, y1=level / EOK,
            line=dict(color=C_GRAY, width=1, dash="dot"),
        )

    manday_idx = list(bridge["단계"]).index("맨데이 인건비")
    manday_row = bridge.iloc[manday_idx]
    if manday_row["금액"]:
        # 주석 자리 잡기가 까다로운 구간이다. 값 라벨은 막대 중앙 위에 있고,
        # 오른쪽에는 진짜 영업이익 막대와 그 라벨이 있다. 그래서
        #   ① 화살표는 막대 '오른쪽 끝'을 가리키고 (중앙 값 라벨을 피한다)
        #   ② 글상자는 위쪽 빈 공간에 띄운다 (오른쪽 라벨과 겹치지 않는다)
        fig.add_annotation(
            x=manday_idx + 0.3, y=(manday_row["끝"] + manday_row["시작"]) / 2 / EOK,
            text="맨데이 —<br>ERP에 없던 원가", showarrow=True,
            arrowhead=2, arrowsize=0.8, arrowwidth=1, arrowcolor=C_GRAY_D,
            ax=40, ay=-68, xanchor="left", align="left",
            bgcolor="rgba(255,255,255,0.9)",
            font=dict(size=11, color=C_GRAY_D),
        )

    fig.update_layout(title=title)
    # 최상단 라벨이 제목에 닿지 않도록 위쪽 여유를 준다
    fig.update_yaxes(tickformat=",.0f", ticksuffix="억",
                     range=[0, bridge["끝"].max() / EOK * 1.12])
    return style_chart(fig, 420)


def _manday_form(conn, project_id: int) -> None:
    st.subheader("맨데이 입력")
    st.caption("ERP에 원가로 안 잡히는 설계 인력 투입분. 등록하면 즉시 손익에 반영됩니다.")

    rates = masters.get_daily_rates(conn)
    roles = list(rates) + ["직접 입력"]

    with st.form("manday_form", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1.4])
        role = c1.selectbox("역할", roles)
        headcount = c2.number_input("인원", min_value=1, max_value=99, value=1, step=1)
        days = c3.number_input("일수", min_value=0.0, max_value=999.0, value=10.0, step=0.5,
                               help="0.5일 단위로 입력할 수 있습니다")
        default_rate = int(rates.get(role, 300_000))
        rate = c4.number_input("일단가(원)", min_value=0, value=default_rate, step=10_000)
        custom_role = st.text_input("역할 직접 입력", "",
                                    disabled=role != "직접 입력", placeholder="예: 특수잠수 감리")

        st.caption(f"예상 인건비 — {won_kr(headcount * days * rate)}")
        if st.form_submit_button("맨데이 등록", type="primary"):
            name = custom_role.strip() if role == "직접 입력" else role
            try:
                masters.add_manday(conn, project_id, name, headcount, days, rate)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success(f"{name} {headcount}명 × {days}일 등록했습니다.")
                st.rerun()

    rows = masters.list_mandays(conn, project_id)
    if not rows:
        st.info("등록된 맨데이가 없습니다. 이 현장은 설계 인건비가 원가에 안 잡혀 있습니다.")
        return

    df = pd.DataFrame(rows).rename(columns={
        "id": "ID", "role": "역할", "headcount": "인원",
        "days": "일수", "daily_rate": "일단가", "cost": "인건비",
    })
    st.dataframe(
        df, hide_index=True, width="stretch",
        column_config={
            c: st.column_config.NumberColumn(format="%,d") for c in ("일단가", "인건비")
        },
    )
    st.caption(f"합계 {won_kr(df['인건비'].sum())}")

    c1, c2 = st.columns([1, 4])
    target = c1.selectbox("삭제할 항목", df["ID"], format_func=lambda i: f"#{i}")
    if c2.button("선택 항목 삭제"):
        masters.delete_manday(conn, int(target))
        st.rerun()


def render_project_detail() -> None:
    """현장 하나를 파고드는 화면.

    브리핑과 같은 기준을 적용한다 — 히어로 하나, 평범한 말, 상세는 접기.
    맨데이 입력은 설계 팀장의 일이라 대표 화면에 펼쳐 둘 이유가 없다.
    """
    st.header("프로젝트 상세")
    conn = get_conn()

    projects = conn.execute("SELECT id, name FROM projects ORDER BY name").fetchall()
    if not projects:
        _no_data_guide()
        return

    _inject_css()
    names = {r["name"]: r["id"] for r in projects}
    picked = st.selectbox("현장", list(names))
    pid = names[picked]

    from src import profit as profit_module

    p = profit_module.compute_project_profit(conn, pid)

    # 이 현장에서 답해야 할 것도 하나다 — 여기서 얼마 남았나.
    st.markdown(
        f"""
        <div class="hero">
          <div class="hero__label">{picked} · 이 현장에서 남은 돈</div>
          <div class="hero__value">{eok(p.operating_profit)}</div>
          <div class="hero__exact">{_hero_exact(p.operating_profit, p.operating_profit_rate)}</div>
          <div class="hero__sub">
            매출 <b>{eok(p.revenue)}</b> 중 현장에서 <b>{eok(p.contribution_margin)}</b> 이 남았고,
            여기에 고정비·설계 인건비 <b>{eok(p.gap)}</b> 이 더 빠졌습니다.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    items = [
        ("설계 인건비", p.manday_cost, "이 현장에 투입된 인력"),
        ("고정비 몫", p.fixed_charge, "회사 고정비 중 이 현장 부담분"),
        ("변동비", p.variable_cost, "자재 · 외주 · 운반 등"),
    ]
    shown = money_row([v for _, v, _ in items])
    st.markdown(
        '<div class="statrow">'
        + "".join(
            f'<div><div class="stat__label">{label}</div>'
            f'<div class="stat__value">{amount}</div>'
            f'<div class="stat__note">{note}</div></div>'
            for (label, _, note), amount in zip(items, shown)
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    if p.operating_profit < 0:
        st.error("이 현장은 고정비와 설계 인건비를 반영하면 **적자**입니다.")
    elif p.revenue and p.operating_profit_rate < report.RISK_THRESHOLD:
        st.warning(
            f"이익률 {pct(p.operating_profit_rate)} — 경고선 "
            f"{int(report.RISK_THRESHOLD * 100)}% 아래입니다."
        )

    st.plotly_chart(
        _waterfall_chart(report.profit_bridge(conn, pid), f"{picked} — 매출에서 이익까지"),
        width="stretch",
    )

    # 아래는 파고들 때만 필요한 것들. 기본은 접어 둔다.
    accounts = report.project_accounts(conn, pid)
    with st.expander("거래 내역 보기"):
        f1, f2 = st.columns(2)
        picked_accounts = f1.multiselect("계정과목", accounts, default=[])
        picked_behaviors = f2.multiselect("원가행태", BEHAVIOR_CHOICES, default=[])
        tx = report.project_transactions(
            conn, pid, accounts=picked_accounts or None, behaviors=picked_behaviors or None
        )
        st.dataframe(
            tx, hide_index=True, width="stretch",
            column_config={"금액": st.column_config.NumberColumn(format="%,d")},
        )
        st.caption(f"{len(tx)}건 · 합계 {won_kr(tx['금액'].sum() if not tx.empty else 0)}")

    # 맨데이 입력은 설계 팀장의 일이다. 대표 화면에 폼을 펼쳐 두지 않는다.
    with st.expander("설계 인력 투입 입력  ⚙"):
        _manday_form(conn, pid)


# ===============================================================
# 페이지 3 — 설정
# ===============================================================


def _settings_loans(conn) -> None:
    st.subheader("차입금")
    st.caption("등록하면 월 이자가 곧바로 고정비 풀과 지출 예정표에 들어갑니다.")

    with st.form("loan_form", clear_on_submit=True):
        c1, c2, c3 = st.columns([2, 1.2, 1])
        name = c1.text_input("차입금 이름", placeholder="예: 기업은행 운전자금")
        principal = c2.number_input("원금(원)", min_value=0, value=100_000_000, step=10_000_000)
        rate_pct = c3.number_input("연이율(%)", min_value=0.0, max_value=30.0,
                                   value=5.0, step=0.1, format="%.2f")
        d1, d2 = st.columns(2)
        start = d1.date_input("실행일", value=None, format="YYYY-MM-DD")
        end = d2.date_input("만기일", value=None, format="YYYY-MM-DD")

        st.caption(
            f"월 이자 — {won_kr(finance.monthly_interest(principal, rate_pct / 100))}  ·  "
            f"30일 기준 일할 {won_kr(finance.daily_interest(principal, rate_pct / 100, 30))}"
        )
        if st.form_submit_button("차입금 등록", type="primary"):
            try:
                masters.add_loan(
                    conn, name, principal, rate_pct / 100,
                    start.isoformat() if start else None,
                    end.isoformat() if end else None,
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success(f"{name} 등록했습니다.")
                st.rerun()

    loans = masters.list_loans(conn)
    if not loans:
        st.info("등록된 차입금이 없습니다.")
        return

    df = pd.DataFrame(loans).rename(columns={
        "id": "ID", "name": "이름", "principal": "원금", "annual_rate": "연이율",
        "start_date": "실행일", "end_date": "만기일", "monthly_interest": "월이자",
    })
    df["연이율"] = (df["연이율"] * 100).map(lambda v: f"{v:.2f}%")
    edited = st.data_editor(
        df, hide_index=True, width="stretch", key="loan_editor",
        disabled=["ID", "월이자"],
        column_config={
            "원금": st.column_config.NumberColumn(format="%,d"),
            "월이자": st.column_config.NumberColumn(format="%,d"),
        },
    )
    st.caption(f"월 이자 합계 {won_kr(df['월이자'].sum())}")

    c1, c2 = st.columns([1, 4])
    if c1.button("수정 저장", key="loan_save"):
        changed = 0
        for before, after in zip(df.to_dict("records"), edited.to_dict("records")):
            fields = {}
            if before["이름"] != after["이름"]:
                fields["name"] = after["이름"]
            if before["원금"] != after["원금"]:
                fields["principal"] = int(after["원금"])
            if before["연이율"] != after["연이율"]:
                fields["annual_rate"] = float(str(after["연이율"]).rstrip("%")) / 100
            for key, col in (("start_date", "실행일"), ("end_date", "만기일")):
                if before[col] != after[col]:
                    fields[key] = str(after[col]) if after[col] else None
            if fields:
                masters.update_loan(conn, int(before["ID"]), **fields)
                changed += 1
        st.success(f"{changed}건 수정했습니다.") if changed else st.info("변경사항이 없습니다.")
        if changed:
            st.rerun()

    target = c2.selectbox("삭제할 차입금", df["ID"],
                          format_func=lambda i: f"#{i} {df.set_index('ID').loc[i, '이름']}",
                          key="loan_del")
    if c2.button("삭제", key="loan_del_btn"):
        masters.delete_loan(conn, int(target))
        st.rerun()


def _settings_fixed_costs(conn) -> None:
    st.subheader("월 고정비")
    st.caption(
        "임차료·관리직 급여처럼 수주가 없어도 매달 나가는 돈입니다. "
        "**경비 원장에 이미 들어 있는 항목은 넣지 마세요 — 이중계상됩니다.**"
    )

    with st.form("fc_form", clear_on_submit=True):
        c1, c2, c3 = st.columns([2, 1.2, 1.2])
        name = c1.text_input("항목명", placeholder="예: 관리직 급여")
        amount = c2.number_input("월 금액(원)", min_value=0, value=1_000_000, step=100_000)
        category = c3.text_input("분류", placeholder="예: 관리인건비")
        if st.form_submit_button("고정비 등록", type="primary"):
            try:
                masters.add_fixed_cost(conn, name, amount, category)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success(f"{name} 등록했습니다.")
                st.rerun()

    rows = masters.list_fixed_costs(conn)
    if not rows:
        st.info("등록된 고정비가 없습니다.")
        return

    df = pd.DataFrame(rows).rename(columns={
        "id": "ID", "name": "항목명", "monthly_amount": "월 금액", "category": "분류",
    })
    st.dataframe(df, hide_index=True, width="stretch",
                 column_config={"월 금액": st.column_config.NumberColumn(format="%,d")})
    st.caption(f"월 합계 {won_kr(df['월 금액'].sum())} · 연 {won_kr(df['월 금액'].sum() * 12)}")

    c1, c2 = st.columns([1, 4])
    target = c1.selectbox("삭제할 항목", df["ID"],
                          format_func=lambda i: f"#{i} {df.set_index('ID').loc[i, '항목명']}",
                          key="fc_del")
    if c2.button("삭제", key="fc_del_btn"):
        masters.delete_fixed_cost(conn, int(target))
        st.rerun()


def _settings_allocation(conn) -> None:
    st.subheader("고정비 배부기준")
    st.caption("공통 고정비를 현장에 나눠 붙이는 기준입니다. 어느 기준이든 배부 합계는 풀과 일치합니다.")

    bases = list(finance.ALL_BASES)
    current = db_module.get_setting(conn, "allocation_basis", finance.DEFAULT_BASIS)
    picked = st.radio(
        "배부기준", bases,
        index=bases.index(current) if current in bases else 0,
        format_func=lambda b: finance.BASIS_LABELS[b],
        horizontal=True,
    )
    if picked != current:
        db_module.set_setting(conn, "allocation_basis", picked)
        st.success(f"배부기준을 '{finance.BASIS_LABELS[picked]}' 로 바꿨습니다.")
        st.rerun()

    st.divider()
    st.subheader("이자 출처")
    st.caption(
        "같은 이자가 차입금 마스터와 경비 원장 양쪽에 있으면 이중계상됩니다. 한쪽만 고르세요."
    )
    sources = [finance.INTEREST_FROM_LOANS, finance.INTEREST_FROM_TRANSACTIONS]
    labels = {
        finance.INTEREST_FROM_LOANS: "차입금 마스터에서 자동계산 (원장의 이자비용 행 제외)",
        finance.INTEREST_FROM_TRANSACTIONS: "경비 원장의 이자비용 계정 사용 (차입금 미반영)",
    }
    now = finance.interest_source(conn)
    chosen = st.radio("이자 출처", sources, index=sources.index(now),
                      format_func=lambda s: labels[s])
    if chosen != now:
        db_module.set_setting(conn, "interest_source", chosen)
        st.rerun()

    breakdown = finance.fixed_cost_breakdown(conn)
    st.write(f"**현재 배부 대상 고정비 풀 — {won_kr(breakdown['합계'])}** ({breakdown['개월수']}개월)")
    st.dataframe(
        pd.DataFrame([
            {"항목": f"고정비 마스터 (월 {won(breakdown['월고정비월액'])}원 × {breakdown['개월수']}개월)",
             "금액": breakdown["월고정비"]},
            {"항목": "차입금 이자", "금액": breakdown["이자비용"]},
            {"항목": "원장의 공통 고정비", "금액": breakdown["공통고정비"]},
        ]),
        hide_index=True, width="stretch",
        column_config={"금액": st.column_config.NumberColumn(format="%,d")},
    )

    if conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]:
        alloc = finance.allocate_fixed_costs(conn)
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM projects")}
        st.dataframe(
            pd.DataFrame([{"프로젝트": names[k], "배부액": v} for k, v in alloc.items()]),
            hide_index=True, width="stretch",
            column_config={"배부액": st.column_config.NumberColumn(format="%,d")},
        )


def _settings_daily_rates(conn) -> None:
    st.subheader("표준일당")
    st.caption("맨데이 입력 화면의 역할별 기본 단가입니다.")

    rates = masters.get_daily_rates(conn)
    st.dataframe(
        pd.DataFrame([{"역할": k, "일단가": v} for k, v in rates.items()]),
        hide_index=True, width="stretch",
        column_config={"일단가": st.column_config.NumberColumn(format="%,d")},
    )

    with st.form("rate_form", clear_on_submit=True):
        c1, c2 = st.columns([2, 1.2])
        role = c1.text_input("역할", placeholder="예: 구조설계")
        rate = c2.number_input("일단가(원)", min_value=0, value=300_000, step=10_000)
        if st.form_submit_button("추가 / 수정", type="primary"):
            try:
                masters.set_daily_rate(conn, role, rate)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success(f"{role} {won_kr(rate)} 저장했습니다.")
                st.rerun()

    if rates:
        c1, c2 = st.columns([1, 4])
        target = c1.selectbox("삭제할 역할", list(rates), key="rate_del")
        if c2.button("삭제", key="rate_del_btn"):
            masters.delete_daily_rate(conn, target)
            st.rerun()


def render_settings() -> None:
    st.header("설정")
    conn = get_conn()
    tabs = st.tabs(["차입금", "월 고정비", "배부기준 · 이자", "표준일당"])
    with tabs[0]:
        _settings_loans(conn)
    with tabs[1]:
        _settings_fixed_costs(conn)
    with tabs[2]:
        _settings_allocation(conn)
    with tabs[3]:
        _settings_daily_rates(conn)


# ===============================================================
# 페이지 5 — 리포트
# ===============================================================


def render_report() -> None:
    """리포트는 '열면 이미 있는 것'이어야 한다.

    이전에는 버전 선택 → AI 체크 → 생성 버튼, 세 번을 조작해야 리포트가 나왔다.
    시간을 아까워하는 사람에게 기본값으로 만들 수 있는 것을 물어보는 건 낭비다.
    기본(대표용·코멘트 없음)을 즉시 띄우고, 다른 형식이 필요한 사람만 아래를 편다.
    """
    st.header("경영 리포트")
    conn = get_conn()

    if conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"] == 0:
        _no_data_guide()
        return

    summary = report.company_summary(conn)
    share = st.session_state.get("report_share", False)
    use_ai = st.session_state.get("report_ai", False)

    if use_ai:
        with st.spinner("AI 코멘트를 생성하는 중…"):
            md, has_comment = report.build_report_with_comment(conn, share=share)
        if not has_comment:
            st.warning(
                f"AI 코멘트를 가져오지 못했습니다 — {report.last_ai_error or '원인 미상'}\n\n"
                "아래 리포트 본문은 영향받지 않습니다."
            )
    else:
        md = report.build_report(conn, share=share)

    suffix = "_직원공유용" if share else ""
    head = st.columns([1, 2.2])
    head[0].download_button(
        "리포트 내려받기 (.md)",
        data=md.encode("utf-8"),
        file_name=f"경영리포트_{summary['기준월표기']}{suffix}.md",
        mime="text/markdown",
        type="primary",
        width="stretch",
    )
    head[1].markdown(
        f'<div class="quiet" style="padding-top:0.6rem">'
        f"{summary['기준월표기']} 기준"
        + (" · <b>직원 공유용</b> (금액 가림)" if share else "")
        + ("  · AI 코멘트 포함" if use_ai and has_comment else "")
        + "</div>",
        unsafe_allow_html=True,
    )

    with st.expander("다른 형식으로 만들기"):
        st.radio(
            "버전",
            [False, True],
            format_func=lambda v: "직원 공유용 (금액 가림)" if v else "대표용 (금액 포함)",
            key="report_share",
            horizontal=True,
        )
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        st.checkbox(
            "AI 경영 코멘트 3문단 추가",
            key="report_ai",
            disabled=not has_key,
            help=None if has_key else "이 기능을 쓰려면 API 키 설정이 필요합니다.",
        )
        if not has_key:
            st.caption("AI 코멘트는 별도 설정이 필요해 현재 꺼져 있습니다. 리포트 본문에는 영향이 없습니다.")

    st.divider()
    st.markdown(md)


PAGES = {
    "아침 브리핑": render_briefing,
    "프로젝트 상세": render_project_detail,
    "리포트": render_report,
    "설정": render_settings,
    "데이터": render_data,
}

# 메뉴를 역할로 나눈다.
#
# 대표가 실제로 쓰는 화면은 '아침 브리핑'과 '리포트' 둘뿐이다. 그런데 다섯 개가
# 같은 무게로 나열돼 있으면 열자마자 "내가 이걸 다 조작해야 하나" 로 읽힌다.
# 엑셀 업로드·분류 수정·차입금 등록은 경리와 실무자의 일이다.
# 기능을 없애는 게 아니라, 누구 화면인지를 메뉴에 드러낸다.
MENU_GROUPS = [
    ("보기", ["아침 브리핑", "리포트"]),
    ("관리", ["프로젝트 상세", "설정", "데이터"]),
]
_GROUP_OF = {page: group for group, pages in MENU_GROUPS for page in pages}
MENU_ORDER = [page for _, pages in MENU_GROUPS for page in pages]


def _menu_label(page: str) -> str:
    """'관리' 화면에만 표식을 달아 조회용과 구분한다."""
    return page if _GROUP_OF[page] == "보기" else f"{page}  ⚙"


def main() -> None:
    st.title("⚓ Mooring Point 경영지원 대시보드")

    st.sidebar.caption("**보기** — 대표님이 보시는 화면")
    choice = st.sidebar.radio(
        "메뉴",
        MENU_ORDER,
        format_func=_menu_label,
        label_visibility="collapsed",
    )
    st.sidebar.caption("⚙ 표시는 **관리용** — 경리·실무 담당자가 쓰는 화면입니다.")
    st.sidebar.divider()
    st.sidebar.caption(
        {
            "아침 브리핑": "오늘 상황 30초 요약",
            "리포트": "경영 리포트 생성 · 내려받기",
            "프로젝트 상세": "현장별 폭포차트 · 거래 · 맨데이 입력",
            "설정": "차입금 · 고정비 · 배부기준 · 표준일당",
            "데이터": "엑셀 업로드 · 분류 수정",
        }[choice]
    )
    PAGES[choice]()


if __name__ == "__main__":
    main()
