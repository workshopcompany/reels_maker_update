"""
[단계 5] 너구리 장면 생성 (SDXL Turbo 480p → 1080p 업스케일)
=============================================================
승인된 대본을 8~14개 장면으로 나누고, 장면마다 다른 포즈/배경의 너구리 이미지를 만든다.
한 장면짜리 영상을 반복하지 않고, 45초 내외 쇼츠를 채우는 것이 목적이다.

실행 인터프리터: config.scenes.python (CUDA torch + diffusers 가 있는 파이썬)
  예: venv/Scripts/python.exe scripts_generate_scenes.py
  (이 프로젝트의 pycapcut venv 는 torch 가 CPU 라서 이미지 생성에 쓰지 않는다)

입력
- output/script.txt      (승인된 대본)
- output/voice.mp3       (장면 길이 배분 기준)
- config.json > scenes

출력
- output/scenes/base/scene_XX.png   480x856 원본 (SDXL)
- output/scenes/scene_XX.png        1080x1920 업스케일 (ffmpeg lanczos / Upscayl CLI)
- output/scenes/clean/scene_XX.png  말풍선 없는 깨끗한 업스케일 원본 (문구 지우기/다시 그리기용)
- output/scenes/scenes.json         장면 텍스트·시작/끝·프롬프트·이미지·모션·말풍선 문구
- output/wangp_jobs.json            (motion_count>0 이면) WanGP 로 움직일 장면 목록

말풍선(B-Roll 없이 만들 때)
- 장면별 bubble_text 가 있으면 업스케일된 이미지 위에 말풍선을 그리고, 없으면 그리지 않는다.
  (대본 글자를 굽는 것이 아니라, 사이트 주소 같은 '안내 문구'를 넣는 용도)
- 문구는 웹 '🖼 장면 · 말풍선'에서 장면마다 입력한다. 문구만 바꿀 때는
  `--bubbles-only` 로 이미지를 다시 생성하지 않고 말풍선만 다시 그린다.

정책
- SDXL 프롬프트에는 글자/말풍선을 넣지 않는다(모델이 글자를 잘 못 그린다). 말풍선은 코드로 그린다.
- 480p 로 생성해 업스케일한다. 1080p 직접 생성은 1080 Ti 에서 너무 느리다.
- SDXL 실패 시 가짜 이미지를 만들지 않고 실패로 끝낸다.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# 단편화로 인한 CUDA OOM 완화 (torch import 전에 설정해야 적용된다)
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

PROJECT = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT / "config.json"
OUTPUT_DIR = PROJECT / "output"
SCENES_DIR = OUTPUT_DIR / "scenes"
BASE_DIR = SCENES_DIR / "base"
CLEAN_DIR = SCENES_DIR / "clean"
SCENES_JSON = SCENES_DIR / "scenes.json"
WANGP_JOBS = OUTPUT_DIR / "wangp_jobs.json"
SCRIPT_FILE = OUTPUT_DIR / "script.txt"
VOICE_FILE = OUTPUT_DIR / "voice.mp3"

sys.path.insert(0, str(PROJECT))
import bubble_overlay  # noqa: E402  (PIL 전용 경량 모듈 — torch 를 쓰지 않는다)
import motion_plan  # noqa: E402  (표준 라이브러리 전용)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

DEFAULT_STYLE = (
    "2D webtoon style illustration, thick clean outlines, flat cel shading, "
    "bright saturated colors, simple colorful background"
)
DEFAULT_CHARACTER = (
    "cute chubby raccoon mascot character, brown fur, big dark eye mask, cream belly, "
    "striped tail, friendly smile, standing upright like a person, no clothes"
)
# CLIP 은 77 토큰을 넘으면 뒤쪽을 잘라버린다. 프롬프트를 짧게 유지하지 않으면
# 'full body'·'no text' 같은 제약이 사라져 머리가 잘리거나 글자가 생긴다.
PROMPT_WORDS_CHARACTER = 13
PROMPT_WORDS_VISUAL = 13
PROMPT_WORDS_STYLE = 10
# 부정 프롬프트는 guidance>0 일 때만 실제로 쓰인다(SDXL-turbo 기본값은 0).
DEFAULT_NEGATIVE = (
    "two heads, duplicate character, conjoined bodies, extra head, extra character, "
    "multiple raccoons, cropped head, cut off head, out of frame, text, letters, "
    "speech bubble, watermark, logo, blurry"
)
FALLBACK_ACTIONS = [
    "pointing at a floating holographic screen with an excited smile",
    "holding a small laptop and showing the screen to the viewer",
    "scratching its head with a puzzled but cute expression",
    "giving a big thumbs up with sparkling eyes",
    "holding a magnifying glass and leaning forward curiously",
    "shrugging with a playful wink",
    "running forward with a tiny rocket in its hand",
    "waving hello with a warm smile",
    "holding a coffee mug and explaining calmly",
    "presenting with both arms open, proud expression",
    "sitting at a desk with a keyboard, typing quickly",
    "holding a smartphone and pointing at it",
]
FALLBACK_BACKGROUNDS = [
    "cozy wooden workshop with tools",
    "bright city street with signs",
    "clean modern desk with a computer",
    "colorful gradient studio background",
    "small cafe table with a laptop",
    "night city rooftop with neon lights",
]


def expand(path: str) -> str:
    return os.path.normpath(os.path.expandvars(path))


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


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
    text = re.sub(r"\[[^\]\n]{1,24}\]", "", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


# ----------------------------------------------------------------------
# 장면 나누기 + 길이 배분
# ----------------------------------------------------------------------
def split_units(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", strip_style_tags(text))
    units = [p.strip() for p in parts if p.strip()]
    return units or [strip_style_tags(text)]


def split_long_unit(unit: str) -> list[str]:
    """긴 문장을 쉼표/공백 기준으로 둘로 나눈다."""
    for sep in (", ", "，", " "):
        if sep in unit:
            idx = max(1, len(unit) // 2)
            left = unit.rfind(sep, 0, idx)
            if left < len(unit) * 0.25:
                left = unit.find(sep, idx)
            if left > 0:
                return [unit[:left].strip(" ,"), unit[left + len(sep):].strip()]
    return [unit]


def plan_scene_texts(text: str, count: int) -> list[str]:
    units = split_units(text)
    guard = 0
    while len(units) < count and guard < 80:
        guard += 1
        idx = max(range(len(units)), key=lambda i: len(units[i]))
        if len(units[idx]) < 12:
            break
        pieces = split_long_unit(units[idx])
        if len(pieces) < 2:
            break
        units[idx:idx + 1] = pieces

    if len(units) <= count:
        return units

    # 문자 수가 균형을 이루도록 연속된 문장을 정확히 count 개 그룹으로 나눈다.
    lengths = [len(u) for u in units]
    total = sum(lengths) or 1
    prefix = [0]
    for n in lengths:
        prefix.append(prefix[-1] + n)

    groups: list[list[str]] = []
    start = 0
    for g in range(count):
        remaining_groups = count - g
        if remaining_groups == 1:
            end = len(units)
        else:
            max_end = len(units) - remaining_groups + 1  # 남은 그룹에 최소 1문장씩
            target_chars = total * (g + 1) / count
            best_end, best_diff = start + 1, None
            for end_candidate in range(start + 1, max_end + 1):
                diff = abs(prefix[end_candidate] - target_chars)
                if best_diff is None or diff < best_diff:
                    best_end, best_diff = end_candidate, diff
            end = best_end
        groups.append(units[start:end])
        start = end

    return [" ".join(g) for g in groups if g]


def plan_scenes(text: str, duration: float, cfg: dict) -> list[dict]:
    sc = cfg.get("scenes", {}) or {}
    scene_sec = float(sc.get("scene_seconds", 3.8) or 3.8)
    min_scenes = int(sc.get("min_scenes", 8) or 8)
    max_scenes = int(sc.get("max_scenes", 14) or 14)
    count = int(round(duration / scene_sec)) if duration > 0 else min_scenes
    count = max(min_scenes, min(max_scenes, count))

    texts = plan_scene_texts(text, count)
    count = len(texts)
    weights = [max(1, len(re.sub(r"\s+", "", t))) for t in texts]
    total_w = sum(weights)

    scenes: list[dict] = []
    cursor = 0.0
    for i, (t, w) in enumerate(zip(texts, weights), start=1):
        dur = duration * w / total_w
        start = cursor
        end = duration if i == count else start + dur
        cursor = end
        scenes.append({
            "index": i,
            "text": t,
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(max(0.5, end - start), 3),
        })
    return scenes


# ----------------------------------------------------------------------
# 장면 → 영어 이미지 프롬프트 (선택한 LLM 번역, 실패 시 기본 액션)
# ----------------------------------------------------------------------
def _scene_llm_generate(prompt: str, cfg: dict) -> str:
    """Ollama 가 켜져 있으면 Ollama, 아니면 웹에서 고른 API 로 번역한다."""
    llm = cfg.get("llm", {}) or {}
    if bool(llm.get("ollama_enabled", False)):
        out = _ollama_generate(prompt, cfg)
        if out:
            return out
    try:
        sys.path.insert(0, str(PROJECT))
        import llm_client  # noqa: WPS433

        pool = llm.get("provider") or "auto"
        text, meta = llm_client.generate(
            prompt + "\n\n영어 단어만, 설명 없이 25단어 이내로 답해라.",
            cfg,
            None if pool in ("auto", "") else pool,
        )
        print(f"[장면LLM] {meta.get('provider') or meta.get('backend')} · {meta.get('model')}")
        return text
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 장면 프롬프트 LLM 실패({type(e).__name__}) → 기본 액션으로 진행")
        return ""


def _ollama_generate(prompt: str, cfg: dict, timeout: int = 60) -> str:
    llm = cfg.get("llm", {}) or {}
    base = (llm.get("ollama_url") or os.getenv("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
    model = (llm.get("ollama_model") or "").strip()
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        installed = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return ""
    if not installed:
        return ""
    if not model or model not in installed:
        for pref in ("qwen3.5", "qwen3", "gemma3", "gemma", "llama"):
            model = next((n for n in installed if pref in n.lower()), "") or model
        model = model or installed[0]
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.4, "num_predict": 90},
    }
    req = urllib.request.Request(
        f"{base}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (json.loads(resp.read().decode("utf-8")).get("response") or "").strip()
    except Exception:
        return ""


def strip_character_mentions(text: str) -> str:
    """장면 설명에 너구리(캐릭터) 묘사가 또 들어가면 모델이 **너구리를 한 마리 더** 그린다.

    프롬프트 앞부분에 이미 캐릭터 설명이 있으므로, visual 에서는 캐릭터 언급을
    들어낸다(남는 것은 동작·소품·배경). 예: 머리 두 개 / 두 마리 사고 방지.
    """
    chunks = [c.strip() for c in str(text or "").split(",") if c.strip()]
    keep = [c for c in chunks if not re.search(r"raccoon|mascot|character", c, flags=re.I)]
    return ", ".join(keep) if keep else ""


def clean_english_prompt(text: str) -> str:
    text = text.strip().splitlines()[0] if text.strip() else ""
    text = re.sub(r"^(prompt|image prompt|answer|output|result)\s*:\s*", "", text, flags=re.I)
    # 화면에 글자가 들어가도록 유도하는 따옴표 구절은 제거한다(대본/자막은 굽지 않음).
    text = re.sub(r"\"[^\"]{1,60}\"|'[^']{1,60}'", "", text)
    text = text.strip(" \"'`")
    text = re.sub(r"\s+", " ", text)
    if re.search(r"[가-힣]", text):
        return ""
    if len(re.findall(r"[A-Za-z]", text)) < 15:
        return ""
    return text[:240].strip(" ,")


def _first_words(text: str, limit: int) -> str:
    words = re.sub(r"\s+", " ", str(text or "")).strip(" ,").split(" ")
    return " ".join(w for w in words[:limit] if w).strip(" ,")


def scene_prompt(scene: dict, cfg: dict, topic: str) -> tuple[str, str]:
    sc = cfg.get("scenes", {}) or {}
    character = (sc.get("character") or DEFAULT_CHARACTER).strip()
    style = (sc.get("style") or DEFAULT_STYLE).strip()
    ask = (
        "You write ONE English Stable Diffusion prompt for a vertical 9:16 shorts image.\n"
        "The mascot is a cute chubby 2D webtoon raccoon.\n"
        f"Topic: {topic}\n"
        f"Korean narration for this scene: {scene['text']}\n\n"
        "Describe only the raccoon's action/pose, props, and a simple vivid background. "
        "Max 28 words, comma separated, English only, no text/speech bubble/watermark. "
        "Do not repeat the character description.\nAnswer:"
    )
    translated = strip_character_mentions(clean_english_prompt(_scene_llm_generate(ask, cfg)))
    if translated:
        visual = translated
    else:
        i = scene["index"] - 1
        visual = f"{FALLBACK_ACTIONS[i % len(FALLBACK_ACTIONS)]}, {FALLBACK_BACKGROUNDS[i % len(FALLBACK_BACKGROUNDS)]}"

    # 순서가 중요하다: 제약(solo/full body)이 앞에 있어야 77토큰에서 잘리지 않는다.
    # 'solo, single character, one head' 는 같은 그림에 너구리가 두 마리(머리 두 개)로
    # 그려지는 사고를 줄이는 가장 효과적인 표현이다(부정어는 CFG=0 에서 무시됨).
    parts = {
        "head": "solo, single character, one head",
        "character": _first_words(character, PROMPT_WORDS_CHARACTER),
        "visual": _first_words(visual, PROMPT_WORDS_VISUAL),
        "style": _first_words(style, PROMPT_WORDS_STYLE),
        "tail": "full body, centered, vertical composition, no text, no speech bubble",
    }
    scene["prompt_parts"] = parts
    prompt = ", ".join(v for v in parts.values() if v)
    return prompt, visual


# ----------------------------------------------------------------------
# SDXL 480p 생성 → 1080p 업스케일
# ----------------------------------------------------------------------
def bubble_style(cfg: dict) -> dict:
    return ((cfg.get("scenes", {}) or {}).get("bubble") or {})


def apply_scene_bubble(scene: dict, cfg: dict, fresh_source: bool = False) -> None:
    """장면 이미지에 말풍선을 적용/제거한다(문구가 비면 깨끗한 원본으로 복원).

    fresh_source=True 면 방금 만든 이미지로 clean 원본을 **새로 덮어쓴다**.
    (예전 회차의 clean 원본이 남아 옛 이미지가 섞이는 사고 방지)
    """
    image = str(scene.get("image") or "")
    if not image:
        return
    text = str(scene.get("bubble_text") or "").strip()
    try:
        result = bubble_overlay.apply_to_scene(image, text, CLEAN_DIR, bubble_style(cfg),
                                              fresh_source=fresh_source)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] {scene.get('index')}번 말풍선 실패: {type(e).__name__}: {e}")
        return
    if result.get("error"):
        print(f"[WARN] {result['error']}")
        return
    if result.get("warning"):
        print(f"[WARN] {result['warning']}")
        return
    if text:
        print(f"[BUBBLE {int(scene.get('index') or 0):02d}] “{text[:32]}”")


def upscale_image(src: Path, dst: Path, cfg: dict) -> str:
    sc = cfg.get("scenes", {}) or {}
    out_w = int(sc.get("out_width", 1080) or 1080)
    out_h = int(sc.get("out_height", 1920) or 1920)
    upscaler = (sc.get("upscaler") or "auto").strip().lower()
    raw_bin = (sc.get("upscayl_bin") or "").strip()
    bin_path = expand(raw_bin) if raw_bin else ""

    if upscaler in ("auto", "upscayl") and bin_path and Path(bin_path).is_file():
        tmp_dir = SCENES_DIR / "_upscayl"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        model = (sc.get("upscayl_model") or "realesrgan-x4plus").strip()
        cmd = [bin_path, "-i", str(src), "-o", str(tmp_dir), "-s", "2",
               "-m", model, "-f", "png", "-n", f"up_{src.stem}"]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            produced = tmp_dir / f"up_{src.stem}.png"
            if produced.exists():
                src = produced
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Upscayl 실패({type(e).__name__}) → ffmpeg 로 업스케일")

    used = "upscayl" if "_upscayl" in str(src) else "ffmpeg"
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-vf", f"scale={out_w}:{out_h}:flags=lanczos,unsharp=5:5:0.5",
        "-frames:v", "1", str(dst),
    ]
    subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if not dst.exists() or dst.stat().st_size < 5000:
        raise RuntimeError(f"업스케일 실패: {dst.name}")
    return used


# ----------------------------------------------------------------------
# GPU 메모리 정리 (이미지 ↔ 영상 순차 사용)
# ----------------------------------------------------------------------
def gpu_memory() -> dict:
    """nvidia-smi 로 VRAM 상태(MB)를 읽는다. 없으면 빈 dict."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
        line = (out.stdout or "").strip().splitlines()[0]
        used, free, total = (int(x.strip()) for x in line.split(",")[:3])
        return {"used_mb": used, "free_mb": free, "total_mb": total}
    except Exception:
        return {}


