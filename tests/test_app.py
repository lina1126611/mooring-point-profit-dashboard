"""Streamlit 앱 스모크 테스트.

브라우저 없이 앱을 실제로 실행해서 각 페이지가 예외 없이 렌더링되는지 본다.
UI 코드에 계산식을 넣지 않기로 했으므로 여기서는 '터지지 않는가'만 검증한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"

PAGES = ["아침 브리핑", "프로젝트 상세", "리포트", "설정", "데이터"]


def _run(page: str) -> AppTest:
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    assert not at.exception, f"초기 렌더링 실패: {at.exception}"
    at.sidebar.radio[0].set_value(page).run()
    return at


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_without_exception(page):
    at = _run(page)
    assert not at.exception, f"'{page}' 페이지 렌더링 실패: {at.exception}"


def test_title_is_present():
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    assert not at.exception
    assert any("Mooring Point" in t.value for t in at.title)


def test_data_page_shows_edit_filter():
    """데이터 페이지의 분류 수정 필터가 렌더링된다."""
    at = _run("데이터")
    labels = [r.label for r in at.radio]
    assert "대상" in labels


# ===============================================================
# 아침 브리핑 — 대표가 30초 안에 봐야 하는 것들이 실제로 화면에 있는가
# ===============================================================


def _briefing_html() -> str:
    """브리핑 화면의 마크다운/HTML 을 한 덩어리로."""
    at = _run("아침 브리핑")
    return "\n".join(m.value for m in at.markdown)


def test_briefing_leads_with_operating_profit():
    """화면이 이끄는 숫자는 '진짜 영업이익' 하나다.

    KPI 카드 4장을 같은 크기로 늘어놓으면 무엇부터 봐야 하는지가 화면에
    표현되지 않는다. 히어로 1개 + 보조 수치 구조를 고정한다.
    """
    html = _briefing_html()
    # CSS 정의에도 같은 이름이 나오므로 실제 렌더된 요소만 센다
    assert html.count('class="hero__value"') == 1, "히어로 숫자는 화면당 하나"
    assert "진짜 영업이익" in html


def test_briefing_shows_gap_components():
    """왜 안 남았는지 — 고정비·이자·맨데이가 보조 수치로 함께 있다."""
    html = _briefing_html()
    for label in ("고정비 배부", "이자비용", "맨데이 인건비", "다음 달 나갈 돈"):
        assert label in html, label


def test_briefing_amounts_use_comma_and_won():
    """금액은 천 단위 콤마 + '원' 표기."""
    import re

    html = _briefing_html()
    amounts = re.findall(r"[\d,]+원", html)
    assert amounts, "금액이 하나도 없다"
    assert any("," in a for a in amounts), amounts


def test_briefing_hides_detail_behind_expanders():
    """9칼럼 표와 3개월 지출표는 접혀 있다 — 30초 화면의 방해 요소."""
    at = _run("아침 브리핑")
    labels = [e.label for e in at.expander]
    assert any("현장별 상세" in l for l in labels), labels
    assert any("향후 3개월" in l for l in labels), labels


def test_briefing_defers_data_quality_to_data_page():
    """미분류·미귀속 경고는 브리핑에서 노란 박스로 띄우지 않는다.

    둘 다 데이터를 고쳐야 풀리는 문제라 조치할 수 있는 화면에 있어야 하고,
    브리핑에 두면 정작 봐야 할 손익 경고와 경쟁한다.
    """
    at = _run("아침 브리핑")
    assert not at.warning, [w.value for w in at.warning]


def test_data_page_carries_quality_warnings():
    """옮긴 경고가 데이터 페이지에 실제로 있다 (샘플 DB에 미귀속 변동비가 있음)."""
    from src import db as db_module
    from src import report as report_module

    conn = db_module.open_app_db()
    summary = report_module.company_summary(conn)
    if not (summary["미분류건수"] or summary["미귀속변동비건수"]):
        pytest.skip("현재 DB에 데이터 품질 문제가 없다")

    at = _run("데이터")
    joined = "\n".join(w.value for w in at.warning)
    assert "미분류" in joined or "미귀속" in joined, joined


def test_briefing_has_comparison_chart():
    at = _run("아침 브리핑")
    assert len(at.get("plotly_chart")) == 1


def test_briefing_reports_risk_state():
    """경고 박스는 위험 현장이 있으면 error, 없으면 success 로 반드시 뜬다."""
    at = _run("아침 브리핑")
    assert at.error or at.success


def test_settings_page_has_four_tabs():
    at = _run("설정")
    assert len(at.tabs) >= 4


# ===============================================================
# 폭포차트 — 맨데이 구간이 눈에 걸리는가 (발표의 확인 포인트)
# ===============================================================


def test_waterfall_marks_manday_distinctly(conn):
    """맨데이 막대만 빗금 + 주석. 색을 늘리지 않고 구분한다."""
    import app
    from src import report
    from tests.test_finance import add_fixed_cost, add_project, add_tx
    from tests.test_profit import add_manday

    pid = add_project(conn, "A현장", "2026-01-01", "2026-01-31")
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=pid)
    add_tx(conn, "2026-01-11", 60_000_000, "매입", "변동", project_id=pid)
    add_fixed_cost(conn, "임차료", 10_000_000)
    add_manday(conn, pid, "구조설계", 3, 20, 250_000)

    bridge = report.profit_bridge(conn, pid)
    fig = app._waterfall_chart(bridge, "테스트")
    bar = fig.data[0]

    steps = list(bar.x)
    assert steps == ["매출", "변동비", "공헌이익", "고정비", "맨데이 인건비", "진짜 영업이익"]

    idx = steps.index("맨데이 인건비")
    assert list(bar.marker.pattern.shape)[idx] == "/"          # 유일한 빗금
    assert sum(1 for s in bar.marker.pattern.shape if s) == 1
    assert bar.y[idx] > 0                                       # 높이가 있다 = 보인다
    assert any("ERP" in a.text for a in fig.layout.annotations)  # 왜 여기 있는지 설명


def test_waterfall_bars_are_connected(conn):
    """막대가 이어져 보이도록 연결선을 그린다."""
    import app
    from src import report
    from tests.test_finance import add_project, add_tx

    pid = add_project(conn, "A현장", "2026-01-01", "2026-01-31")
    add_tx(conn, "2026-01-10", 100_000_000, "매출", "해당없음", project_id=pid)
    fig = app._waterfall_chart(report.profit_bridge(conn, pid), "테스트")
    assert len(fig.layout.shapes) == 5   # 막대 6개 사이의 연결선


# ===============================================================
# 리포트 페이지
# ===============================================================


def test_report_page_has_version_and_ai_controls():
    """버전 선택(대표용/직원공유용)과 AI 코멘트 옵션이 있다."""
    at = _run("리포트")
    labels = [r.label for r in at.radio]
    assert "버전" in labels
    assert any("AI" in c.label for c in at.checkbox)


def test_report_page_generates_and_offers_download(monkeypatch):
    """생성 버튼을 누르면 리포트 본문과 .md 다운로드 버튼이 나온다."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    at = _run("리포트")
    at.button[0].click().run()
    assert not at.exception, at.exception

    assert any("현장이익과 최종이익의 차이" in m.value for m in at.markdown)
    assert len(at.download_button) == 1
    assert at.download_button[0].label.endswith("(.md)")


