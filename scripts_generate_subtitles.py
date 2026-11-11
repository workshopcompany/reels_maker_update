"""
[단계 7] 발화 타이밍 추출 (faster-whisper tiny)
=================================================
  python scripts_generate_subtitles.py
  python scripts_generate_subtitles.py --voice output/voice.mp3 --max-chars 12

역할
- 대본 단어가 '언제' 나오는지(시작/끝)만 얻는다. 영상에 글자를 굽지 않는다.
- tiny 모델은 한국어 받아쓰기 정확도가 낮기 때문에 여기서 나온 text 는 참고용이며,
  MP4 번인/CapCut 텍스트 트랙은 기본적으로 사용하지 않는다.
  (자막은 유튜브 쇼츠/릴스의 자동 자막 기능을 쓴다.)

산출물
- output/subtitles.json : {purpose:"timing-only", duration, model, subtitles:[{start,end,text}]}
- output/subtitles.srt  : 타임코드 확인용

원칙
- 로컬 모델 경로를 우선 사용한다(오프라인 동작). 없을 때만 모델 이름으로 다운로드를 시도한다.
- 타이밍 블록이 0개면 실패(종료 코드 1). 예전에는 무음 더미 음성 때문에 0개인데도 '완료'로 표시됐다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
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

load_dotenv(PROJECT / ".env", override=False)


def expand(path: str) -> str:
    return os.path.normpath(os.path.expandvars(path))


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def resolve_model(config: dict, explicit: str | None = None) -> str:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    cfg_model = ((config.get("paths", {}) or {}).get("whisper_model") or "")
    if cfg_model:
        candidates.append(expand(cfg_model))
    env_model = os.getenv("WHISPER_MODEL_PATH")
    if env_model:
        candidates.append(expand(env_model))

    for c in candidates:
        if not c:
            continue
        p = Path(c)
        if p.exists() and p.is_dir():
            return str(p)
        if p.exists() and p.is_file():
            return str(p.parent)

    # models/whisper/**/model.bin 자동 탐색
    for bin_file in sorted(PROJECT.glob("models/whisper/**/model.bin"),
                           key=lambda x: x.stat().st_mtime, reverse=True):
        return str(bin_file.parent)
    return ""


def srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def join_word(acc: str, word: str) -> str:
    if not acc:
        return word
    if word[:1] in ",.!?~%)]}":
        return acc + word
    return acc + " " + word


def group_words(words: list[dict], max_chars: int) -> list[dict]:
    blocks: list[dict] = []
    cur = {"text": "", "start": None, "end": None}
    for w in words:
        token = (w.get("word") or "").strip()
        if not token:
            continue
        candidate = join_word(cur["text"], token)
        if cur["text"] and len(candidate) > max_chars:
            blocks.append({"text": cur["text"], "start": cur["start"], "end": cur["end"]})
            cur = {"text": token, "start": w["start"], "end": w["end"]}
        else:
            cur["text"] = candidate
            cur["start"] = cur["start"] if cur["start"] is not None else w["start"]
            cur["end"] = w["end"]
    if cur["text"]:
        blocks.append({"text": cur["text"], "start": cur["start"], "end": cur["end"]})

    for i, b in enumerate(blocks):
        b["start"] = round(float(b["start"]), 2)
        b["end"] = round(max(float(b["end"]), b["start"] + 0.2), 2)
        if i + 1 < len(blocks) and b["end"] > blocks[i + 1]["start"] > b["start"]:
            b["end"] = round(blocks[i + 1]["start"], 2)
    return blocks


def probe_duration(path: str) -> float:
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float((out.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def write_srt(blocks: list[dict], path: Path) -> None:
    lines = []
    for i, b in enumerate(blocks, 1):
        lines += [str(i), f"{srt_time(b['start'])} --> {srt_time(b['end'])}", b["text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="faster-whisper 자막 생성")
    ap.add_argument("--voice", default=str(OUTPUT_DIR / "voice.mp3"))
    ap.add_argument("--output", default=str(OUTPUT_DIR / "subtitles.json"))
    ap.add_argument("--srt", default=str(OUTPUT_DIR / "subtitles.srt"))
    ap.add_argument("--max-chars", type=int, default=None)
    ap.add_argument("--language", default=None)
    ap.add_argument("--model", default=None, help="로컬 모델 디렉터리 또는 모델 크기(tiny/base/small/medium)")
    args = ap.parse_args()

    config = load_config()
    voice_path = Path(expand(args.voice))
    if not voice_path.exists() or voice_path.stat().st_size < 1000:
        print(f"[ERROR] 음성 파일이 없습니다: {voice_path}")
        print("        먼저 4단계(ElevenLabs TTS)를 완료하세요.")
        return 1

    audio_len = probe_duration(str(voice_path))
    if audio_len and audio_len < 0.8:
        print(f"[ERROR] 음성이 너무 짧습니다({audio_len:.2f}초). TTS 결과를 확인하세요.")
        return 1

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("[ERROR] faster-whisper 미설치: pip install faster-whisper")
        return 1

    language = args.language or (config.get("persona", {}) or {}).get("language", "ko-KR").split("-")[0]
    max_chars = args.max_chars or int((config.get("persona", {}) or {}).get("max_chars_per_line", 12))

    model_ref = resolve_model(config, args.model)
    if model_ref:
        print(f"[Whisper] 로컬 모델 사용: {model_ref}")
        model = WhisperModel(model_ref, device="cpu", compute_type="int8", local_files_only=True)
        model_label = Path(model_ref).name
    else:
        size = args.model or "small"
        print(f"[Whisper] 로컬 모델 없음 → '{size}' 모델 다운로드 시도")
        model = WhisperModel(size, device="cpu", compute_type="int8")
        model_label = f"hf:{size}"

    print(f"[Whisper] 전사 시작: {voice_path.name} (language={language}, word_timestamps=True)")
    segments, info = model.transcribe(
        str(voice_path), language=language, beam_size=5,
        word_timestamps=True, vad_filter=True,
    )

    words: list[dict] = []
    seg_texts: list[dict] = []
    for seg in segments:
        seg_texts.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()})
        for w in (seg.words or []):
            words.append({"word": w.word, "start": w.start, "end": w.end})

    if not words and not seg_texts:
        print("[ERROR] 자막이 0개입니다. 음성 내용/언어 설정을 확인하세요.")
        return 1

    blocks = group_words(words, max_chars) if words else seg_texts
    payload = {
        "purpose": "timing-only",
        "burn_in": False,
        "text_used_in_video": False,
        "warning": "tiny 모델의 text 는 정확도가 낮습니다. 영상/CapCut 에 글자로 넣지 마세요(기본값).",
        "language": info.language,
        "language_probability": round(getattr(info, "language_probability", 0.0) or 0.0, 3),
        "duration": round(audio_len or info.duration, 2),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": model_label,
        "max_chars_per_line": max_chars,
        "subtitles": blocks,
    }
    out_json = Path(expand(args.output))
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_srt(blocks, Path(expand(args.srt)))

    print(f"[OK] 타이밍 {len(blocks)}개 블록 / 단어 {len(words)}개 (영상에 글자로 넣지 않음)")
    print(f"     {out_json}")
    print(f"     {expand(args.srt)}")
    if model_label in ("hf:tiny", "hf:base"):
        print("[WARN] tiny/base 모델은 한국어 정확도가 낮습니다. small 이상을 권장합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