def _listening_pid(port: int) -> int:
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 0
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line.upper():
            parts = line.split()
            if parts and parts[-1].isdigit():
                return int(parts[-1])
    return 0


def _process_cmdline(pid: int) -> str:
    try:
        ps = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=25)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _kill_pid(pid: int) -> bool:
    return subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                          capture_output=True, text=True).returncode == 0


def free_gpu_before_images(cfg: dict) -> dict:
    """이미지 생성 전 GPU를 비운다.

    WanGP 웹서버가 7860 에서 VRAM 을 잡고 있으면(명령줄에 wgp.py 확인) 종료한다.
    다른 앱이 잡고 있으면 죽이지 않고 경고만 한다.
    """
    sc = cfg.get("scenes", {}) or {}
    wp = cfg.get("wangp", {}) or {}
    before = gpu_memory()
    if before:
        print(f"[GPU] 시작 전 VRAM: 사용 {before['used_mb']}MB / 여유 {before['free_mb']}MB "
              f"(전체 {before['total_mb']}MB)")
    stopped: list[dict] = []
    if bool(sc.get("auto_stop_wangp", True)):
        port = urlparse(wp.get("base_url") or "http://127.0.0.1:7860").port or 7860
        pid = _listening_pid(port)
        if pid:
            cmd = _process_cmdline(pid)
            if "wgp.py" in cmd.lower():
                print(f"[GPU] WanGP 서버가 GPU를 쓰고 있습니다 (PID {pid}, {port}포트) → 이미지 생성을 위해 종료")
                if _kill_pid(pid):
                    for _ in range(20):
                        time.sleep(1)
                        if not _listening_pid(port):
                            break
                    stopped.append({"what": "wangp_server", "pid": pid})
            else:
                print(f"[WARN] {port} 포트는 WanGP 가 아닌 프로세스(PID {pid})가 사용 중입니다: {cmd[:120]}")
    after = gpu_memory()
    if after:
        print(f"[GPU] 정리 후 VRAM: 사용 {after['used_mb']}MB / 여유 {after['free_mb']}MB")
        if after["free_mb"] < 6000:
            print("[WARN] VRAM 여유가 6GB 미만입니다. 브라우저 GPU 가속·게임·다른 AI 앱을 끄면 안전합니다.")
    return {"before": before, "after": after, "stopped": stopped}


