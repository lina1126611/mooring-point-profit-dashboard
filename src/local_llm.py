"""로컬 LLM(Ollama) 보조 분류 — 원본 데이터를 PC 밖으로 내보내지 않는다.

왜 로컬인가:
  실데이터는 보안각서 대상이다. 거래처명·사용처를 클라우드 AI에 보내면
  각서 위반이다. 그런데 가명화하면 "주유소니까 차량유지비" 라는 판정 근거가
  사라져서 분류를 할 수 없다. 그래서 **분류는 원본을 쥔 로컬에서** 한다.
  밖으로 나가는 것은 '주유비/운반비' 같은 일반 회계용어뿐이다.

역할 분담 (LLM을 최소한으로 쓴다):
  1) rules.ACCOUNT_RULES — 결정적·테스트됨·즉시. 여기서 걸리면 LLM 안 쓴다.
  2) 규칙에 안 걸린 것만 LLM에 물어본다.
  3) LLM이 확신 없으면 '미분류'로 남긴다. 억지로 끼워 맞추지 않는다.
     (rules.py 의 핵심 원칙과 같다 — 틀린 분류는 미분류보다 나쁘다)

LLM 결과는 **확정이 아니라 사람 검토용 후보**다. 사람이 확인한 것만
rules.py 에 키워드로 반영하고, 원본 사용처명은 저장소에 커밋하지 않는다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from src.rules import COST_BEHAVIOR, UNCLASSIFIED

DEFAULT_HOST = "http://localhost:11434"
# RAM 여유가 적은 노트북 기준. 여유가 8GB 이상이면 qwen2.5:7b 가 더 정확하다.
DEFAULT_MODEL = "qwen2.5:3b"
TIMEOUT_PROBE = 2.0
TIMEOUT_GENERATE = 120.0

# LLM 이 고를 수 있는 계정 = COST_BEHAVIOR 에 등록된 것뿐.
# 여기 없는 계정을 반환하면 원가행태가 '해당없음' 으로 눕혀져 금액이 조용히
# 사라진다. 그래서 목록을 좁혀서 주고, 벗어난 답은 미분류로 되돌린다.
def allowed_accounts() -> list[str]:
    """LLM에 제시할 계정 목록. 매출·미분류는 제외한다."""
    from src.rules import NOT_APPLICABLE

    return sorted(
        name for name, behavior in COST_BEHAVIOR.items()
        if behavior != NOT_APPLICABLE
    )


PROMPT = """당신은 한국 중소 엔지니어링 회사의 경리 담당자입니다.
법인카드 사용처(가맹점) 이름을 보고 어느 계정과목에 해당하는지 판단하세요.

가맹점 이름: {name}

고를 수 있는 계정과목 (이 중 하나만):
{accounts}

규칙:
- 이름만으로 업종을 확신할 수 없으면 반드시 "미분류"라고 답하세요.
- 추측하지 마세요. 틀린 분류는 미분류보다 나쁩니다.
- confidence 는 0.0~1.0 사이 숫자입니다. 확신이 없으면 낮게 주세요.

아래 JSON 형식으로만 답하세요:
{{"account": "계정과목명", "confidence": 0.0, "reason": "판단 근거 한 문장"}}"""


# ===============================================================
# 응답 파싱 — 순수 함수라 테스트로 고정한다
# ===============================================================


def parse_response(raw: str, allowed: list[str]) -> dict:
    """LLM 응답에서 (계정, 확신도, 근거)를 뽑는다.

    LLM 은 목록에 없는 계정을 만들어내거나 JSON 을 깨뜨릴 수 있다. 그때
    조용히 통과시키면 원가행태 매핑에 없는 계정이 DB 에 들어가 금액이
    사라진다. 그래서 **의심스러우면 전부 미분류로 되돌린다.**
    """
    fallback = {"account": UNCLASSIFIED, "confidence": 0.0, "reason": ""}

    text = (raw or "").strip()
    if not text:
        return {**fallback, "reason": "빈 응답"}

    # 모델이 앞뒤에 설명을 붙이는 경우가 있어 첫 JSON 객체만 떼어낸다
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {**fallback, "reason": "JSON 아님"}

    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {**fallback, "reason": "JSON 파싱 실패"}
    if not isinstance(data, dict):
        return {**fallback, "reason": "JSON 이 객체가 아님"}

    account = str(data.get("account", "")).strip()
    if account not in allowed:
        # 목록에 없는 계정을 만들어냈다 → 미분류. 근거는 남겨서 추적 가능하게.
        return {
            "account": UNCLASSIFIED,
            "confidence": 0.0,
            "reason": f"목록 외 계정({account or '없음'})",
        }

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    return {
        "account": account,
        "confidence": confidence,
        "reason": str(data.get("reason", "")).strip()[:120],
    }


# ===============================================================
# Ollama 통신
# ===============================================================


def available(host: str = DEFAULT_HOST) -> bool:
    """Ollama 가 떠 있는지 확인한다. 없으면 조용히 False (설치 안내는 호출부)."""
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=TIMEOUT_PROBE):
            return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def installed_models(host: str = DEFAULT_HOST) -> list[str]:
    """설치된 모델 이름 목록. Ollama 가 없으면 빈 리스트."""
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=TIMEOUT_PROBE) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return []
    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


def ask(
    prompt: str,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
) -> str | None:
    """Ollama 에 한 번 물어본다. 실패하면 None (예외를 밖으로 안 던진다).

    분류 스크립트는 수백 건을 도는데 중간에 한 건이 실패했다고 전체가
    멈추면 안 된다. 실패한 건은 미분류로 남고 사람이 처리한다.
    """
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0},  # 같은 입력에 같은 답이 나오도록
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{host}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_GENERATE) as r:
            return json.loads(r.read().decode("utf-8")).get("response")
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None


def classify_merchant(
    name: str,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    allowed: list[str] | None = None,
) -> dict:
    """가맹점 이름 하나를 계정과목 후보로 분류한다.

    반환: {"account", "confidence", "reason"} — 확정이 아니라 사람 검토용 후보.
    """
    allowed = allowed or allowed_accounts()
    prompt = PROMPT.format(name=name, accounts="\n".join(f"- {a}" for a in allowed))
    raw = ask(prompt, model=model, host=host)
    if raw is None:
        return {"account": UNCLASSIFIED, "confidence": 0.0, "reason": "LLM 호출 실패"}
    return parse_response(raw, allowed)
