"""
[단계 3] 너구리 페르소나 대본 초안 생성
[단계 4] ElevenLabs TTS 음성 생성
================================================
사용법
  python scripts_generate_script.py --draft-only            # 대본 초안만 생성
  python scripts_generate_script.py --draft-only --approve   # 초안 생성 + 즉시 승인(자동 모드)
  python scripts_generate_script.py --tts-only               # 승인된 대본으로 음성 생성
  python scripts_generate_script.py --update-voices          # ElevenLabs 음성 목록 갱신

원칙
- LLM 백엔드가 없으면 실패한다(가짜 대본 금지). `--backend template` 을 명시해야만 템플릿 사용.
- ElevenLabs 키가 플레이스홀더면 실패한다(무음 더미 음성 금지).
- 트렌드 2단계가 고른 후보(output/trends.json)와 채널 분석(output/channel_analysis.json)을
  프롬프트에 그대로 넣어 "왜 이 주제인지"가 대본에 반영되게 한다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT / "config.json"
OUTPUT_DIR = PROJECT / "output"

# stdout 를 새 래퍼로 교체하면, 웹 서버가 이 모듈을 import 할 때 기존 스트림이 닫혀
# Werkzeug 로깅/소켓이 깨진다. 인코딩만 바꾸는 reconfigure 를 사용한다.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv(PROJECT / ".env", override=True)

import llm_client


def expand(path: str) -> str:
    return os.path.normpath(os.path.expandvars(path))


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def read_json(name: str) -> dict:
    p = OUTPUT_DIR / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ----------------------------------------------------------------------
# 프롬프트
# ----------------------------------------------------------------------
def build_prompt(keyword: str, today: str, config: dict, trend: dict, channel: dict,
                 note: str = "") -> str:
    p = config["persona"]
    min_chars = int(p.get("min_chars", 260))
    ch = channel.get("channel", {}) if channel else {}
    keywords = ", ".join(k["keyword"] for k in (channel.get("top_keywords", []) or [])[:8]) or "(없음)"
    top_titles = [v["title"] for v in (channel.get("top_videos", []) or [])[:3]]

    trend_block = ""
    if trend:
        sel = trend.get("selected", {})
        trend_block = (
            f"참고할 핫한 영상:\n"
            f"- 제목: {sel.get('title', '')}\n"
            f"- 출처: {sel.get('source', '')} / {sel.get('url', '')}\n"
            f"- 반응: 조회수 {sel.get('views', 0):,} / 좋아요 {sel.get('likes', 0):,}\n"
            f"- 내용 발췌: {(sel.get('content_excerpt') or '(설명 없음)')[:600]}\n"
            f"- 선택 이유: {sel.get('reason', '')}\n"
        )

    return (
        f"오늘 날짜: {today}\n"
        f"키워드: {keyword}\n\n"
        f"내 채널 정보:\n"
        f"- 채널명: {ch.get('title', '(미분석)')}\n"
        f"- 자주 다루는 키워드: {keywords}\n"
        f"- 인기 영상 예시: {' / '.join(top_titles) if top_titles else '(없음)'}\n\n"
        f"{trend_block}\n"
        f"{('내가 직접 정한 주제 요청: ' + note.strip()) if note.strip() else ''}\n"
        f"너구리 쇼츠 대본을 작성해라.\n"
        f"규칙:\n"
        f"- 말투: {p['tone']} (위트 있는 테크 너구리, 친근한 반말)\n"
        f"- 모든 문장은 반말로 끝낸다. '~요/습니다/해요' 존댓말 금지\n"
        f"- 영어 문장/인용 금지 (고유명사만 영어 허용)\n"
        f"- 길이: 공백 포함 {min_chars}~{p['max_chars']}자 (최소 {min_chars}자, 절대 초과 금지)\n"
        f"- 분량: 30초 이상 {p['max_duration_sec']}초 미만 (문장 6~8개)\n"
        f"- 구조: 1문장 훅(3초) → 핵심 4~6문장(왜 중요한지 · 어떻게 쓰는지 · 실용 팁) → 마무리 CTA 1문장\n"
        f"- 감정 태그는 문장 앞에 [excited], [giggles], [whisper] 중 필요한 것만\n"
        f"- 언어: {p['language']} (한국어)\n"
        f"- 해시태그/이모지/설명/제목/메타데이터 금지. 대본 본문만 출력.\n"
        f"- 참고 영상의 내용을 그대로 베끼지 말고, 내 채널 관점으로 재해석해라.\n"
    )


# ----------------------------------------------------------------------
# 후처리
# ----------------------------------------------------------------------
SENTENCE_END = re.compile(r"(?<=[.!?！？다요죠니다])\s+")


EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\uFE0F\u2600\u2B50\u2B55"
    "]+",
    flags=re.UNICODE,
)


def clean_script(raw: str) -> str:
    text = raw.strip()
    # 코드펜스 제거
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    # 라벨 제거
    text = re.sub(r"^\s*(대본|스크립트|script|출력)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    # 마크다운 강조/헤더 제거
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.MULTILINE)
    # 시간/연출 지문 제거: (0~3초) (화면 전환) (자막: ...) 등
    text = re.sub(r"[（(][^)）]*(?:초|컷|화면|자막|BGM|효과음|연출|장면|카메라)[^)）]*[)）]", "", text)
    # 이모지/해시태그 제거
    text = EMOJI_RE.sub("", text)
    text = re.sub(r"#\S+", "", text)
    # 마크다운 불릿/번호 제거
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
    # 따옴표로 감싼 경우 제거
    if len(text) > 2 and text[0] in "“\"'" and text[-1] in "”\"'":
        text = text[1:-1]
    # 공백/빈 줄 정리
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def enforce_length(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    # 문장 경계에서 자르기
    cut = text[:max_chars]
    boundary = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"), cut.rfind("다"), cut.rfind("\n"))
    if boundary >= max_chars * 0.5:
        trimmed = cut[: boundary + 1].strip()
    else:
        trimmed = cut[: max_chars - 1].rstrip() + "…"
    return trimmed, True


# ----------------------------------------------------------------------
# ElevenLabs
# ----------------------------------------------------------------------
def is_real_key(key: str | None) -> bool:
    """플레이스홀더가 아닌 키가 .env 에 있는지만 본다.
    (키 유효성은 API 호출로만 알 수 있으므로 여기서 길이로 단정하지 않는다.)"""
    if not key:
        return False
    key = key.strip()
    if "your" in key.lower() or key.lower().startswith("sk_your"):
        return False
    return key.startswith("sk_") and len(key) >= 20


def verify_elevenlabs_key(key: str | None, timeout: float = 15.0) -> tuple[bool, str]:
    """키 유효성 확인. (True/False, 사람이 읽을 사유)

    /v1/user 는 user_read 권한이 있어야만 200 이 온다. ElevenLabs 대시보드에서
    권한을 좁혀 만든 키(음성/TTS 만 허용)는 정상인데도 401 missing_permissions 가
    떨어져서 '키 무효'로 잘못 표시됐다. 그래서 voices_read 권한만 필요한
    /v1/voices 로 유효성을 판단하고, 사용량은 권한이 있을 때만 덧붙인다.
    """
    key = (key or "").strip()
    if not is_real_key(key):
        return False, ".env 에 ELEVENLABS_API_KEY 가 없습니다(또는 플레이스홀더)."

    try:
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": key}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        count = len(data.get("voices", []) or [])
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, ("키가 유효하지 않습니다 (HTTP 401). .env 의 키가 잘렸는지 확인하세요 "
                           f"(길이 {len(key)}자).")
        if e.code == 429:
            return True, "키 유효 (요청 한도 초과) · 잠시 후 음성 목록을 다시 불러오세요"
        return False, f"ElevenLabs 응답 오류 (HTTP {e.code})"
    except Exception as e:  # noqa: BLE001
        return False, f"ElevenLabs 확인 실패: {type(e).__name__}"

    detail = f"유효 · 음성 {count}개"
    # 사용량(구독)은 user_read 권한이 있을 때만 조회된다. 없으면 조용히 생략.
    try:
        req2 = urllib.request.Request(
            "https://api.elevenlabs.io/v1/user", headers={"xi-api-key": key}
        )
        with urllib.request.urlopen(req2, timeout=timeout) as resp2:
            sub = (json.loads(resp2.read().decode()).get("subscription") or {})
        detail += (f" · {sub.get('tier', '?')} 플랜 · "
                   f"{sub.get('character_count', 0):,}/{sub.get('character_limit', 0):,}자 사용")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            detail += " · 사용량 조회 권한(user_read) 없음 — 음성/TTS는 정상"
    except Exception:  # noqa: BLE001
        pass
    return True, detail


def get_available_voices(api_key: str) -> list[dict]:
    req = urllib.request.Request("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": api_key})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    voices = []
    for v in data.get("voices", []):
        labels = v.get("labels") or {}
        voices.append({
            "voice_id": v.get("voice_id", ""),
            "name": v.get("name", ""),
            "category": v.get("category", ""),
            "language": labels.get("language", ""),
            # 목록에서 남/여를 구분할 수 있게 라벨을 같이 저장한다
            "gender": labels.get("gender", ""),
            "age": labels.get("age", ""),
            "accent": labels.get("accent", ""),
        })
    return voices


def update_voices_config(config: dict) -> int:
    key = os.getenv("ELEVENLABS_API_KEY")
    if not is_real_key(key):
        print("[ERROR] ELEVENLABS_API_KEY 가 설정되지 않았거나 플레이스홀더입니다. .env 를 확인하세요.")
        return 1
    try:
        voices = get_available_voices(key)
    except Exception as e:
        print(f"[ERROR] 음성 목록 조회 실패: {e}")
        return 1
    if not voices:
        print("[WARN] API가 빈 음성 목록을 반환했습니다.")
        return 1
    config.setdefault("elevenlabs", {})["voices"] = voices
    ids = [v["voice_id"] for v in voices]
    if config["elevenlabs"].get("voice_id") not in ids:
        config["elevenlabs"]["voice_id"] = ids[0]
    config["elevenlabs"].setdefault("default_voice_id", config["elevenlabs"]["voice_id"])
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 음성 {len(voices)}개를 config.json 에 저장했습니다.")
    for v in voices[:20]:
        print(f"  - {v['name']} ({v['voice_id']}) [{v['language']}]")
    return 0


def pick_voice_id(config: dict) -> str:
    """우선순위: config default_voice_id > config voice_id > .env ELEVENLABS_VOICE_ID

    웹에서 고른 음성이 항상 이기도록 config 를 먼저 본다.
    (.env 는 config 가 비어 있을 때만 쓰는 보조 값)
    """
    el = config.get("elevenlabs", {}) or {}
    voices = [v["voice_id"] for v in el.get("voices", []) or []]
    for candidate in (el.get("default_voice_id"), el.get("voice_id")):
        if candidate and (not voices or candidate in voices):
            return candidate
    env_voice = (os.getenv("ELEVENLABS_VOICE_ID") or "").strip()
    if env_voice:
        return env_voice
    return voices[0] if voices else ""


def probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float((out.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def strip_style_tags(text: str) -> str:
    """edge-tts 등 SSML 태그를 지원하지 않는 엔진용 대본 정리."""
    cleaned = re.sub(r"\[[^\]\n]{1,24}\]", "", text)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return "\n".join(line.strip() for line in cleaned.splitlines() if line.strip())


def parse_rate_percent(value) -> int:
    """' +15% ', '1.15', '0.9', 15 같은 표기를 % 정수(-30~20)로 바꿈다."""
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        num = float(value)
        pct = round((num - 1) * 100) if 0.5 <= num <= 1.5 else round(num)
        return max(-30, min(20, int(pct)))
    s = str(value).strip().replace(" ", "")
    try:
        if s.endswith("%"):
            pct = round(float(s[:-1]))
        else:
            num = float(s)
            pct = round((num - 1) * 100) if 0.5 <= num <= 1.5 else round(num)
    except ValueError:
        return 0
    return max(-30, min(20, int(pct)))


def rate_percent_from_config(config: dict) -> int:
    tts_cfg = config.get("tts", {}) or {}
    if "rate_percent" in tts_cfg:
        return parse_rate_percent(tts_cfg.get("rate_percent"))
    return parse_rate_percent(tts_cfg.get("edge_rate") or "+0%")


def synth_elevenlabs(script_text: str, config: dict, audio_path: Path,
                     voice_id: str | None = None, model_id: str | None = None,
                     rate_percent: int | None = None) -> dict:
    """voice_id/model_id/rate_percent 를 주면 config 보다 그 값을 우선한다(웹 샘플 미리듣기용)."""
    key = os.getenv("ELEVENLABS_API_KEY")
    el = config.get("elevenlabs", {}) or {}
    voice_id = (voice_id or "").strip() or pick_voice_id(config)
    if not voice_id:
        raise RuntimeError("사용할 voice_id 가 없습니다. --update-voices 로 목록을 갱신하세요.")
    model_id = ((model_id or "").strip()
                or el.get("model_id") or os.getenv("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2")
    output_format = el.get("output_format", "mp3_44100_128")

    # [excited] 같은 감정 태그는 eleven_v3 만 오디오 태그로 해석한다.
    # v2 계열에서는 그대로 읽혀서 "excited" 가 들릴 수 있으니 제거한다.
    text = script_text if model_id.startswith("eleven_v3") else strip_style_tags(script_text)
    if not text:
        raise RuntimeError("대본이 비어 있습니다.")

    pct = rate_percent_from_config(config) if rate_percent is None else int(rate_percent)
    pct = max(-30, min(20, pct))
    # ElevenLabs speed 는 0.7~1.2 만 허용한다
    speed = max(0.7, min(1.2, 1 + pct / 100))

    print(f"[TTS] ElevenLabs voice={voice_id} model={model_id} format={output_format} "
          f"speed={speed:.2f} chars={len(text)}")
    from elevenlabs import ElevenLabs, save
    from elevenlabs.types import VoiceSettings

    client = ElevenLabs(api_key=key)
    audio = client.text_to_speech.convert(
        voice_id=voice_id, model_id=model_id, text=text, output_format=output_format,
        voice_settings=VoiceSettings(speed=speed),
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    save(audio, str(audio_path))
    return {"provider": "elevenlabs", "voice_id": voice_id, "model_id": model_id,
            "output_format": output_format, "speed": round(speed, 2),
            "rate_percent": pct}


def synth_edge(script_text: str, config: dict, audio_path: Path,
               rate_percent: int | None = None) -> dict:
    """무료 edge-tts 폴백 (API 키 불필요, 실제 음성)."""
    tts_cfg = config.get("tts", {}) or {}
    voice = (tts_cfg.get("edge_voice") or "ko-KR-SunHiNeural").strip()
    # 공통 속도 설정(rate_percent)을 우선 사용 — 웹의 '말하기 속도'가 두 엔진 모두에 적용된다
    pct = rate_percent_from_config(config) if rate_percent is None else int(rate_percent)
    pct = max(-30, min(20, pct))
    rate = f"{'+' if pct >= 0 else ''}{pct}%"
    text = strip_style_tags(script_text)
    if not text:
        raise RuntimeError("대본이 비어 있습니다.")

    print(f"[TTS] edge-tts voice={voice} rate={rate} chars={len(text)}")
    try:
        import asyncio

        import edge_tts
    except ImportError as e:
        raise RuntimeError(
            "edge-tts 가 설치되지 않았습니다. `pip install edge-tts` 로 설치하세요."
        ) from e

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    async def _run() -> None:
        audio = edge_tts.Communicate(text, voice, rate=rate)
        await audio.save(str(audio_path))

    asyncio.run(_run())
    return {"provider": "edge-tts", "voice_id": voice, "model_id": "edge-neural",
            "output_format": "mp3", "rate": rate}


def synthesize(script_text: str, config: dict) -> int:
    audio_path = OUTPUT_DIR / "voice.mp3"
    el = config.get("elevenlabs", {}) or {}
    tts_cfg = config.get("tts", {}) or {}
    provider_pref = (tts_cfg.get("provider") or "auto").strip().lower()
    min_duration = float(tts_cfg.get("min_duration_sec", 3))
    key = os.getenv("ELEVENLABS_API_KEY")

    if el.get("enabled") is False and provider_pref == "elevenlabs":
        print("[SKIP] config.elevenlabs.enabled=false")
        return 1

    # 시도 순서 결정 (auto: 키가 있으면 ElevenLabs → edge-tts, 없으면 edge-tts)
    attempts: list[str] = []
    if provider_pref in ("auto", "elevenlabs") and el.get("enabled") is not False:
        if is_real_key(key):
            attempts.append("elevenlabs")
            print(f"[TTS] .env 키 발견 (길이 {len((key or '').strip())}자) → ElevenLabs 시도")
        else:
            print("[TTS] ElevenLabs API 키가 없습니다(또는 플레이스홀더) → edge-tts 폴백 사용")
    if provider_pref == "edge" or (provider_pref != "elevenlabs" and tts_cfg.get("fallback_to_edge", True)):
        attempts.append("edge")

    if not attempts:
        print("[ERROR] 사용할 TTS 경로가 없습니다.")
        print("        .env 의 ELEVENLABS_API_KEY 에 실제 키를 넣거나,")
        print("        config.tts.provider 를 auto/edge 로 두세요 (edge-tts 는 키 불필요).")
        return 1

    if re.search(r"[가-힣]", script_text):
        print("[INFO] 한국어 대본입니다. 영어 전용 음성은 발음이 어색할 수 있습니다.")

    audio_path.unlink(missing_ok=True)
    errors: list[str] = []
    for provider in attempts:
        try:
            if provider == "elevenlabs":
                info = synth_elevenlabs(script_text, config, audio_path)
            else:
                info = synth_edge(script_text, config, audio_path)
        except Exception as e:
            msg = f"{provider}: {type(e).__name__}: {e}"
            errors.append(msg)
            print(f"[WARN] TTS 실패 — {msg}")
            continue
        if not audio_path.exists():
            errors.append(f"{provider}: 파일이 생성되지 않았습니다")
            continue
        size = audio_path.stat().st_size
        duration = probe_duration(audio_path)
        if size < 3000 or duration < min_duration:
            errors.append(f"{provider}: 비정상 음성 (크기 {size}B, 길이 {duration:.2f}s)")
            print(f"[WARN] {errors[-1]}")
            audio_path.unlink(missing_ok=True)
            continue

        meta = {
            **info,
            "fallback": bool(errors),
            "skipped_providers": errors,
            "chars": len(script_text),
            "size_bytes": size,
            "duration_sec": round(duration, 2),
            "script_sha": __import__("hashlib").sha256(script_text.encode("utf-8")).hexdigest(),
        }
        (OUTPUT_DIR / "voice_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[OK] 음성 저장: {audio_path} ({duration:.1f}초, {size/1024:.0f}KB, {info['provider']})")
        return 0

    print("[ERROR] 모든 TTS 경로가 실패했습니다.")
    for e in errors:
        print(f"        - {e}")
    print("        (무음 더미를 만들지 않습니다 — 자막 0개/1초 영상 방지)")
    return 1


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="너구리 쇼츠 대본 + TTS")
    ap.add_argument("keyword", nargs="?", default=None)
    ap.add_argument("--draft-only", action="store_true")
    ap.add_argument("--approve", action="store_true", help="초안을 script.txt 로 승인(복사)")
    ap.add_argument("--auto-approve", action="store_true", help="(구버전 호환) --approve 와 동일")
    ap.add_argument("--tts-only", action="store_true")
    ap.add_argument("--update-voices", action="store_true")
    ap.add_argument("--backend", default=None,
                    help="auto | freebuff | gemini | groq | openrouter | ollama | template")
    ap.add_argument("--manual", action="store_true",
                    help="내가 입력한 주제로만 대본 작성(트렌드 후보·발췌 무시)")
    ap.add_argument("--note", default="", help="(선택) 대본에 꼭 넣고 싶은 포인트")
    # Windows 는 argv 를 ANSI 로 해석해 한글이 '?' 로 깨진다 → 웹은 환경변수로 넘긴다.
    ap.add_argument("--topic-env", action="store_true",
                    help="주제/포인트를 환경변수(RACCOON_TOPIC/RACCOON_NOTE)에서 읽기")
    ap.add_argument("--force", action="store_true", help="(호환용) 기존 플래그 무시")
    args = ap.parse_args()

    config = load_config()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    approve = args.approve or args.auto_approve

    if args.update_voices:
        return update_voices_config(config)

    if args.tts_only:
        script_path = OUTPUT_DIR / "script.txt"
        if not script_path.exists() or script_path.stat().st_size < 10:
            print("[ERROR] 승인된 대본이 없습니다: output/script.txt")
            print("        3단계에서 초안을 만들고 웹 UI에서 '승인'을 누르세요.")
            return 1
        text = script_path.read_text(encoding="utf-8").strip()
        print(f"[1/1] 승인된 대본 {len(text)}자로 음성 생성")
        return synthesize(text, config)

    # ---------- 대본 초안 ----------
    env_topic = os.environ.get("RACCOON_TOPIC", "").strip()
    env_note = os.environ.get("RACCOON_NOTE", "").strip()
    manual = bool(args.manual or args.topic_env or env_topic)
    note = (env_note or args.note or "").strip()

    # 수동 주제는 트렌드 후보/발췌를 프롬프트에 넣지 않는다(내 주제만으로 쓴다).
    trend = {} if manual else read_json("trends.json")
    channel = read_json("channel_analysis.json")

    keyword = (env_topic if manual else "") or args.keyword
    if not keyword:
        keyword = (trend.get("selected_keyword") or "").strip()
    if not keyword:
        print("[ERROR] 키워드가 없습니다. 2단계(트렌드)를 먼저 실행하거나 키워드를 인자로 주세요.")
        return 1

    today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    prompt = build_prompt(keyword, today, config, trend, channel, note)

    print("=" * 64)
    print(f"[대본] 키워드: {keyword}")
    print(f"[대본] 방식: {'수동 주제 (트렌드 미사용)' if manual else '트렌드 기반'}")
    if note:
        print(f"[대본] 추가 요청: {note}")
    if trend.get("selected"):
        print(f"[대본] 참고 영상: {trend['selected'].get('title', '')[:70]}")
        print(f"[대본] 선택 이유: {trend['selected'].get('reason', '')}")
    print("=" * 64)

    try:
        raw, meta = llm_client.generate(prompt, config, args.backend)
    except Exception as e:
        print(f"[ERROR] {e}")
        return 1

    text = clean_script(raw)
    max_chars = int(config["persona"].get("max_chars", 350))
    min_chars = int(config["persona"].get("min_chars", 260))
    text, trimmed = enforce_length(text, max_chars)

    # 30초 이상 쇼츠가 되도록 너무 짧은 초안은 한 번만 늘려서 재생성한다
    length_attempts = 1
    while len(text) < min_chars and length_attempts < 2:
        print(f"[WARN] 초안이 {len(text)}자로 짧습니다 (최소 {min_chars}자). 한 번 더 생성합니다.")
        retry_prompt = (
            prompt
            + f"\n\n[수정 요청] 방금 초안은 {len(text)}자라서 30초가 안 된다. "
            f"아래 초안 문장을 그대로 반복하지 말고, 같은 주제로 정보를 더 구체적으로 풀어 "
            f"{min_chars}~{max_chars}자 분량의 새 대본을 써라.\n"
            f"- 문장 7개, 각 문장은 공백 포함 35자 이상\n"
            f"- 각 문장에 '왜 중요한지 / 어떻게 쓰는지 / 주의할 점 / 실제 예시' 중 하나를 담아라\n"
            f"- 마지막 문장만 CTA\n\n"
            f"(참고용 이전 초안 — 복사 금지)\n{text}\n"
        )
        # 같은 문장이 반복되지 않도록 재시도는 온도를 올린다
        retry_config = dict(config)
        llm_cfg = dict(config.get("llm") or {})
        llm_cfg["temperature"] = max(0.95, float(llm_cfg.get("temperature", 0.7)))
        retry_config["llm"] = llm_cfg
        try:
            raw, meta = llm_client.generate(retry_prompt, retry_config, args.backend)
        except Exception as e:
            print(f"[WARN] 재생성 실패: {e}")
            break
        text = clean_script(raw)
        text, trimmed = enforce_length(text, max_chars)
        length_attempts += 1
    if len(text) < min_chars:
        print(f"[WARN] {len(text)}자 (최소 {min_chars}자 미달). 웹에서 대본을 조금 늘리면 30초 이상이 됩니다.")

    if len(text) < 20:
        print(f"[ERROR] 생성된 대본이 너무 짧습니다({len(text)}자). 백엔드 응답: {raw[:200]!r}")
        return 1

    (OUTPUT_DIR / "script_draft.txt").write_text(text, encoding="utf-8")
    meta_out = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "source": "manual" if manual else "trend",
        "note": note,
        "keyword": keyword,
        "backend": meta.get("backend"),
        "model": meta.get("model"),
        "backend_attempts": meta.get("attempts", []),
        "raw_chars": len(raw),
        "final_chars": len(text),
        "min_chars": min_chars,
        "length_attempts": length_attempts,
        "trimmed": trimmed,
        "trend": {
            "id": (trend.get("selected") or {}).get("id", ""),
            "title": (trend.get("selected") or {}).get("title", ""),
            "url": (trend.get("selected") or {}).get("url", ""),
            "source": (trend.get("selected") or {}).get("source", ""),
            "score": (trend.get("selected") or {}).get("score"),
        },
        "channel": {
            "title": ((channel.get("channel") or {}).get("title", "")),
            "keywords": [k["keyword"] for k in (channel.get("top_keywords") or [])],
        },
        "prompt": prompt,
    }
    (OUTPUT_DIR / "script_meta.json").write_text(
        json.dumps(meta_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("")
    print(text)
    print("")
    print(f"[OK] 대본 초안 저장: output/script_draft.txt ({len(text)}자, 백엔드 {meta.get('backend')}"
          f"{' / ' + str(meta.get('model')) if meta.get('model') else ''})")
    if trimmed:
        print(f"[WARN] {max_chars}자 제한으로 문장을 잘라냈습니다 (원본 {len(raw)}자). 웹에서 다듬으세요.")

    if not approve:
        print("[NEXT] 웹 UI에서 대본을 검토/수정한 뒤 '승인'하거나 --approve 로 실행하세요.")
        return 0

    shutil.copyfile(OUTPUT_DIR / "script_draft.txt", OUTPUT_DIR / "script.txt")
    print(f"[OK] 대본 승인(복사): output/script.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