def fit_prompt(parts: dict, tokenizer, budget: int = 74) -> str:
    """CLIP 77토큰을 넘으면 뒤쪽(full body·no text…)이 잘려 머리가 잘리거나 글자가 생긴다.

    그래서 토큰 수를 직접 재서, 남는 부분(visual → character → style)을 뒤에서부터 덜어낸다.
    제약(head/tail)은 절대 자르지 않는다.
    """
    visual = str(parts.get("visual") or "").split()
    character = str(parts.get("character") or "").split()
    style = str(parts.get("style") or "").split()
    head, tail = str(parts.get("head") or ""), str(parts.get("tail") or "")

    def build() -> str:
        chunks = [head, " ".join(character), " ".join(visual), " ".join(style), tail]
        return ", ".join(c for c in chunks if c)

    def ntokens() -> int:
        try:
            return len(tokenizer(build(), truncation=False).input_ids)
        except TypeError:
            return len(tokenizer(build()).input_ids)

    for bucket in (visual, character, style):
        while bucket and ntokens() > budget:
            bucket.pop()
    text = build()
    if ntokens() > budget:   # 그래도 넘으면 토큰 단위로 자른다(최후 수단)
        ids = tokenizer(text).input_ids[:budget]
        text = tokenizer.decode(ids, skip_special_tokens=True)
    return text