def test_report_page_ai_option_disabled_without_key(monkeypatch):
    """키가 없으면 AI 코멘트 체크박스가 꺼진 채 비활성화된다."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    at = _run("리포트")
    ai = next(c for c in at.checkbox if "AI" in c.label)
    assert ai.value is False


def test_briefing_orders_deductions_by_size():
    """차감 항목은 금액 큰 순으로 놓인다.

    읽는 순서가 중요도와 어긋나면 히어로 아래 수치가 눈에 안 들어온다.
    (맨데이가 최대 누수인데 네 번째에 있던 것을 고친 자리)
    """
    from src import db as db_module
    from src import report as report_module

    conn = db_module.open_app_db()
    gap = report_module.profit_gap(conn)
    if not gap["차감합계"]:
        pytest.skip("차감 항목이 없다")

    html = _briefing_html()
    labels = {
        "맨데이 인건비": gap["맨데이인건비"],
        "고정비 배부": gap["고정비"],
        "이자비용": gap["이자"],
    }
    present = {k: v for k, v in labels.items() if k in html}
    positions = sorted(present, key=lambda k: html.index(k))
    amounts = [present[k] for k in positions]
    assert amounts == sorted(amounts, reverse=True), list(zip(positions, amounts))


def test_briefing_labels_profit_as_pre_tax():
    """히어로 숫자가 세전임을 화면에 적는다 — 법인세는 이듬해 나간다."""
    html = _briefing_html()
    assert "세전" in html
    assert "법인세" in html
