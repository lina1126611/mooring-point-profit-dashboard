"""로컬 LLM 보조 분류 테스트.

핵심은 **LLM 을 믿지 않는 것**이다. 목록 외 계정이나 깨진 JSON 이 통과하면
COST_BEHAVIOR 매핑에 없는 계정이 DB 에 들어가 금액이 조용히 사라진다.
그 경로를 전부 미분류로 되돌리는지 고정한다.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from src import local_llm
from src.rules import COST_BEHAVIOR, NOT_APPLICABLE, UNCLASSIFIED

ALLOWED = local_llm.allowed_accounts()


# ---------------------------------------------------------------
# 계정 목록
# ---------------------------------------------------------------


def test_allowed_accounts_excludes_not_applicable():
    """매출·미분류는 LLM 이 고를 수 있는 선택지가 아니다."""
    assert UNCLASSIFIED not in ALLOWED
    for account in ALLOWED:
        assert COST_BEHAVIOR[account] != NOT_APPLICABLE


def test_allowed_accounts_all_have_behavior():
    """제시한 계정은 전부 원가행태가 있어야 한다. 없으면 금액이 사라진다."""
    for account in ALLOWED:
        assert account in COST_BEHAVIOR


# ---------------------------------------------------------------
# parse_response — 정상
# ---------------------------------------------------------------


def test_parse_valid_response():
    raw = json.dumps({"account": "운반비", "confidence": 0.9, "reason": "택배사"})
    got = local_llm.parse_response(raw, ALLOWED)
    assert got == {"account": "운반비", "confidence": 0.9, "reason": "택배사"}


def test_parse_ignores_text_around_json():
    """모델이 앞뒤에 설명을 붙여도 JSON 만 떼어낸다."""
    raw = '다음과 같습니다.\n{"account": "운반비", "confidence": 0.8, "reason": "x"}\n이상입니다.'
    assert local_llm.parse_response(raw, ALLOWED)["account"] == "운반비"


def test_parse_clamps_confidence():
    for value, expected in ((5, 1.0), (-3, 0.0), ("0.5", 0.5)):
        raw = json.dumps({"account": "운반비", "confidence": value})
        assert local_llm.parse_response(raw, ALLOWED)["confidence"] == expected


def test_parse_truncates_long_reason():
    raw = json.dumps({"account": "운반비", "confidence": 1, "reason": "가" * 500})
    assert len(local_llm.parse_response(raw, ALLOWED)["reason"]) == 120


# ---------------------------------------------------------------
# parse_response — 믿을 수 없는 응답은 전부 미분류
# ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "JSON 아니고 그냥 말",
        "{깨진 json",
        '["리스트", "객체아님"]',
        '"문자열"',
        "null",
    ],
)
def test_parse_unusable_response_falls_back_to_unclassified(raw):
    got = local_llm.parse_response(raw, ALLOWED)
    assert got["account"] == UNCLASSIFIED
    assert got["confidence"] == 0.0


def test_parse_rejects_account_outside_allowed_list():
    """LLM 이 없는 계정을 만들어내는 경우 — 제일 위험한 실패다."""
    raw = json.dumps({"account": "카드수수료비용", "confidence": 0.99})
    got = local_llm.parse_response(raw, ALLOWED)
    assert got["account"] == UNCLASSIFIED
    assert got["confidence"] == 0.0
    assert "목록 외 계정" in got["reason"]
    assert "카드수수료비용" in got["reason"]  # 추적 가능해야 한다


def test_parse_rejects_unclassified_echo_as_out_of_list():
    """'미분류' 자체는 목록에 없으므로 미분류로 되돌아온다 (의도된 동작)."""
    raw = json.dumps({"account": UNCLASSIFIED, "confidence": 0.1})
    assert local_llm.parse_response(raw, ALLOWED)["account"] == UNCLASSIFIED


def test_parse_missing_account_key():
    got = local_llm.parse_response(json.dumps({"confidence": 0.9}), ALLOWED)
    assert got["account"] == UNCLASSIFIED
    assert "없음" in got["reason"]


def test_parse_bad_confidence_type():
    raw = json.dumps({"account": "운반비", "confidence": "높음"})
    got = local_llm.parse_response(raw, ALLOWED)
    assert got["account"] == "운반비"
    assert got["confidence"] == 0.0


def test_parse_with_empty_allowed_list_rejects_everything():
    """경계: 고를 수 있는 계정이 없으면 무엇도 통과하지 못한다."""
    raw = json.dumps({"account": "운반비", "confidence": 1.0})
    assert local_llm.parse_response(raw, [])["account"] == UNCLASSIFIED


# ---------------------------------------------------------------
# 통신 실패 — 예외를 밖으로 던지지 않는다
# ---------------------------------------------------------------


def test_available_false_when_ollama_absent(monkeypatch):
    def _boom(*a, **k):
        raise urllib.error.URLError("연결 거부")

    monkeypatch.setattr(local_llm.urllib.request, "urlopen", _boom)
    assert local_llm.available() is False
    assert local_llm.installed_models() == []


def test_ask_returns_none_on_failure(monkeypatch):
    def _boom(*a, **k):
        raise TimeoutError("시간 초과")

    monkeypatch.setattr(local_llm.urllib.request, "urlopen", _boom)
    assert local_llm.ask("아무 프롬프트") is None


def test_classify_merchant_survives_llm_failure(monkeypatch):
    """수백 건을 도는 중 한 건이 실패해도 전체가 멈추면 안 된다."""
    monkeypatch.setattr(local_llm, "ask", lambda *a, **k: None)
    got = local_llm.classify_merchant("어떤주유소")
    assert got["account"] == UNCLASSIFIED
    assert got["reason"] == "LLM 호출 실패"


def test_classify_merchant_passes_through_parsed_result(monkeypatch):
    monkeypatch.setattr(
        local_llm, "ask",
        lambda *a, **k: json.dumps({"account": "운반비", "confidence": 0.7, "reason": "택배"}),
    )
    got = local_llm.classify_merchant("어떤택배")
    assert got == {"account": "운반비", "confidence": 0.7, "reason": "택배"}


def test_prompt_contains_name_and_accounts():
    """프롬프트에 계정 목록이 안 들어가면 LLM 이 아무 계정이나 만들어낸다."""
    prompt = local_llm.PROMPT.format(
        name="테스트가맹점", accounts="\n".join(f"- {a}" for a in ALLOWED)
    )
    assert "테스트가맹점" in prompt
    assert "운반비" in prompt
    assert "미분류" in prompt  # 확신 없으면 미분류하라는 지시
