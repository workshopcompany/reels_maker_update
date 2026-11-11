"""
너구리 쇼츠 - 파이프라인 코어
================================
웹 UI(web_pipeline.py)와 CLI 러너(pipeline_runner.py)가 함께 사용하는
단계 정의 / 상태 저장 / 실행 엔진.

설계 원칙
- 각 단계는 독립 스크립트로 실행되고, 종료 코드 0 + 산출물 존재일 때만 'done'.
- 실패는 숨기지 않는다. (가짜 음성/가짜 대본을 만들지 않음)
- 상태는 output/pipeline_state.json 에 저장해 웹/CLI/배치가 같은 진행률을 본다.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT / "output"
STATE_FILE = OUTPUT_DIR / "pipeline_state.json"
LOG_FILE = OUTPUT_DIR / "pipeline.log"
CONFIG_FILE = PROJECT / "config.json"
TRENDS_FILE = OUTPUT_DIR / "trends.json"
TRENDS_CONFIRM_FILE = OUTPUT_DIR / "trends_confirmed.json"

MAX_LOG_LINES = 800
# 산출물이 '이번 실행'에서 갱신됐다고 볼 시간 여유(초)
FRESH_SLACK_SEC = 15
# TTS 음성 최소 길이 / 미리보기 MP4 최소 크기
MIN_VOICE_SEC = 3.0
MIN_PREVIEW_BYTES = 30_000

_LOCK = threading.RLock()
_running: set[str] = set()
# 실행 중인 자식 프로세스(중단 버튼/프로세스 사망 감지에 사용)
_procs: dict[str, subprocess.Popen] = {}
# 사용자가 중단을 요청한 단계 (종료 코드 대신 '중단'으로 표시하려고 기억)
_stopped: set[str] = set()


# ----------------------------------------------------------------------
# 기본 유틸
# ----------------------------------------------------------------------
def expand(path: str) -> str:
    return os.path.normpath(os.path.expandvars(path))


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def cfg_path(*keys, default=None):
    """config.json 안전 탐색: cfg_path('paths', 'output_dir')"""
    node = load_config()
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def python_exe() -> str:
    """파이프라인 스크립트를 실행할 파이썬.
    RACCOON_PYTHON > config.paths.venv > 현재 인터프리터 순서.
    (웹 UI를 시스템 파이썬으로 띄워도 단계는 venv 패키지로 실행되게 한다)"""
    env_override = os.getenv("RACCOON_PYTHON")
    if env_override and Path(env_override).exists():
        return env_override
    try:
        venv = ((load_config().get("paths", {}) or {}).get("venv") or "").strip()
        if venv:
            cand = Path(expand(venv)) / "Scripts" / "python.exe"
            if cand.exists():
                return str(cand)
    except Exception:
        pass
    return sys.executable


def step_python(step: dict) -> str:
    """단계별 인터프리터. WanGP 단계는 WanGP 전용 가상환경 파이썬이 필요하다."""
    if step.get("python") == "wangp":
        exe = ((load_config().get("wangp", {}) or {}).get("python") or "").strip()
        if exe and Path(expand(exe)).exists():
            return expand(exe)
        print(f"[WARN] WanGP 파이썬을 찾지 못했습니다({exe}). 현재 인터프리터로 시도합니다.")
    if step.get("python") == "scenes":
        exe = ((load_config().get("scenes", {}) or {}).get("python") or "").strip()
        if exe and Path(expand(exe)).exists():
            return expand(exe)
        print(f"[WARN] 장면용 파이썬을 찾지 못했습니다({exe}). 현재 인터프리터로 시도합니다.")
    return python_exe()


# ----------------------------------------------------------------------
# 단계 정의
# ----------------------------------------------------------------------
STEPS = [
    {
        "id": "channel",
        "num": 1,
        "name": "채널 분석",
        "desc": "내 유튜브 채널의 최근 영상·조회수·업로드 패턴·주제 키워드를 수집합니다.",
        "module": "scripts_channel_analysis.py",
        "args": [],
        "requires": [],
        "optional": False,
        "artifacts": ["output/channel_analysis.json", "output/channel_analysis.md"],
    },
    {
        "id": "trends",
        "num": 2,
        "name": "트렌드 수집 + 선택",
        "desc": "YouTube(Data API, 실패 시 yt-dlp)·GitHub·HuggingFace 후보를 모아 점수를 매기고, 선택한 후보와 이유를 기록합니다.",
        "module": "scripts_fetch_trends.py",
        "args": [],
        "requires": ["channel"],
        "optional": True,  # 채널 분석이 없어도 키워드만으로 수집 가능
        "artifacts": ["output/trends.json", "output/trends_report.md"],
    },
    {
        "id": "script",
        "num": 3,
        "name": "대본 초안 (너구리 페르소나)",
        "desc": "선택된 트렌드 + 채널 스타일로 260~350자 대본 초안을 만듭니다. 어떤 LLM(Gemini/Groq/OpenRouter/Freebuff)을 쓸지는 웹 '🧠 대본 LLM'에서 고릅니다(기본: API, Ollama는 꺼짐).",
        "module": "scripts_generate_script.py",
        "args": ["--draft-only"],
        "requires": ["trends"],
        "optional": False,
        "artifacts": ["output/script_draft.txt", "output/script_meta.json"],
    },
    {
        "id": "tts",
        "num": 4,
        "name": "음성 생성 (ElevenLabs TTS)",
        "desc": "승인된 대본(output/script.txt)으로 voice.mp3를 생성합니다.",
        "module": "scripts_generate_script.py",
        "args": ["--tts-only"],
        "requires": ["script"],
        "optional": False,
        "artifacts": ["output/voice.mp3"],
    },
    {
        "id": "scenes",
        "num": 5,
        "name": "너구리 장면 생성 (SDXL 480p→1080p)",
        "desc": "승인 대본을 8~14장면으로 나누고, 장면마다 다른 포즈의 너구리 이미지를 만듭니다. 웹 '🖼 장면 · 말풍선'에서 문구를 넣으면 그 장면에 말풍선이 그려집니다(B-Roll 없이 만들 때 화면 안내용). 생성 전 WanGP 서버를 종료해 VRAM을 비우고, 끝나면 모델을 해제합니다.",
        "module": "scripts_generate_scenes.py",
        "args": [],
        "python": "scenes",
        "requires": ["tts"],
        "optional": True,  # SDXL 환경이 없으면 기존 단일 WanGP 경로로 폴백
        "artifacts": ["output/scenes/scenes.json", "output/scenes/scene_*.png"],
        "artifact_limit": 30,   # 장면 이미지는 전부 보여준다(웹에서 스크롤)
    },
    {
        "id": "broll",
        "num": 6,
        "name": "B-Roll 준비 (녹화/창 선택/MP4/사용 안 함)",
        "desc": "전체 화면 또는 선택한 창을 ffmpeg gdigrab 으로 녹화하거나, 업로드한 MP4를 B-Roll 로 씁니다. 웹에서 '사용 안 함'을 고르면 6단계를 건너뛰고 장면 전체 화면 + 말풍선으로 만듭니다.",
        "module": "scripts_capture_broll.py",
        "args": [],
        "requires": ["tts"],
        "optional": True,
        "artifacts": ["output/broll_meta.json", r"%USERPROFILE%\릴스자료\broll_*.mp4"],
    },
    {
        "id": "wangp",
        "num": 7,
        "name": "WanGP 움직임 생성 (GPU, 선택)",
        "desc": "웹 '장면별 모션'에서 고른 장면(기본 2개)을 WanGP img2video 로 짧게 움직입니다. 말풍선이 없는 원본으로 생성하고, 말풍선은 10단계 조립에서 영상 위에 얹습니다. 실패해도 정지 이미지+줌/팬으로 진행됩니다.",
        "module": "scripts_generate_wangp_motion.py",
        "args": [],
        "python": "wangp",
        "requires": ["scenes"],
        "optional": True,
        "artifacts": ["output/wangp_result.json"],
    },
    {
        "id": "subtitles",
        "num": 8,
        "name": "발화 타이밍 (Whisper tiny)",
        "desc": "voice.mp3에서 말이 나오는 위치(시작/끝)만 뽑습니다. tiny 받아쓰기는 정확도가 낮아 영상 자막으로 굽지 않습니다.",
        "module": "scripts_generate_subtitles.py",
        "args": [],
        "requires": ["tts"],
        "optional": False,
        "artifacts": ["output/subtitles.json", "output/subtitles.srt"],
    },
    {
        "id": "capcut",
        "num": 9,
        "name": "CapCut 초안 생성 (pycapcut)",
        "desc": "10단계 미리보기 MP4 와 같은 장면 프레임(장면별 B-Roll·말풍선 포함)을 한 트랙에 이어 붙인 pycapcut 드래프트를 만듭니다. 음성/효과음 트랙을 함께 넣고, Whisper 텍스트 트랙은 기본 생략.",
        "module": "scripts_build_capcut_draft.py",
        "args": [],
        "requires": ["tts", "subtitles"],
        "optional": False,
        "artifacts": ["output/capcut_result.json"],
    },
    {
        "id": "assemble",
        "num": 10,
        "name": "미리보기 MP4 (FFmpeg)",
        "desc": "장면마다 [상단 B-Roll 칸 + 하단 너구리 칸] 또는 [장면 전체 화면]을 미리 합성해 9:16 쇼츠 MP4를 만듭니다. 장면 맞춤(머리 기준/전체 보이기)·B-Roll 칸 높이는 웹 🎥 B-Roll 소스에서 저장하며, 9단계 CapCut 초안도 같은 프레임을 씁니다.",
        "module": "scripts_assemble_shorts.py",
        "args": [],
        "requires": ["tts", "subtitles"],
        "optional": True,
        "artifacts": ["output/final/shorts_final_*.mp4"],
    },
    {
        "id": "upload",
        "num": 11,
        "name": "유튜브 업로드 (선택)",
        "desc": "완성된 미리보기 MP4를 YouTube에 업로드합니다(제목·설명은 youtube_meta.json 의 별도 생성 문구). config.youtube.enabled=true 일 때만 자동 실행.",
        "module": "scripts_publish_youtube.py",
        "args": ["--latest", "--yes"],
        "requires": ["assemble"],
        "optional": True,
        "artifacts": ["output/_published.json"],
    },
]

STEP_BY_ID = {s["id"]: s for s in STEPS}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------
# 상태 저장
# ----------------------------------------------------------------------
def _blank_step() -> dict:
    return {
        "status": "pending",  # pending | running | done | failed | skipped
        "started_at": "",
        "finished_at": "",
        "returncode": None,
        "error": "",
        "log": [],
    }


def blank_state() -> dict:
    return {
        "mode": "manual",
        "created_at": _now(),
        "updated_at": _now(),
        "steps": {s["id"]: _blank_step() for s in STEPS},
    }


def _atomic_write(path: Path, payload: dict) -> None:
    """원자적 쓰기. 웹 UI가 같은 파일을 읽는 중이면 Windows 에서 replace 가
    PermissionError(WinError 5)로 막힐 수 있어 잠시 재시도한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    for attempt in range(12):
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    # 마지막 수단: 임시 파일을 포기하고 직접 덮어쓴다
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    path.write_text(text, encoding="utf-8")