def generate_images(scenes: list[dict], cfg: dict, seed_shift: int = 0) -> None:
    sc = cfg.get("scenes", {}) or {}
    model_path = expand(sc.get("model_path") or "models/sdxl-turbo")
    if not Path(model_path).exists():
        raise RuntimeError(f"SDXL 모델 폴더가 없습니다: {model_path}")
    gen_w = int(sc.get("gen_width", 480) or 480)
    gen_h = int(sc.get("gen_height", 856) or 856)
    if gen_w % 8:
        gen_w -= gen_w % 8
    if gen_h % 8:
        gen_h -= gen_h % 8
    steps = int(sc.get("steps", 4) or 4)
    guidance = float(sc.get("guidance", 0.0) or 0.0)
    base_seed = int(sc.get("base_seed", 20261024) or 20261024) + int(seed_shift or 0)
    negative = str(sc.get("negative") or DEFAULT_NEGATIVE).strip()
    if guidance <= 0:
        print("[SDXL] guidance=0 (turbo) → negative 프롬프트는 쓰이지 않습니다")

    import torch
    from diffusers import AutoPipelineForText2Image

    if not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA 를 쓸 수 없습니다. config.scenes.python 이 CUDA torch 가 있는 "
            "파이썬인지 확인하세요 (예: venv/Scripts/python.exe)."
        )

    # 이미지 생성 전에 GPU를 비운다 (WanGP 서버가 VRAM을 잡고 있으면 OOM)
    gpu_info = free_gpu_before_images(cfg)

    offload_cfg = sc.get("cpu_offload", "auto")
    free_mb = int((gpu_info.get("after") or {}).get("free_mb") or 0)
    use_offload = (free_mb < 6500) if str(offload_cfg).lower() == "auto" else bool(offload_cfg)

    t0 = time.time()
    print(f"[SDXL] 로드: {model_path} ({torch.cuda.get_device_name(0)})")
    pipe = AutoPipelineForText2Image.from_pretrained(
        model_path, torch_dtype=torch.float16, variant="fp16", local_files_only=True
    )
    if use_offload:
        try:
            pipe.enable_model_cpu_offload()
            print(f"[SDXL] VRAM 여유 {free_mb}MB → CPU offload 모드 (느리지만 OOM 방지)")
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] CPU offload 실패({type(e).__name__}) → GPU 직접 로드")
            pipe.to("cuda")
    else:
        pipe.to("cuda")
    try:
        pipe.enable_vae_tiling()
        pipe.enable_vae_slicing()
    except Exception:
        pass
    try:
        pipe.enable_attention_slicing()
    except Exception:
        pass
    try:
        pipe.set_progress_bar_config(disable=True)
    except Exception:
        pass
    print(f"[SDXL] 준비 완료 {time.time()-t0:.1f}s · {gen_w}x{gen_h} · steps={steps}")

    for scene in scenes:
        idx = scene["index"]
        base_path = BASE_DIR / f"scene_{idx:02d}.png"
        out_path = SCENES_DIR / f"scene_{idx:02d}.png"
        t = time.time()
        generator = torch.Generator(device="cuda").manual_seed(base_seed + idx * 97)
        prompt_text = scene["image_prompt"]
        if scene.get("prompt_parts"):
            prompt_text = fit_prompt(scene["prompt_parts"], pipe.tokenizer)
        ntok = len(pipe.tokenizer(prompt_text).input_ids)
        try:
            image = pipe(
                prompt=prompt_text,
                negative_prompt=negative if guidance > 0 else None,
                width=gen_w,
                height=gen_h,
                num_inference_steps=steps,
                guidance_scale=guidance,
                generator=generator,
            ).images[0]
        except torch.cuda.OutOfMemoryError as e:
            gc.collect()
            torch.cuda.empty_cache()
            raise RuntimeError(
                "CUDA OOM: GPU 메모리가 부족합니다. WanGP 서버/다른 GPU 앱을 끄고 "
                "config.scenes.cpu_offload=true 로 다시 실행하세요. "
                f"(원본 오류: {e})"
            ) from e
        base_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(base_path)
        how = upscale_image(base_path, out_path, cfg)
        scene["base_image"] = str(base_path)
        scene["image"] = str(out_path)
        # 말풍선은 업스케일된 최종 이미지 위에 그린다
        # clean/ 원본도 이번 이미지로 항상 새로 보관한다(옛 원본 재사용 금지)
        apply_scene_bubble(scene, cfg, fresh_source=True)
        print(f"[SCENE {idx:02d}/{len(scenes)}] {time.time()-t:.1f}s · {how} · {ntok}토큰 · "
              f"{scene['duration']:.1f}s · {scene['text'][:34]}")

    # 이미지가 끝나면 VRAM 을 완전히 돌려준다 (뒤이어 WanGP 영상이 GPU를 쓴다)
    try:
        pipe.to("cpu")
    except Exception:
        pass
    try:
        del pipe
    except Exception:
        pass
    gc.collect()
    try:
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()
    except Exception:
        pass
    print("[GPU] SDXL 메모리 해제 완료")


