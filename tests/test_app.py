"""Streamlit 앱 스모크 테스트.

브라우저 없이 앱을 실제로 실행해서 각 페이지가 예외 없이 렌더링되는지 본다.
UI 코드에 계산식을 넣지 않기로 했으므로 여기서는 '터지지 않는가'만 검증한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"

PAGES = ["아침 브리핑", "프로젝트 상세", "설정", "데이터"]


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


def test_briefing_shows_four_kpi_cards():
    at = _run("아침 브리핑")
    labels = [m.label for m in at.metric]
    assert any("매출" in l for l in labels)
    assert any("공헌이익" in l for l in labels)
    assert any("진짜 영업이익" in l for l in labels)
    assert any("지출" in l for l in labels)


def test_briefing_amounts_use_comma_and_won():
    """금액은 천 단위 콤마 + '원' 표기."""
    at = _run("아침 브리핑")
    values = [m.value for m in at.metric]
    assert all(v.endswith("원") for v in values), values
    assert any("," in v for v in values)


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