def load_state() -> dict:
    with _LOCK:
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                state = blank_state()
        else:
            state = blank_state()
        # 스키마 보정 (단계 추가/변경 대응)
        state.setdefault("steps", {})
        state.setdefault("mode", "manual")
        for s in STEPS:
            state["steps"].setdefault(s["id"], _blank_step())
            state["steps"][s["id"]].setdefault("log", [])
        return state


def save_state(state: dict) -> None:
    with _LOCK:
        state["updated_at"] = _now()
        _atomic_write(STATE_FILE, state)


def _pid_alive(pid: int | None) -> bool:
    """프로세스가 살아 있는지(다른 프로세스가 띄운 단계까지 고려)."""
    if not pid:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                                 capture_output=True, text=True, timeout=15)
            return str(int(pid)) in (out.stdout or "")
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def recover_interrupted() -> int:
    """이전 프로세스가 남긴 running 상태를 실패로 정리한다.

    웹서버가 강제 종료되면(메모리 부족·창 닫기·재부팅) 자식 스크립트도 같이 죽지만
    state 파일에는 status="running" 이 남는다. 그대로 두면 UI 가 영원히 '실행 중…'
    에서 멈추고 다시 실행할 수도 없다(초기화 버튼도 running 에서는 막혀 있음).
    지금 이 프로세스가 실제로 돌리고 있지 않은 running 단계는 '중단됨'으로 정리한다.
    """
    with _LOCK:
        tracked = set(_running)
    state = load_state()
    changed = 0
    for sid, st in state["steps"].items():
        if st.get("status") != "running" or sid in tracked:
            continue
        proc = _procs.get(sid)
        if proc is not None and proc.poll() is None:
            continue  # 아직 살아 있는 자식은 건드리지 않는다
        if _pid_alive(st.get("pid")):
            continue  # CLI 러너/스케줄러가 띄운 단계일 수 있다 → 살아 있으면 놔둔다
        st["status"] = "failed"
        st["error"] = "실행이 중간에 끊겼습니다(서버/프로세스 종료). 다시 실행하세요."
        st["finished_at"] = _now()
        st.setdefault("log", []).append(
            f"[{datetime.now().strftime('%H:%M:%S')}] ✖ 실행 중 프로세스가 사라져 '중단됨'으로 정리했습니다"
        )
        changed += 1
    if changed:
        save_state(state)
    return changed