# ----------------------------------------------------------------------
# WanGP 모션 클립 작업 목록 (하이브리드)
# ----------------------------------------------------------------------
def plan_motion_jobs(scenes: list[dict], cfg: dict, indices: list[int] | None = None) -> list[dict]:
    """어떤 장면을 움직일지 정한다 (motion_plan 공용 모듈).

    모션 입력 이미지는 반드시 말풍선 없는 clean 원본을 쓴다. WanGP 가 이미지를 다시
    그리기 때문에 말풍선을 넣은 이미지로 만들면 글자가 뭉개진다.
    """
    return motion_plan.build_jobs(scenes, cfg, indices)


# ----------------------------------------------------------------------
def load_prev_bubbles() -> dict[str, str]:
    """기존 scenes.json 의 {대본 문장: 말풍선 문구} 맵."""
    try:
        data = json.loads(SCENES_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, str] = {}
    for scene in data.get("scenes") or []:
        bubble = str(scene.get("bubble_text") or "").strip()
        text = str(scene.get("text") or "").strip()
        if bubble and text:
            out[text] = bubble
    return out


def bubbles_only(cfg: dict) -> int:
    """scenes.json 의 bubble_text 만 반영해 이미지에 다시 그린다(이미지 재생성 없음)."""
    try:
        data = json.loads(SCENES_JSON.read_text(encoding="utf-8"))
    except Exception:
        print("[ERROR] output/scenes/scenes.json 이 없습니다. 5단계를 먼저 실행하세요.")
        return 1
    scenes = [s for s in (data.get("scenes") or []) if s.get("image")]
    if not scenes:
        print("[ERROR] 장면 이미지가 없습니다.")
        return 1
    drew = 0
    for scene in scenes:
        apply_scene_bubble(scene, cfg)
        if str(scene.get("bubble_text") or "").strip():
            drew += 1
    SCENES_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 말풍선 적용 {drew}/{len(scenes)}장면 (나머지는 말풍선 없음)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="너구리 장면 이미지 생성 (SDXL 480p → 1080p)")
    ap.add_argument("--count", type=int, default=0, help="장면 수 강제 (0=자동)")
    ap.add_argument("--no-motion", action="store_true", help="WanGP 모션 작업 목록을 만들지 않음")
    ap.add_argument("--only", type=int, default=0, help="특정 장면만 다시 생성")
    ap.add_argument("--seed-shift", type=int, default=0,
                    help="시드를 옮겨 같은 장면을 다른 그림으로 다시 생성 (--only 와 함께 사용)")
    ap.add_argument("--bubbles-only", action="store_true",
                    help="이미지를 다시 만들지 않고 scenes.json 의 말풍선 문구만 다시 그리기/지우기")
    args = ap.parse_args()

    cfg = load_config()
    sc = cfg.get("scenes", {}) or {}

    if args.bubbles_only:
        return bubbles_only(cfg)

    if not bool(sc.get("enabled", True)):
        print("[SKIP] config.scenes.enabled=false")
        return 0

    if not SCRIPT_FILE.exists():
        print("[ERROR] output/script.txt 가 없습니다. 3단계 대본을 승인하세요.")
        return 1
    if not VOICE_FILE.exists():
        print("[ERROR] output/voice.mp3 가 없습니다. 4단계 TTS 를 먼저 실행하세요.")
        return 1

    duration = probe_duration(VOICE_FILE)
    if duration < 1.0:
        print(f"[ERROR] 음성 길이가 비정상입니다({duration:.2f}s).")
        return 1

    text = SCRIPT_FILE.read_text(encoding="utf-8").strip()
    if not text:
        print("[ERROR] 대본이 비어 있습니다.")
        return 1

    if args.count and args.count > 0:
        sc["min_scenes"] = args.count
        sc["max_scenes"] = args.count
        cfg["scenes"] = sc
        scenes = plan_scenes(text, duration, cfg)
    else:
        scenes = plan_scenes(text, duration, cfg)

    # 이미 입력해 둔 말풍선 문구는 '같은 대본 문장'에 한해 이어서 쓴다.
    prev_bubbles = load_prev_bubbles()
    for scene in scenes:
        scene["bubble_text"] = prev_bubbles.get(scene["text"], "")

    topic = ""
    meta = {}
    try:
        meta = json.loads((OUTPUT_DIR / "script_meta.json").read_text(encoding="utf-8"))
        topic = str((meta.get("trend") or {}).get("title") or meta.get("keyword") or "")
    except Exception:
        pass
    if not topic:
        topic = "trending tech topic"

    print("=" * 70)
    print(f"[장면] 음성 {duration:.1f}s → {len(scenes)}장면 (평균 {duration/len(scenes):.1f}s)")
    print(f"[장면] 스타일: {str(sc.get('style') or DEFAULT_STYLE)[:70]}")
    print("=" * 70)

    for scene in scenes:
        prompt, visual = scene_prompt(scene, cfg, topic)
        scene["image_prompt"] = prompt
        scene["visual"] = visual

    SCENES_DIR.mkdir(parents=True, exist_ok=True)
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    keep_plan: list[int] = []
    if args.only:
        target = [s for s in scenes if s["index"] == args.only]
        if not target:
            print(f"[ERROR] {args.only}번 장면이 없습니다.")
            return 1
        # 한 장면만 다시 만들 때 웹에서 고른 모션 계획을 기본값으로 되돌리지 않는다
        keep_plan = motion_plan.planned_indices(WANGP_JOBS)
        generate_images(target, cfg, seed_shift=args.seed_shift)
        old = {}
        try:
            old = json.loads(SCENES_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
        by_idx = {s["index"]: s for s in (old.get("scenes") or [])}
        by_idx.update({s["index"]: s for s in target})
        scenes = [by_idx[i] for i in sorted(by_idx)]
    else:
        generate_images(scenes, cfg, seed_shift=args.seed_shift)

    if args.no_motion:
        jobs = []
    elif args.only and keep_plan:
        jobs = plan_motion_jobs(scenes, cfg, keep_plan)
    else:
        jobs = plan_motion_jobs(scenes, cfg)
    job_by_scene = {j["scene_index"]: j for j in jobs}
    for scene in scenes:
        job = job_by_scene.get(scene["index"]) or {}
        scene["motion"] = bool(job)
        scene["motion_image"] = job.get("image", "")
        scene["motion_clip"] = ""

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "voice_duration": round(duration, 3),
        "target_sec": float(sc.get("target_sec", 45) or 45),
        "count": len(scenes),
        "character": sc.get("character") or DEFAULT_CHARACTER,
        "style": sc.get("style") or DEFAULT_STYLE,
        "scenes": scenes,
    }
    SCENES_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    WANGP_JOBS.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 70)
    print(f"[OK] 장면 {len(scenes)}개 · {duration:.1f}초")
    print(f"     {SCENES_JSON}")
    print(f"     모션(WanGP) 예정 {len(jobs)}장면: {', '.join(str(j['scene_index']) for j in jobs) or '없음'}")
    print("     (웹 '🎬 WanGP → 장면별 모션'에서 장면을 바꿀 수 있습니다)")
    print("     모션 이미지: 말풍선 없는 clean 원본 · 말풍선은 10단계 조립에서 영상 위에 얹습니다")
    print("[NEXT] 6단계 B-Roll → 7단계 WanGP(모션) → 10단계 미리보기")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"[ERROR] 장면 생성 실패: {type(e).__name__}: {e}")
        sys.exit(1)
