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
    """왜 안 남았는지 — 보조 수치는 3개까지만.

    이 화면을 보는 사람은 시간을 아까워한다. 차이의 90%를 설명하는 두 항목과
    현금 감각 하나면 충분하고, 3%짜리 항목은 30초 화면에서 노이즈다.
    """
    html = _briefing_html()
    for label in ("설계 인건비", "고정비 · 이자", "다음 달 나갈 돈"):
        assert label in html, label
    assert html.count('class="stat__label"') == 3


def test_briefing_states_the_biggest_cause():
    """화면이 답을 말한다.

    숫자를 늘어놓고 '맨데이가 제일 크네'를 대표가 직접 알아채게 하지 않는다.
    가장 큰 차감 항목을 문장으로 지목한다.
    """
    from src import db as db_module
    from src import report as report_module

    conn = db_module.open_app_db()
    gap = report_module.profit_gap(conn)
    if not gap["차감합계"]:
        pytest.skip("차감 항목이 없다")

    biggest = max(
        [("설계 인건비", gap["맨데이인건비"]),
         ("고정비", gap["고정비"] + gap["직접고정비"]),
         ("이자", gap["이자"])],
        key=lambda x: x[1],
    )[0]

    html = _briefing_html()
    assert "가장 큰 원인은" in html
    assert biggest in html


def test_briefing_hero_uses_eok_with_exact_amount():
    """히어로는 억 단위로 읽히고, 정확한 원 단위 금액을 함께 적는다.

    열 자리 숫자는 한눈에 안 들어온다. 그렇다고 정확한 금액을 지우면
    회계 화면으로서 못 쓰므로 바로 아래 작게 병기한다.
    """
    from src import db as db_module
    from src import report as report_module

    conn = db_module.open_app_db()
    profit_won = report_module.company_summary(conn)["진짜영업이익"]
    if abs(profit_won) < 100_000_000:
        pytest.skip("억 단위가 아니다")

    html = _briefing_html()
    assert "억원" in html
    assert f"{profit_won:,}원" in html


def test_briefing_avoids_jargon_in_hero_label():
    """히어로 라벨은 평범한 말로. 회계 명칭은 각주로 내린다.

    대표는 '진짜 영업이익'이라는 우리끼리 쓰는 용어를 배울 이유가 없다.
    """
    html = _briefing_html()
    label_line = html[html.index('class="hero__label"'):html.index('class="hero__value"')]
    assert "회사에 남은 돈" in label_line
    assert "영업이익" not in label_line          # 라벨에는 없고
    assert "진짜 영업이익(세전)" in html          # 각주에는 있다


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


def test_report_is_ready_without_any_clicks(monkeypatch):
    """리포트는 '열면 이미 있는 것'이다.

    이전에는 버전 선택 → AI 체크 → 생성 버튼, 세 번을 조작해야 리포트가
    나왔다. 시간을 아까워하는 사람에게 기본값으로 만들 수 있는 것을
    물어보는 건 낭비다.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    at = _run("리포트")
    assert not at.exception, at.exception

    assert any("현장이익과 최종이익의 차이" in m.value for m in at.markdown)
    assert len(at.download_button) == 1
    assert at.download_button[0].label.endswith("(.md)")


def test_report_options_are_folded_away(monkeypatch):
    """형식 선택은 접어 둔다 — 기본값이면 아무것도 고를 필요가 없다."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    at = _run("리포트")
    assert any("다른 형식" in e.label for e in at.expander), [e.label for e in at.expander]


def test_report_default_is_full_amount_version(monkeypatch):
    """기본은 대표용(금액 포함). 마스킹은 일부러 골라야 한다."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    at = _run("리포트")
    body = "\n".join(m.value for m in at.markdown)
    assert "(비공개)" not in body


def test_report_page_ai_option_disabled_without_key(monkeypatch):
    """키가 없으면 AI 코멘트 체크박스가 꺼진 채 비활성화된다."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    at = _run("리포트")
    ai = next(c for c in at.checkbox if "AI" in c.label)
    assert ai.value is False


def test_menu_separates_viewing_from_managing():
    """메뉴가 역할을 드러낸다.

    대표가 쓰는 화면은 브리핑·리포트 둘뿐인데 다섯 개가 같은 무게로 나열되면
    "내가 이걸 다 조작해야 하나" 로 읽힌다. 관리용에만 표식을 단다.
    """
    import app

    assert app.MENU_ORDER[:2] == ["아침 브리핑", "리포트"], app.MENU_ORDER
    assert app._menu_label("아침 브리핑") == "아침 브리핑"
    assert app._menu_label("리포트") == "리포트"
    for page in ("프로젝트 상세", "설정", "데이터"):
        assert app._menu_label(page).endswith("⚙"), page


def test_menu_covers_every_page():
    """그룹에서 빠진 페이지가 없다 — 새 화면을 넣고 등록을 잊으면 안 된다."""
    import app

    assert set(app.MENU_ORDER) == set(app.PAGES)


def test_briefing_labels_profit_as_pre_tax():
    """히어로 숫자가 세전임을 화면에 적는다 — 법인세는 이듬해 나간다."""
    html = _briefing_html()
    assert "세전" in html
    assert "법인세" in html


# ===============================================================
# 프로젝트 상세 — 브리핑과 같은 기준을 적용했는가
# ===============================================================


def test_detail_leads_with_one_number():
    """현장 화면도 히어로 하나. KPI 카드 4장을 늘어놓지 않는다."""
    at = _run("프로젝트 상세")
    html = "\n".join(m.value for m in at.markdown)
    assert html.count('class="hero__value"') == 1
    assert "이 현장에서 남은 돈" in html


def test_detail_avoids_jargon_labels():
    """'공헌이익 (1단)' 같은 우리끼리 쓰는 용어를 화면 라벨로 쓰지 않는다."""
    at = _run("프로젝트 상세")
    html = "\n".join(m.value for m in at.markdown)
    assert "설계 인건비" in html
    assert "공헌이익 (1단)" not in html
    assert "진짜 영업이익 (2단)" not in html


def test_detail_folds_away_drilldown_and_input():
    """거래 내역과 맨데이 입력은 접어 둔다.

    맨데이 입력은 설계 팀장의 일이라 대표 화면에 폼을 펼쳐 둘 이유가 없다.
    """
    at = _run("프로젝트 상세")
    labels = [e.label for e in at.expander]
    assert any("거래 내역" in l for l in labels), labels
    manday = [l for l in labels if "설계 인력" in l]
    assert manday, labels
    assert manday[0].endswith("⚙"), manday   # 관리용 표식


# ===============================================================
# 금액 표기 — 한 줄에서 단위가 섞이면 크기 비교가 안 된다
# ===============================================================


def test_money_row_uses_one_unit():
    """나란히 놓는 이유는 크기 비교다. 단위가 섞이면 그게 안 된다."""
    import app

    mixed = app.money_row([58_390_000, 68_215_995, 810_000_000])
    assert all(v.endswith("억원") for v in mixed), mixed

    small = app.money_row([58_390_000, 68_215_995])
    assert all(v.endswith("원") and "억" not in v for v in small), small


def test_hero_exact_line_avoids_duplicate_amount():
    """히어로가 이미 원 단위면 같은 숫자를 아래 줄에 또 찍지 않는다."""
    import app

    big = app._hero_exact(1_860_320_000, 0.225)
    assert "1,860,320,000원" in big and "22.5%" in big

    small = app._hero_exact(47_644_005, 0.049)
    assert "47,644,005원" not in small
    assert small == "이익률 4.9%"