def stop_step(step_id: str) -> bool:
    """실행 중인 단계의 자식 프로세스를 강제 종료하고 '중단됨'으로 표시."""
    proc = _procs.get(step_id)
    with _LOCK:
        active = step_id in _running
    if proc is None and not active:
        return False
    _stopped.add(step_id)
    if proc is not None and proc.poll() is None:
        try:
            if os.name == "nt":
                # 자식(ffmpeg/python)까지 함께 정리
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=20)
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _append_log(step_id, "⏹ 사용자가 실행을 중단했습니다")
    state = load_state()
    st = state["steps"].get(step_id)
    if st is not None and st.get("status") == "running":
        st["status"] = "failed"
        st["error"] = "사용자가 중단했습니다."
        st["finished_at"] = _now()
        save_state(state)
    return True


def reset_step(step_id: str) -> None:
    state = load_state()
    if step_id in state["steps"]:
        state["steps"][step_id] = _blank_step()
        save_state(state)
    if step_id == "trends":
        clear_trends_confirmation()


def reset_all() -> None:
    state = load_state()
    state["steps"] = {s["id"]: _blank_step() for s in STEPS}
    state.pop("pause", None)
    save_state(state)
    clear_trends_confirmation()


# ----------------------------------------------------------------------
# 트렌드 확정 게이트 (자동화 전 사람 확인)
# ----------------------------------------------------------------------
def selected_trend_id() -> str:
    data = read_json("output/trends.json") or {}
    return str(data.get("selected_id") or "")


