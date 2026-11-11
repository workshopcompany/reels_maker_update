"""
[공용] 유튜브 업로드 문구(제목·설명·태그·썸네일 추천) 생성
============================================================
업로드 단계(scripts_publish_youtube.py)와 웹 UI 가 같이 쓴다.

- 설명란에 대본을 그대로 붙이지 않고, 대본·트렌드·채널 키워드로 **새 설명문구**를 만든다.
- LLM 이 쓰는 마크다운 강조(**), 백틱, 목록 기호, 괄호 같은 특수문자는 clean_plain() 으로 걷어낸다.
- 썸네일 추천 한 줄을 만들어 설명문구 끝에 넣는다.
- 결과는 output/youtube_meta.json 에 저장하고, 웹에서 직접 수정할 수 있다.

LLM 이 없거나 실패하면 대본 기반 템플릿으로 만들고 source 를 남긴다(가짜 문구를 만들지 않는다).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT / "config.json"
OUTPUT_DIR = PROJECT / "output"
META_FILE = OUTPUT_DIR / "youtube_meta.json"

DEFAULT_TAGS = ["shorts", "쇼츠", "ai", "it", "꿀팁"]
HASHTAGS = "#Shorts #AI #IT꿀팁 #테크"


def expand(path: str) -> str:
    return os.path.normpath(os.path.expandvars(path))


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ----------------------------------------------------------------------
# 평문 정리 (AI 가 쓰는 마크다운/특수문자 제거)
# ----------------------------------------------------------------------
def clean_plain(text: str) -> str:
    """마크다운 강조·기호·불필요한 특수문자를 걷어낸 평문으로 만든다.

    - `**강조**`, `*`, `__`, `~~`, 백틱 → 제거
    - `# 제목`(공백 있는 제목 기호) → 기호만 제거, `#Shorts` 해시태그는 유지
    - `[텍스트](링크)` → 텍스트만, `[]<>|\\` 같은 장식 기호 제거
    """
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"</?[a-zA-Z][^>]*>", "", t)          # HTML 태그
    t = re.sub(r"`{1,3}", "", t)
    t = re.sub(r"\*\*|__|~~|\*", "", t)                  # 마크다운 강조
    t = re.sub(r"^#{1,6}\s+", "", t, flags=re.MULTILINE)  # 제목 기호
    t = re.sub(r"#{2,}", "", t)                            # 남은 ##
    t = re.sub(r"#+(?=\s)", "", t)                          # 문장 중 '#' 기호(#Shorts 는 유지)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.MULTILINE)     # 인용
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)        # 링크 → 텍스트
    t = re.sub(r"[\[\]<>|\\{}]", "", t)
    t = re.sub(r"^[\-\u2022]\s+", "", t, flags=re.MULTILINE)   # 목록 기호
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    lines = [ln.strip() for ln in t.split("\n")]
    return "\n".join(lines).strip()


def build_prompt(script: str, trend: dict, channel_keywords: list[str]) -> str:
    tags = ", ".join(channel_keywords[:8]) or "(없음)"
    return f"""너는 유튜브 쇼츠 메타데이터 작성자다. 아래 대본으로 업로드용 제목/설명/태그/썸네일 추천을 만들어라.

[대본]
{script}

[트렌드 주제] {trend.get('title') or ''} / {trend.get('keyword') or ''}
[채널 키워드] {tags}

규칙
- title: 40자 이내 한국어 한 줄. 낚시성 과장 금지. '#shorts' 는 넣지 마라(업로드 시 따로 붙인다).
- description: 5~7줄 평문. 첫 줄은 후킹 문장, 중간은 핵심 내용 3줄, 마지막 줄은 해시태그 3~5개.
- 마크다운 강조(**), 별표, 백틱, 목록 기호, 이모지, 특수문자 금지. 문장부호 . , ! ? ~ 만 허용.
- tags: 소문자 키워드 8~12개 배열.
- thumbnail: 썸네일 추천 한 줄. 2D 너구리 캐릭터가 나오는 장면 중 어떤 장면/표정인지 + 이미지에 크게 넣을 짧은 글자(3~6자) 를 포함.