def trends_confirmation() -> dict:
    data = read_json("output/trends_confirmed.json") or {}
    return data if isinstance(data, dict) else {}


def is_trends_confirmed() -> bool:
    """현재 trends.json 의 선택과 같은 후보를 사람이 확정했는지."""
    data = read_json("output/trends.json") or {}
    if not data.get("selected"):
        return False
    conf = trends_confirmation()
    sid = str(data.get("selected_id") or "")
    return bool(conf) and str(conf.get("selected_id") or "") == sid


def confirm_trends(selected_id: str | None = None, keyword: str = "", note: str = "") -> dict:
    data = read_json("output/trends.json") or {}
    sid = selected_id or str(data.get("selected_id") or "")
    payload = {
        "selected_id": sid,
        "keyword": keyword or str(data.get("selected_keyword") or ""),
        "confirmed_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
    }
    _atomic_write(TRENDS_CONFIRM_FILE, payload)
    clear_pause()
    return payload


def clear_trends_confirmation() -> None:
    try:
        TRENDS_CONFIRM_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def set_pause(reason: str, step: str = "trends", message: str = "") -> None:
    state = load_state()
    state["pause"] = {
        "reason": reason,
        "step": step,
        "message": message,
        "at": _now(),
    }
    save_state(state)


def clear_pause() -> None:
    state = load_state()
    if state.pop("pause", None) is not None:
        save_state(state)


def set_mode(mode: str) -> None:
    state = load_state()
    state["mode"] = "auto" if mode == "auto" else "manual"
    save_state(state)


# ----------------------------------------------------------------------
# 산출물 / 선행 조건
# ----------------------------------------------------------------------
def resolve_artifacts(step: dict, limit: int = 6) -> list[dict]:
    items: list[Path] = []
    for pattern in step.get("artifacts", []):
        full = expand(pattern)
        p = Path(full)
        if p.is_absolute():
            matches = sorted(p.parent.glob(p.name), key=lambda x: x.stat().st_mtime, reverse=True)
        else:
            matches = sorted(PROJECT.glob(pattern), key=lambda x: x.stat().st_mtime, reverse=True)
        items.extend([m for m in matches if m.is_file()])
    seen, out = set(), []
    for m in sorted(items, key=lambda x: x.stat().st_mtime, reverse=True):
        key = str(m)
        if key in seen:
            continue
        seen.add(key)
        out.append({"path": str(m), "name": m.name, "size": m.stat().st_size, "mtime": m.stat().st_mtime})
        if len(out) >= limit:
            break
    return out


def prereq_status(step: dict) -> tuple[bool, str]:
    state = load_state()
    for req in step.get("requires", []):
        if state["steps"].get(req, {}).get("status") != "done":
            return False, f"'{STEP_BY_ID[req]['name']}' 단계를 먼저 완료하세요."
    if step["id"] == "tts":
        script = PROJECT / "output" / "script.txt"
        if not script.exists() or script.stat().st_size < 10:
            return False, "승인된 대본(output/script.txt)이 없습니다. 3단계에서 초안을 만들고 승인하세요."
    return True, ""


# ----------------------------------------------------------------------
# 산출물 검증 (가짜 완료 방지)
# ----------------------------------------------------------------------
def validate_step_output(step: dict, artifacts: list[dict], started_epoch: float) -> str | None:
    """이번 실행이 실제로 쓸만한 산출물을 남겼는지 확인. 문제가 있으면 사유 문자열 반환."""
    if step.get("artifacts") and not artifacts:
        return "산출물이 없습니다."

    fresh = [a for a in artifacts if a["mtime"] >= started_epoch - FRESH_SLACK_SEC]
    if step.get("artifacts") and not fresh:
        return "산출물이 이번 실행에서 갱신되지 않았습니다(예전 파일)."

    sid = step["id"]

    if sid == "trends":
        data = read_json("output/trends.json") or {}
        if not data.get("candidates"):
            return "trends.json 후보가 0개입니다."
        if not data.get("selected"):
            return "선택된 트렌드가 없습니다."

    elif sid == "script":
        draft = OUTPUT_DIR / "script_draft.txt"
        if not draft.exists() or len(draft.read_text(encoding="utf-8", errors="replace").strip()) < 40:
            return "대본 초안이 비었습니다."

    elif sid == "tts":
        meta = read_json("output/voice_meta.json") or {}
        duration = float(meta.get("duration_sec") or 0)
        if duration < MIN_VOICE_SEC:
            return f"음성 길이가 비정상입니다({duration:.2f}초)."
        script = OUTPUT_DIR / "script.txt"
        expected = hashlib.sha256(script.read_text(encoding="utf-8").strip().encode("utf-8")).hexdigest() \
            if script.exists() else ""
        if meta.get("script_sha") and expected and meta["script_sha"] != expected:
            return "승인된 대본과 음성이 일치하지 않습니다(대본이 수정됨). 4단계를 다시 실행하세요."

    elif sid == "broll":
        if not bool((load_config().get("broll", {}) or {}).get("enabled", True)):
            return "B-Roll 사용 안 함으로 설정되어 있습니다(웹 'B-Roll 소스'에서 사용 안 함 선택)."
        meta = read_json("output/broll_meta.json") or {}
        out = Path(expand(str(meta.get("output") or "")))
        # 정지 화면 녹화는 압축률이 높아 수 KB 밖에 안 될 수 있다
        if not meta.get("output") or not out.exists() or out.stat().st_size < 2_000:
            return "B-Roll 결과 영상이 없거나 너무 작습니다."

    elif sid == "wangp":
        data = read_json("output/wangp_result.json") or {}
        if not data.get("success"):
            return "WanGP 생성 실패: " + (str(data.get("error") or "이유 없음")[:200])
        files = [f for f in (data.get("files") or []) if Path(f).exists()]
        if not files:
            return "WanGP 결과 MP4 가 없습니다."

    elif sid == "subtitles":
        data = read_json("output/subtitles.json") or {}
        if len(data.get("subtitles") or []) == 0:
            return "자막이 0개입니다(음성에 말이 없거나 Whisper 실패)."

    elif sid == "capcut":
        data = read_json("output/capcut_result.json") or {}
        if not data.get("draft_dir") or float(data.get("duration_sec") or 0) <= 0:
            return "CapCut 초안 결과가 비정상입니다."
        if not Path(data["draft_dir"]).exists():
            return "CapCut 드래프트 폴더가 없습니다."

    elif sid == "assemble":
        videos = [a for a in fresh if a["name"].lower().endswith(".mp4")]
        if not videos:
            return "미리보기 MP4가 없습니다."
        if max(a["size"] for a in videos) < MIN_PREVIEW_BYTES:
            return "미리보기 MP4가 너무 작습니다(빈 영상 의심)."

    return None