출력은 아래 JSON 하나만. 다른 말은 쓰지 마라.
{{"title": "...", "description": "...", "tags": ["..."], "thumbnail": "..."}}"""


def _extract_json(text: str) -> dict:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ----------------------------------------------------------------------
# 생성
# ----------------------------------------------------------------------
def _context() -> dict:
    script = _read(OUTPUT_DIR / "script.txt")
    script_meta = _read_json(OUTPUT_DIR / "script_meta.json")
    trends = _read_json(OUTPUT_DIR / "trends.json")
    channel = _read_json(OUTPUT_DIR / "channel_analysis.json")
    selected = trends.get("selected") or {}
    trend = {
        "title": selected.get("title") or "",
        "keyword": trends.get("selected_keyword") or script_meta.get("keyword") or "",
        "reason": selected.get("reason") or "",
    }
    keywords = [k.get("keyword") for k in (channel.get("top_keywords") or []) if k.get("keyword")]
    return {"script": script, "trend": trend, "keywords": keywords}


def _template(ctx: dict) -> dict:
    script = ctx["script"]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", script) if s.strip()]
    hook = sentences[0] if sentences else (ctx["trend"]["keyword"] or "오늘의 꿀팁")
    body = " ".join(sentences[1:4]) if len(sentences) > 1 else ""
    keyword = ctx["trend"]["keyword"] or ctx["trend"]["title"] or "AI"
    description = "\n".join(x for x in [
        clean_plain(hook),
        clean_plain(body),
        f"이번 영상은 {keyword} 이야기입니다. 끝까지 보면 바로 써먹을 수 있어요.",
        f"썸네일 추천: 너구리가 놀란 표정 + 큰 글자 '{keyword[:6]}'",
        HASHTAGS,
    ] if x)
    return {
        "title": clean_plain(f"{keyword[:30]} 꿀팁")[:60],
        "description": clean_plain(description),
        "tags": DEFAULT_TAGS,
        "thumbnail": f"너구리가 놀란 표정 + 큰 글자 '{keyword[:6]}'",
        "source": "template",
    }


def generate(config: dict | None = None, use_llm: bool = True) -> dict:
    """제목/설명/태그/썸네일 추천을 만들고 output/youtube_meta.json 에 저장한다."""
    config = config or load_config()
    ctx = _context()
    if not ctx["script"]:
        raise RuntimeError("output/script.txt 가 없습니다. 3단계 대본을 먼저 만드세요.")

    data: dict = {}
    error = ""
    if use_llm:
        try:
            import llm_client
            text, meta = llm_client.generate(build_prompt(ctx["script"], ctx["trend"], ctx["keywords"]),
                                            config)
            parsed = _extract_json(text)
            if parsed.get("title") or parsed.get("description"):
                data = {
                    "title": str(parsed.get("title") or "").strip(),
                    "description": str(parsed.get("description") or "").strip(),
                    "tags": [str(t).strip().lstrip("#") for t in (parsed.get("tags") or []) if str(t).strip()],
                    "thumbnail": str(parsed.get("thumbnail") or "").strip(),
                    "source": "llm",
                    "provider": meta.get("backend") or meta.get("provider") or "",
                    "model": meta.get("model") or "",
                }
            else:
                error = "LLM 응답에서 JSON 을 찾지 못했습니다."
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"

    if not data:
        data = _template(ctx)

    # 마크다운/특수문자 정리 + 썸네일 추천을 설명문구에 반영
    data["title"] = clean_plain(data.get("title") or "")[:100]
    data["description"] = clean_plain(data.get("description") or "")
    data["thumbnail"] = clean_plain(data.get("thumbnail") or "")
    if data["thumbnail"] and data["thumbnail"] not in data["description"]:
        data["description"] = (data["description"].rstrip() +
                               f"\n\n썸네일 추천: {data['thumbnail']}").strip()
    if not data.get("tags"):
        data["tags"] = DEFAULT_TAGS
    if not data["description"].strip():
        raise RuntimeError("설명문구가 비었습니다. 다시 생성하세요.")

    data.update({
        "error": error,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "script_chars": len(ctx["script"]),
        "trend_keyword": ctx["trend"]["keyword"],
    })
    save(data)
    return data


def save(data: dict) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    META_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def load() -> dict:
    return _read_json(META_FILE)


def update(title: str | None = None, description: str | None = None,
           tags: list[str] | None = None, thumbnail: str | None = None) -> dict:
    """웹에서 수정한 값을 저장한다(설명문구는 다시 평문 정리)."""
    data = load() or {}
    if title is not None:
        data["title"] = clean_plain(title)[:100]
    if description is not None:
        data["description"] = clean_plain(description)
    if tags is not None:
        data["tags"] = [str(t).strip().lstrip("#") for t in tags if str(t).strip()]
    if thumbnail is not None:
        data["thumbnail"] = clean_plain(thumbnail)
    data["source"] = data.get("source") or "manual"
    return save(data)


def load_or_generate(config: dict | None = None) -> dict:
    """업로드 단계용: 저장된 문구가 없으면 생성한다."""
    data = load()
    if data.get("description") and data.get("title"):
        return data
    return generate(config)


def publish_payload(config: dict | None = None) -> dict:
    """업로드에 실제로 쓰일 제목/설명/태그(제목에 #shorts 보정)."""
    config = config or load_config()
    data = load_or_generate(config)
    title = (data.get("title") or "").strip()
    if title and "#shorts" not in title.lower():
        title = f"{title} #shorts"
    return {
        "title": title[:100],
        "description": data.get("description") or "",
        "tags": data.get("tags") or DEFAULT_TAGS,
        "thumbnail": data.get("thumbnail") or "",
        "source": data.get("source") or "",
        "meta": data,
    }