def _invalidate_downstream(step_id: str) -> None:
    """선행 단계를 다시 실행하면, 그 결과를 쓰던 후속 단계는 다시 '대기'로 돌린다."""
    def dependents(sid: str) -> list[str]:
        return [s["id"] for s in STEPS if sid in (s.get("requires") or [])]

    state = load_state()
    stack, seen, changed = dependents(step_id), set(), False
    while stack:
        sid = stack.pop()
        if sid in seen or is_running(sid):
            continue
        seen.add(sid)
        st = state["steps"][sid]
        if st.get("status") in ("done", "failed", "skipped"):
            st["status"] = "pending"
            st["error"] = ""
            st["returncode"] = None
            st["started_at"] = ""
            st["finished_at"] = ""
            st.setdefault("log", []).append(
                f"[{datetime.now().strftime('%H:%M:%S')}] ↺ 선행 단계({STEP_BY_ID[step_id]['name']}) "
                f"재실행으로 초기화"
            )
            changed = True
        stack.extend(dependents(sid))
    if changed:
        save_state(state)


def next_step_after(step_id: str) -> str | None:
    ids = [s["id"] for s in STEPS]
    if step_id not in ids:
        return None
    i = ids.index(step_id) + 1
    return ids[i] if i < len(ids) else None


def is_running(step_id: str | None = None) -> bool:
    with _LOCK:
        if step_id:
            return step_id in _running
        return bool(_running)


# ----------------------------------------------------------------------
# 실행
# ----------------------------------------------------------------------
def _append_log(step_id: str, line: str) -> None:
    line = line.rstrip("\n")
    if not line:
        return
    with _LOCK:
        state = load_state()
        log = state["steps"][step_id].setdefault("log", [])
        log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {line}")
        if len(log) > MAX_LOG_LINES:
            del log[: len(log) - MAX_LOG_LINES]
        save_state(state)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{_now()}] [{step_id}] {line}\n")
    except Exception:
        pass


def run_step(step_id: str, timeout: int | None = None, extra_args: list[str] | None = None,
             extra_env: dict | None = None) -> bool:
    """단일 단계 실행 (블로킹). 성공 여부 반환.

    extra_env: 한글 같은 비ASCII 문자열을 인자로 넘기면 Windows 에서 '?' 로 깨지므로
    (argv 는 ANSI 코드페이지로 해석됨) 환경변수로 전달할 때 쓴다.
    """
    step = STEP_BY_ID.get(step_id)
    if not step:
        raise ValueError(f"알 수 없는 단계: {step_id}")

    with _LOCK:
        if step_id in _running:
            return False
        _running.add(step_id)
    _stopped.discard(step_id)

    if step_id == "trends":
        # 새로 수집/선택하기 전에는 이전 확정을 무효화한다.
        clear_trends_confirmation()
        clear_pause()

    started_epoch = time.time()
    state = load_state()
    state["steps"][step_id] = _blank_step()
    state["steps"][step_id]["status"] = "running"
    state["steps"][step_id]["started_at"] = _now()
    save_state(state)
    _append_log(step_id, f"▶ {step['num']}. {step['name']} 시작")

    script_path = PROJECT / step["module"]
    if not script_path.exists():
        state = load_state()
        state["steps"][step_id].update(
            status="failed", finished_at=_now(),
            error=f"스크립트 없음: {script_path.name}",
        )
        save_state(state)
        with _LOCK:
            _running.discard(step_id)
        return False

    interpreter = step_python(step)
    _append_log(step_id, f"python: {interpreter}")
    cmd = [interpreter, str(script_path)] + list(step.get("args", [])) + list(extra_args or [])
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["RACCOON_STEP"] = step_id
    env.update({k: str(v) for k, v in (extra_env or {}).items()})

    if timeout is None:
        timeout = int((load_config().get("pipeline", {}) or {}).get("step_timeout_sec", 1800))

    rc = -1
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        _procs[step_id] = proc
        # 다른 프로세스(웹/CLI)가 상태를 보고 있어도 "살아 있음"을 알 수 있게 PID 를 남긴다
        _snap = load_state()
        if step_id in _snap["steps"]:
            _snap["steps"][step_id]["pid"] = proc.pid
            save_state(_snap)
        deadline = time.time() + timeout
        assert proc.stdout is not None
        for line in proc.stdout:
            _append_log(step_id, line)
            if time.time() > deadline:
                proc.kill()
                _append_log(step_id, f"✖ 타임아웃({timeout}s)으로 중단")
                break
        rc = proc.wait(timeout=30)
    except Exception as e:  # pragma: no cover - 방어적
        _append_log(step_id, f"✖ 실행 예외: {e}")
        rc = -1
    finally:
        _procs.pop(step_id, None)

    artifacts = resolve_artifacts(step)
    problem = validate_step_output(step, artifacts, started_epoch) if rc == 0 else None
    state = load_state()
    st = state["steps"][step_id]
    st["returncode"] = rc
    st["pid"] = None
    st["finished_at"] = _now()

    if rc != 0:
        st["status"] = "failed"
        st["error"] = f"종료 코드 {rc}"
        _append_log(step_id, f"✖ 실패 (종료 코드 {rc})")
    elif problem:
        st["status"] = "failed"
        st["error"] = problem
        _append_log(step_id, f"✖ {problem}")
    else:
        st["status"] = "done"
        _append_log(step_id, f"✔ 완료 (산출물 {len(artifacts)}개)")

    save_state(state)
    if step_id in _stopped:
        # 중단 요청은 종료 코드가 1 로 나오기 때문에, 이유를 사용자에게 정확히 보여준다
        _stopped.discard(step_id)
        st["status"] = "failed"
        st["error"] = "사용자가 중단했습니다."
        save_state(state)
        _append_log(step_id, "⏹ 사용자가 실행을 중단했습니다")
    if st["status"] == "done":
        _invalidate_downstream(step_id)
    with _LOCK:
        _running.discard(step_id)
    return st["status"] == "done"


def run_auto(start_from: str | None = None, step_ids: list[str] | None = None) -> dict:
    """자동 모드: 단계를 순서대로 실행. 필수 단계 실패 시 중단."""
    config = load_config()
    stop_on_failure = bool(config.get("pipeline", {}).get("stop_on_failure", True))
    youtube_enabled = bool(config.get("youtube", {}).get("enabled", False))
    broll_enabled = bool((config.get("broll", {}) or {}).get("enabled", True))

    targets = step_ids or [s["id"] for s in STEPS]
    if start_from:
        order = [s["id"] for s in STEPS]
        targets = order[order.index(start_from):] if start_from in order else targets

    # 트렌드 확정 게이트: 자동 모드라도 사람이 후보를 고르기 전에는 대본 이후로 넘어가지 않는다.
    pause_after_trends = bool((config.get("pipeline", {}) or {}).get("pause_after_trends", True))
    order = [s["id"] for s in STEPS]
    gated = False
    if pause_after_trends and not is_trends_confirmed():
        after_trends = [t for t in targets if t in order and order.index(t) > order.index("trends")]
        if after_trends:
            targets = [t for t in targets if t in order and order.index(t) <= order.index("trends")]
            gated = True

    results = {}
    for sid in targets:
        step = STEP_BY_ID[sid]
        state = load_state()
        if state["steps"][sid]["status"] == "done":
            results[sid] = "already_done"
            continue
        if sid == "upload" and not youtube_enabled:
            results[sid] = "skipped"
            state["steps"][sid]["status"] = "skipped"
            state["steps"][sid]["error"] = "config.youtube.enabled=false (건너뜀)"
            save_state(state)
            continue

        if sid == "broll" and not broll_enabled:
            results[sid] = "skipped"
            state["steps"][sid]["status"] = "skipped"
            state["steps"][sid]["error"] = "B-Roll 사용 안 함 (장면 전체 화면 + 말풍선으로 진행)"
            save_state(state)
            continue

        ok, reason = prereq_status(step)
        if not ok and not step.get("optional"):
            state["steps"][sid]["status"] = "skipped"
            state["steps"][sid]["error"] = reason
            save_state(state)
            results[sid] = "blocked"
            if stop_on_failure:
                break
            continue

        if sid == "script":
            auto_approve = bool(config.get("pipeline", {}).get("auto_approve_script", True))
            ok = run_step(sid, extra_args=["--approve"] if auto_approve else [])
        else:
            ok = run_step(sid)

        results[sid] = "done" if ok else "failed"
        if not ok and not step.get("optional") and stop_on_failure:
            break

    if gated and load_state()["steps"].get("trends", {}).get("status") == "done":
        data = read_json("output/trends.json") or {}
        sel = data.get("selected") or {}
        message = (
            "트렌드 후보를 사람이 확인해야 다음 단계로 갑니다. "
            f"현재 자동 선택: {sel.get('title', '(없음)')} — 웹에서 후보를 고르고 '확정'을 누르세요."
        )
        set_pause("트렌드 확정 대기", "trends", message)
        results["_paused"] = "trends"
        _append_log("trends", f"⏸ {message}")

    return results


# ----------------------------------------------------------------------
# 요약 (웹/CLI 표시용)
# ----------------------------------------------------------------------
def snapshot() -> dict:
    # 서버가 비정상 종료된 뒤 남은 'running' 상태를 스스로 정리한다(UI 멈춤 방지)
    recover_interrupted()
    state = load_state()
    steps = []
    for s in STEPS:
        st = state["steps"][s["id"]]
        ok, reason = prereq_status(s)
        steps.append(
            {
                **{k: s[k] for k in ("id", "num", "name", "desc", "optional", "requires")},
                "status": st["status"],
                "started_at": st["started_at"],
                "finished_at": st["finished_at"],
                "error": st.get("error", ""),
                "returncode": st.get("returncode"),
                "log": st.get("log", [])[-200:],
                "artifacts": resolve_artifacts(s, int(s.get("artifact_limit") or 6)),
                "can_run": ok and st["status"] != "running" and not is_running(),
                "blocked_reason": "" if ok else reason,
                "running": st["status"] == "running",
            }
        )

    done = sum(1 for s in steps if s["status"] == "done")
    skipped = sum(1 for s in steps if s["status"] == "skipped")
    total = len(steps)
    # 건너뛴 선택 단계도 "처리됨"으로 보고 진행률을 계산한다
    percent = round((done + skipped) * 100 / total) if total else 0
    return {
        "mode": state.get("mode", "manual"),
        "updated_at": state.get("updated_at", ""),
        "pause": state.get("pause") or None,
        "trends_confirmed": is_trends_confirmed(),
        "steps": steps,
        "progress": {"done": done, "skipped": skipped, "total": total, "percent": percent},
        "running": is_running(),
        "project": str(PROJECT),
    }


def read_json(rel: str) -> dict | list | None:
    p = PROJECT / rel
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
