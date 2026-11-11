"""
너구리 쇼츠 - 웹 파이프라인 (http://127.0.0.1:5000)
=====================================================
- 단계별 수동 진행(기본) / 전체 자동 실행
- 진행률·로그·산출물 실시간 표시 (2초 폴링)
- 트렌드 수집 결과(도구/쿼리/후보/점수/선택 이유) 표로 노출
- 대본 초안 수정·승인 → TTS 단계 진행
"""
from __future__ import annotations

import ctypes
import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from flask import Flask, abort, jsonify, render_template, request, send_file

import broll_lib
import bubble_overlay
import llm_client
import motion_plan
import pipeline_core as core
import youtube_meta

PROJECT = core.PROJECT
OUTPUT_DIR = PROJECT / "output"
BROLL_SOURCE = OUTPUT_DIR / "broll_source.json"
WANGP_JOB = OUTPUT_DIR / "wangp_job.json"
WANGP_JOB_STATE = OUTPUT_DIR / "wangp_job_state.json"
WANGP_SERVER_PID = OUTPUT_DIR / "wangp_server.pid"
WANGP_SERVER_LOG = OUTPUT_DIR / "wangp_server.log"
WANGP_GEN_LOG = OUTPUT_DIR / "wangp_generate.log"
WANGP_GEN_PID = OUTPUT_DIR / "wangp_gen.pid"
LAYOUT_PREVIEW = OUTPUT_DIR / "layout_preview" / "sample.jpg"
SCENES_JSON = OUTPUT_DIR / "scenes" / "scenes.json"
WANGP_JOBS = OUTPUT_DIR / "wangp_jobs.json"
SCENES_DIR = OUTPUT_DIR / "scenes"
TTS_SAMPLE = OUTPUT_DIR / "voice_sample.mp3"
TTS_SAMPLE_META = OUTPUT_DIR / "voice_sample_meta.json"
TTS_MODELS = ["eleven_multilingual_v2", "eleven_turbo_v2_5", "eleven_flash_v2_5", "eleven_multilingual_v1"]
EDGE_VOICES = [
    "ko-KR-SunHiNeural", "ko-KR-InJoonNeural", "ko-KR-HyunsuMultilingualNeural",
    "ko-KR-JiMinNeural", "ko-KR-SeoHyeonNeural", "ko-KR-YuJinNeural",
    "en-US-AriaNeural", "en-US-GuyNeural",
]
_WANGP_LOCK = threading.Lock()

ENV_FILE = PROJECT / ".env"
load_dotenv(ENV_FILE, override=False)

_ENV_CACHE: dict = {"mtime": -1.0}


def reload_env(force: bool = False) -> None:
    """
    .env 를 다시 읽어 ELEVENLABS_* 값만 os.environ 에 반영한다.

    서버를 켤 때 load_dotenv(override=False) 로 한 번만 읽으면, 실행 중에 키를
    바꿔도 계속 '키 없음'으로 보인다(자식 프로세스도 옛 값을 물려받는다).
    그래서 요청마다 .env 수정 시각을 확인해 바뀐 경우에만 다시 읽는다.
    (다른 서비스 키를 덮어쓰지 않도록 ELEVENLABS_* 만 갱신)
    """
    try:
        mtime = ENV_FILE.stat().st_mtime
    except OSError:
        mtime = 0.0
    if not force and mtime == _ENV_CACHE["mtime"]:
        return
    try:
        values = dotenv_values(ENV_FILE)
    except Exception:  # noqa: BLE001
        values = {}

    changed = False
    for k, v in values.items():
        if not k or not k.startswith("ELEVENLABS_") or v is None:
            continue
        v = v.strip()
        if os.environ.get(k) != v:
            os.environ[k] = v
            changed = True

    _ENV_CACHE["mtime"] = mtime
    if changed or force:
        _EL_CACHE.update({"at": 0.0, "key": "", "state": "missing", "detail": ""})
        _STATUS_CACHE.update({"at": 0.0, "data": {}})
        print(f"[ENV] .env 다시 읽음 (ElevenLabs 값 갱신{'' if changed else ' · 변경 없음'})")

# 콘솔 인코딩이 cp949 여도 한글 배너/로그가 깨지지 않게
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

_STATUS_CACHE: dict = {"at": 0.0, "data": {}}


# ----------------------------------------------------------------------
# 환경 점검 (20초 캐시)
# ----------------------------------------------------------------------
def _probe(url: str, timeout: float = 1.0) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except Exception:
        return False


_EL_CACHE: dict = {"at": 0.0, "key": "", "state": "missing", "detail": ""}


def elevenlabs_status() -> dict:
    """키 존재/플레이스홀더/401/유효를 구분한다. (10분 캐시)"""
    reload_env()
    key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    if not key:
        return {"state": "missing", "configured": False, "verified": False,
                "detail": ".env 에 ELEVENLABS_API_KEY 가 없습니다.", "key_len": 0}

    try:
        sgs = importlib.import_module("scripts_generate_script")
        is_real = sgs.is_real_key(key)
    except Exception:
        is_real = key.startswith("sk_") and "your" not in key.lower()

    if not is_real:
        return {"state": "placeholder", "configured": False, "verified": False,
                "detail": f".env 의 키가 플레이스홀더입니다({len(key)}자). ElevenLabs 대시보드의 실제 키로 교체하세요.",
                "key_len": len(key)}

    now = time.time()
    if _EL_CACHE["key"] == key and now - _EL_CACHE["at"] < 600:
        return {"state": _EL_CACHE["state"], "configured": True,
                "verified": _EL_CACHE["state"] == "valid",
                "detail": _EL_CACHE["detail"], "key_len": len(key)}

    try:
        sgs = importlib.import_module("scripts_generate_script")
        ok, detail = sgs.verify_elevenlabs_key(key)
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"확인 실패: {type(e).__name__}"

    state = "valid" if ok else "invalid"
    _EL_CACHE.update({"at": now, "key": key, "state": state, "detail": detail})
    return {"state": state, "configured": True, "verified": ok, "detail": detail, "key_len": len(key)}


def system_status() -> dict:
    if time.time() - _STATUS_CACHE["at"] < 20 and _STATUS_CACHE["data"]:
        return _STATUS_CACHE["data"]

    config = core.load_config()
    el = config.get("elevenlabs", {}) or {}
    el_state = elevenlabs_status()

    llm = config.get("llm", {}) or {}
    llm_stat = llm_client.provider_status(config)
    ready_llms = [k for k, v in (llm_stat.get("providers") or {}).items()
                  if v.get("has_key") and (k != "ollama" or v.get("enabled"))]
    ollama_url = (llm.get("providers", {}).get("ollama", {}).get("base_url")
                  or llm.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")

    channel = core.read_json("output/channel_analysis.json") or {}
    whisper_cfg = ((config.get("paths", {}) or {}).get("whisper_model") or "")
    whisper_path = Path(core.expand(whisper_cfg)) if whisper_cfg else None
    if not whisper_path or not whisper_path.exists():
        found = sorted(PROJECT.glob("models/whisper/**/model.bin"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        whisper_path = found[0].parent if found else whisper_path

    capcut_root = Path(core.expand((config.get("paths", {}) or {}).get("capcut_draft_root", "")))
    assets = Path(core.expand((config.get("paths", {}) or {}).get("assets_dir", str(PROJECT / "assets"))))
    token_path = (config.get("youtube", {}) or {}).get("token_path", "")

    data = {
        "elevenlabs": {
            **el_state,
            "enabled": bool(el.get("enabled", True)),
            "voice_id": el.get("default_voice_id") or el.get("voice_id") or os.getenv("ELEVENLABS_VOICE_ID") or "",
            "tts_provider": ((config.get("tts", {}) or {}).get("provider") or "auto"),
            "edge_voice": ((config.get("tts", {}) or {}).get("edge_voice") or ""),
        },
        "llm": {
            "provider": llm_stat.get("provider", "auto"),
            "available": ready_llms,
            "ollama_enabled": bool(llm_stat.get("ollama_enabled")),
            "ollama_url": ollama_url,
            "ollama_reachable": _probe(f"{ollama_url}/api/tags"),
            "ollama_model": llm.get("providers", {}).get("ollama", {}).get("model", ""),
        },
        "youtube": {
            "token": bool(token_path) and Path(core.expand(token_path)).exists(),
            "upload_enabled": bool((config.get("youtube", {}) or {}).get("enabled", False)),
            "channel": (channel.get("channel") or {}).get("title", ""),
        },
        "whisper": {
            "path": str(whisper_path) if whisper_path else "",
            "exists": bool(whisper_path and Path(whisper_path).exists()),
        },
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "capcut_root": {"path": str(capcut_root), "exists": capcut_root.exists()},
        "broll_dir": core.expand((config.get("paths", {}) or {}).get("broll_dir", "")),
        "assets": {
            "host_png": (assets / "raccoon" / "host.png").exists(),
            "sfx": sorted(p.name for p in (assets / "sfx").glob("*.mp3")) if (assets / "sfx").exists() else [],
        },
    }
    _STATUS_CACHE.update({"at": time.time(), "data": data})
    return data


# ----------------------------------------------------------------------
# WanGP (서버/생성)
# ----------------------------------------------------------------------
def _read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _pid_alive(pid: int) -> bool:
    """PID 생존 확인. tasklist 는 이 PC에서 간헐적으로 멈춰서 서버가 같이 멈추므로 Win32 API 를 쓴다."""
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def wangp_settings() -> dict:
    wp = (core.load_config().get("wangp", {}) or {})
    root = Path(core.expand(wp.get("root", r"E:\WanGP")))
    py = Path(core.expand(wp.get("python", "")))
    base = (wp.get("base_url") or "http://127.0.0.1:7860").rstrip("/")
    return {"cfg": wp, "root": root, "python": py, "base_url": base}


def _parse_rate_percent(value) -> int:
    """'+15%' / '1.15' / 15 → 15 (%)"""
    try:
        sgs = importlib.import_module("scripts_generate_script")
        return sgs.parse_rate_percent(value)
    except Exception:
        return 0


def _rate_percent(tts_cfg: dict) -> int:
    if "rate_percent" in tts_cfg:
        return _parse_rate_percent(tts_cfg.get("rate_percent"))
    return _parse_rate_percent(tts_cfg.get("edge_rate") or "+0%")


def tts_settings() -> dict:
    """웹 음성 설정 카드용 현재 값 (config + .env 키 상태)."""
    reload_env()
    config = core.load_config()
    el = config.get("elevenlabs", {}) or {}
    tts_cfg = config.get("tts", {}) or {}
    voices = el.get("voices") or []
    try:
        sgs = importlib.import_module("scripts_generate_script")
        current_voice = sgs.pick_voice_id(config)
    except Exception:
        current_voice = os.getenv("ELEVENLABS_VOICE_ID") or el.get("default_voice_id") or el.get("voice_id") or ""
    sample_meta = core.read_json("output/voice_sample_meta.json") or {}
    return {
        "provider": (tts_cfg.get("provider") or "auto"),
        "fallback_to_edge": bool(tts_cfg.get("fallback_to_edge", True)),
        "voice_id": current_voice,
        "model_id": el.get("model_id") or os.getenv("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2",
        "edge_voice": tts_cfg.get("edge_voice") or "ko-KR-SunHiNeural",
        "edge_rate": tts_cfg.get("edge_rate") or "+0%",
        "rate_percent": _rate_percent(tts_cfg),
        "voices": voices,
        "models": TTS_MODELS,
        "edge_voices": EDGE_VOICES,
        "key": elevenlabs_status(),
        "sample": sample_meta or None,
        "sample_exists": TTS_SAMPLE.exists(),
    }


def _tail(path: Path, n: int = 200) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


def _refresh_wangp_job_state() -> dict:
    """생성 프로세스(PID)가 끝났는지 확인해 상태를 갱신한다.
    (웹 서버가 재시작되어도 생성 결과가 유지되도록 파일 기반으로 판단)"""
    st = core.read_json("output/wangp_job_state.json") or {"status": "idle", "log": []}
    if st.get("status") != "running":
        return st

    pid = _read_pid(WANGP_GEN_PID)
    if pid and _pid_alive(pid):
        st["log"] = _tail(WANGP_GEN_LOG)
        return st

    result = core.read_json("output/wangp_result.json") or {}
    ok = bool(result.get("success"))
    st.update({
        "status": "done" if ok else "failed",
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "log": _tail(WANGP_GEN_LOG),
        "error": "" if ok else (result.get("error") or "생성이 정상 종료되지 않았습니다."),
    })
    if result.get("elapsed_sec"):
        st["elapsed_sec"] = result["elapsed_sec"]
    _write_job_state(st)
    WANGP_GEN_PID.unlink(missing_ok=True)
    _mark_step("wangp", "done" if ok else "failed", "" if ok else st["error"])
    return st


def wangp_status() -> dict:
    s = wangp_settings()
    wp = s["cfg"]
    pid = _read_pid(WANGP_SERVER_PID)
    server_alive = _pid_alive(pid)
    reachable = _probe(s["base_url"], timeout=1.5)
    job_state = _refresh_wangp_job_state()
    if job_state.get("status") == "running" and job_state.get("started_at"):
        try:
            started = datetime.fromisoformat(job_state["started_at"])
            job_state["elapsed_sec"] = round((datetime.now() - started).total_seconds(), 1)
        except Exception:
            pass
    result = core.read_json("output/wangp_result.json") or {}
    job = core.read_json("output/wangp_job.json") or {}

    gen_dir = Path(core.expand((core.load_config().get("paths", {}) or {}).get(
        "generated_videos", str(OUTPUT_DIR / "generated_videos"))))
    videos = []
    if gen_dir.exists():
        clips = list(gen_dir.glob("wangp_*.mp4")) + list(gen_dir.glob("scene_*_wangp.mp4"))
        for p in sorted(clips, key=lambda x: x.stat().st_mtime, reverse=True)[:6]:
            videos.append({"name": p.name, "path": str(p), "mtime": p.stat().st_mtime,
                           "size": p.stat().st_size})
    scenes_data = core.read_json("output/scenes/scenes.json") or {}
    vids = generated_videos_dir()
    motion_done = [int(sc.get("index") or 0) for sc in scenes_data.get("scenes") or []
                   if clip_ready(sc, vids)]
    plan = motion_plan.planned_indices(WANGP_JOBS)
    motion = {
        "plan": plan,
        "done": sorted(i for i in motion_done if i),
        "count": len(plan),
        "scene_count": len(scenes_data.get("scenes") or []),
        "clean_only": True,
    }

    return {
        "root": str(s["root"]),
        "root_exists": (s["root"] / "wgp.py").exists(),
        "python": str(s["python"]),
        "python_exists": s["python"].exists(),
        "base_url": s["base_url"],
        "server": {
            "pid": pid if server_alive else 0,
            "running": server_alive,
            "reachable": reachable,
            # 사용자가 직접 켜둔 WanGP(7860)는 우리가 띄운 PID 가 없다
            "external": reachable and not server_alive,
            "log": str(WANGP_SERVER_LOG),
        },
        "defaults": {
            "resolution": wp.get("resolution", "480x832"),
            "video_length": int(wp.get("video_length", 81)),
            "num_inference_steps": int(wp.get("num_inference_steps", 4)),
        },
        "job": job,
        "job_state": job_state,
        "result": result,
        "videos": videos,
        "motion": motion,
    }


def _mark_step(step_id: str, status: str, message: str = "") -> None:
    try:
        state = core.load_state()
        st = state["steps"][step_id]
        st["status"] = status
        st["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st["error"] = message if status == "failed" else ""
        st.setdefault("log", []).append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}" if message else "")
        core.save_state(state)
    except Exception:
        pass


def _write_job_state(payload: dict) -> None:
    core._atomic_write(WANGP_JOB_STATE, payload)  # noqa: SLF001 (같은 프로젝트 내부 유틸)


def _start_wangp_generation() -> int:
    """WanGP 생성을 별도 프로세스로 떼어내고 PID 를 기록한다.
    (모델 로딩 중 RAM/VRAM 이 많이 필요해도 웹 서버는 살아 있게 한다.)"""
    s = wangp_settings()
    adapter = PROJECT / "scripts_generate_wangp.py"
    WANGP_GEN_LOG.parent.mkdir(parents=True, exist_ok=True)
    # 이전 결과 파일을 지워서 완료 판정이 섞이지 않게 한다
    (OUTPUT_DIR / "wangp_result.json").unlink(missing_ok=True)

    state = {"status": "running", "started_at": datetime.now().isoformat(timespec="seconds"),
             "finished_at": "", "returncode": None, "error": "", "log": []}
    _write_job_state(state)

    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    with open(WANGP_GEN_LOG, "wb") as logf, _WANGP_LOCK:
        proc = subprocess.Popen(
            [str(s["python"]), str(adapter)], cwd=str(PROJECT),
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                 "PYTHONUNBUFFERED": "1"},
            stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags,
        )
    WANGP_GEN_PID.write_text(str(proc.pid), encoding="utf-8")
    return proc.pid


# ----------------------------------------------------------------------
# 상태
# ----------------------------------------------------------------------
def build_state() -> dict:
    snap = core.snapshot()
    trends = core.read_json("output/trends.json") or {}
    script_meta = core.read_json("output/script_meta.json") or {}
    channel = core.read_json("output/channel_analysis.json") or {}
    capcut = core.read_json("output/capcut_result.json") or {}

    draft_path = OUTPUT_DIR / "script_draft.txt"
    approved_path = OUTPUT_DIR / "script.txt"

    trend_view = None
    if trends:
        sel_id = trends.get("selected_id", "")
        trend_view = {
            "collected_at": trends.get("collected_at", ""),
            "duration_sec": trends.get("duration_sec", 0),
            "queries": trends.get("queries", []),
            "tools": trends.get("tools", []),
            "selection_mode": trends.get("selection_mode", ""),
            "selection_pool": trends.get("selection_pool", {}),
            "selected": trends.get("selected", {}),
            "candidate_count": len(trends.get("candidates", [])),
            "excluded_count": len(trends.get("excluded", [])),
            "excluded_sample": [
                {"title": c.get("title", ""), "reason": c.get("exclude_reason", "")}
                for c in (trends.get("excluded", []) or [])[:8]
            ],
            "runner_ups": trends.get("runner_ups", []),
            "selected_id": sel_id,
            "selected_keyword": trends.get("selected_keyword", ""),
            "confirmed": core.is_trends_confirmed(),
            "confirmed_at": (core.trends_confirmation() or {}).get("confirmed_at", ""),
            "pause_after_trends": bool((core.load_config().get("pipeline", {}) or {}).get("pause_after_trends", True)),
            "search_keywords": (core.load_config().get("trending", {}) or {}).get("search_keywords", []),
            "min_views": int((core.load_config().get("trending", {}) or {}).get("min_views", 3000)),
            "max_duration_sec": int((core.load_config().get("trending", {}) or {}).get("max_duration_sec", 180)),
            "candidates": [
                {
                    "rank": i + 1,
                    "id": c.get("id", ""),
                    "source": c.get("source"),
                    "author": c.get("author", ""),
                    "title": c.get("title", ""),
                    "url": c.get("url", ""),
                    "metric": c.get("views") or c.get("stars") or c.get("downloads") or 0,
                    "score": c.get("score"),
                    "published": c.get("published", ""),
                    "duration_sec": c.get("duration_sec", 0),
                    "fit": c.get("fit_keywords", []),
                    "description": (c.get("description") or "")[:400],
                    "selected": c.get("id") == sel_id,
                }
                for i, c in enumerate(trends.get("candidates") or [])
            ],
        }

    return {
        **snap,
        "now": datetime.now().strftime("%H:%M:%S"),
        "trends": trend_view,
        "channel": {
            "title": (channel.get("channel") or {}).get("title", ""),
            "subs": (channel.get("channel") or {}).get("subscribers", 0),
            "videos": (channel.get("stats") or {}).get("analyzed_videos", 0),
            "cadence": (channel.get("stats") or {}).get("upload_cadence_days"),
            "keywords": [k.get("keyword") for k in (channel.get("top_keywords") or [])],
            "source": channel.get("source", ""),
        } if channel else None,
        "script": {
            "draft": draft_path.read_text(encoding="utf-8", errors="replace") if draft_path.exists() else "",
            "approved": approved_path.read_text(encoding="utf-8", errors="replace") if approved_path.exists() else "",
            "meta": script_meta,
            "char_limit": int((core.load_config().get("persona", {}) or {}).get("max_chars", 350)),
        },
        "capcut": capcut or None,
        "tts": tts_settings(),
        "voice": core.read_json("output/voice_meta.json") or None,
        "assembly": core.load_config().get("assembly", {}),
        "scenes": scenes_view(),
        "broll": {
            "source": core.read_json("output/broll_source.json") or {},
            "meta": core.read_json("output/broll_meta.json") or {},
            "dir": core.expand((core.load_config().get("paths", {}) or {}).get("broll_dir", "")),
            "enabled": bool((core.load_config().get("broll", {}) or {}).get("enabled", True)),
            "clips": broll_lib.list_clips(),
        },
        "wangp": wangp_status(),
        "youtube": youtube_view(),
        "system": system_status(),
    }


@app.after_request
def _no_store_html(resp):
    """HTML/API 응답을 브라우저가 캐시하지 않게 한다(낡은 화면으로 테스트하는 사고 방지)."""
    if resp.mimetype in ("text/html", "application/json"):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/")
def index():
    return render_template("index.html", initial=build_state())


@app.route("/api/state")
def api_state():
    return jsonify(build_state())


# ----------------------------------------------------------------------
# 실행 제어
# ----------------------------------------------------------------------
@app.route("/api/run/<step_id>", methods=["POST"])
def api_run_step(step_id: str):
    if step_id not in core.STEP_BY_ID:
        return jsonify({"error": "알 수 없는 단계입니다."}), 400
    if core.is_running():
        return jsonify({"error": "다른 단계가 실행 중입니다. 끝난 뒤 다시 시도하세요."}), 409

    step = core.STEP_BY_ID[step_id]
    if step_id == "broll" and not bool((core.load_config().get("broll", {}) or {}).get("enabled", True)):
        return jsonify({"error": "B-Roll 사용 안 함으로 설정되어 있습니다. '🎥 B-Roll 소스'에서 방식을 바꾸거나, 그대로 두고 10단계를 실행하세요."}), 400
    ok, reason = core.prereq_status(step)
    if not ok:
        return jsonify({"error": reason}), 400

    auto_mode = core.load_state().get("mode") == "auto"

    def worker():
        ok = core.run_step(step_id)
        # 전체 자동 모드: 이 단계가 성공하면 다음 단계부터 끝까지 이어서 실행
        if ok and auto_mode:
            nxt = core.next_step_after(step_id)
            if nxt:
                core.run_auto(start_from=nxt)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "step": step_id, "auto_continue": auto_mode})


@app.route("/api/run_all", methods=["POST"])
def api_run_all():
    if core.is_running():
        return jsonify({"error": "이미 실행 중입니다."}), 409
    payload = request.get_json(silent=True) or {}
    start_from = payload.get("from")

    threading.Thread(target=core.run_auto, kwargs={"start_from": start_from}, daemon=True).start()
    return jsonify({"ok": True, "from": start_from})


@app.route("/api/stop/<step_id>", methods=["POST"])
def api_stop_step(step_id: str):
    """실행 중인 단계를 중단(자식 프로세스 강제 종료)."""
    if step_id not in core.STEP_BY_ID:
        return jsonify({"error": "알 수 없는 단계입니다."}), 400
    stopped = core.stop_step(step_id)
    if not stopped:
        # 이미 프로세스가 사라졌는데 상태만 running 으로 남은 경우
        core.recover_interrupted()
        return jsonify({"ok": True, "stopped": False,
                        "note": "실행 중인 프로세스가 없어서 상태만 정리했습니다."})
    return jsonify({"ok": True, "stopped": True})


@app.route("/api/reset/<step_id>", methods=["POST"])
def api_reset_step(step_id: str):
    if core.is_running():
        return jsonify({"error": "실행 중에는 초기화할 수 없습니다."}), 409
    if step_id not in core.STEP_BY_ID:
        return jsonify({"error": "알 수 없는 단계입니다."}), 400
    core.reset_step(step_id)
    return jsonify({"ok": True})


@app.route("/api/reset_all", methods=["POST"])
def api_reset_all():
    if core.is_running():
        return jsonify({"error": "실행 중에는 초기화할 수 없습니다."}), 409
    core.reset_all()
    return jsonify({"ok": True})


@app.route("/api/mode", methods=["POST"])
def api_mode():
    payload = request.get_json(silent=True) or {}
    mode = payload.get("mode", "manual")
    core.set_mode(mode)
    return jsonify({"ok": True, "mode": "auto" if mode == "auto" else "manual"})


# ----------------------------------------------------------------------
# 대본 편집 / 승인
# ----------------------------------------------------------------------
@app.route("/api/script", methods=["POST"])
def api_save_script():
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"error": "대본이 비어 있습니다."}), 400
    target = payload.get("target", "draft")
    if target not in ("draft", "approved"):
        return jsonify({"error": "target 은 draft 또는 approved 여야 합니다."}), 400
    path = OUTPUT_DIR / ("script.txt" if target == "approved" else "script_draft.txt")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return jsonify({"ok": True, "path": str(path), "chars": len(text)})


@app.route("/api/approve", methods=["POST"])
def api_approve():
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        draft = OUTPUT_DIR / "script_draft.txt"
        text = draft.read_text(encoding="utf-8").strip() if draft.exists() else ""
    if not text:
        return jsonify({"error": "승인할 대본이 없습니다. 초안을 먼저 생성하거나 직접 입력하세요."}), 400

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "script_draft.txt").write_text(text, encoding="utf-8")
    (OUTPUT_DIR / "script.txt").write_text(text, encoding="utf-8")

    state = core.load_state()
    if state["steps"]["script"]["status"] != "done":
        state["steps"]["script"]["status"] = "done"
        state["steps"]["script"]["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["steps"]["script"]["error"] = ""
        state["steps"]["script"]["log"].append(
            f"[{datetime.now().strftime('%H:%M:%S')}] 수동 입력 대본 승인 ({len(text)}자)"
        )
        core.save_state(state)

    return jsonify({"ok": True, "chars": len(text)})


@app.route("/api/script/manual", methods=["POST"])
def api_script_manual():
    """내가 입력한 주제로만 대본 초안을 만든다(트렌드 수집·확정 없이도 가능).

    트렌드 기능은 그대로 두고, '이 주제로 써줘' 통로만 추가한다.
    트렌드 후보·발췌는 프롬프트에 넣지 않는다(--manual).
    결과는 script_draft.txt 이고, 승인은 기존과 똑같이 웹에서 한다.
    """
    payload = request.get_json(silent=True) or {}
    topic = (payload.get("topic") or "").strip()
    note = (payload.get("note") or "").strip()
    if len(topic) < 2:
        return jsonify({"error": "주제를 2자 이상 입력하세요."}), 400
    if core.is_running():
        return jsonify({"error": "다른 단계가 실행 중입니다. 끝난 뒤에 다시 시도하세요."}), 409

    # 한글은 argv 로 넘기면 Windows 에서 '?' 로 깨진다 → 환경변수로 전달
    ok = core.run_step("script", extra_args=["--manual", "--topic-env"],
                       extra_env={"RACCOON_TOPIC": topic, "RACCOON_NOTE": note})
    st = (core.load_state().get("steps") or {}).get("script") or {}
    log = (st.get("log") or [])[-25:]
    if not ok:
        return jsonify({"error": st.get("error") or "대본 생성 실패", "log": log}), 500

    draft = OUTPUT_DIR / "script_draft.txt"
    text = draft.read_text(encoding="utf-8").strip() if draft.exists() else ""
    meta = core.read_json("output/script_meta.json") or {}
    return jsonify({
        "ok": True, "keyword": topic, "chars": len(text), "text": text,
        "backend": meta.get("backend"), "model": meta.get("model"), "log": log,
    })


# ----------------------------------------------------------------------
# 산출물 서빙 (상태에 기록된 파일만)
# ----------------------------------------------------------------------
def _artifact(step_id: str, index: int) -> Path:
    step = core.STEP_BY_ID.get(step_id)
    if not step:
        abort(404)
    items = core.resolve_artifacts(step, limit=20)
    if index < 0 or index >= len(items):
        abort(404)
    p = Path(items[index]["path"])
    if not p.exists() or not p.is_file():
        abort(404)
    return p


@app.route("/file/<step_id>/<int:index>")
def api_file(step_id: str, index: int):
    return send_file(_artifact(step_id, index))


@app.route("/download/<step_id>/<int:index>")
def api_download(step_id: str, index: int):
    return send_file(_artifact(step_id, index), as_attachment=True)


@app.route("/api/trends")
def api_trends():
    return jsonify(core.read_json("output/trends.json") or {})


@app.route("/api/trends/fetch", methods=["POST"])
def api_trends_fetch():
    """검색 키워드/최소 조회수를 저장하고 트렌드를 다시 수집한다(백그라운드)."""
    if core.is_running():
        return jsonify({"error": "다른 단계가 실행 중입니다. 끝난 뒤 다시 시도하세요."}), 409
    payload = request.get_json(silent=True) or {}

    raw = payload.get("keywords")
    if isinstance(raw, str):
        keywords = [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]
    elif isinstance(raw, list):
        keywords = [str(k).strip() for k in raw if str(k).strip()]
    else:
        keywords = [k for k in ((core.load_config().get("trending", {}) or {}).get("search_keywords") or []) if k]
    if not keywords:
        return jsonify({"error": "검색 키워드를 1개 이상 입력하세요."}), 400

    try:
        min_views = max(0, int(payload.get("min_views") or 3000))
        max_duration = max(5, min(900, int(payload.get("max_duration_sec") or 180)))
    except Exception:
        return jsonify({"error": "최소 조회수/최대 길이 값이 올바르지 않습니다."}), 400

    cfg = core.load_config()
    tr = cfg.setdefault("trending", {})
    tr["search_keywords"] = keywords
    tr["min_views"] = min_views
    tr["max_duration_sec"] = max_duration
    core.CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    core.clear_trends_confirmation()
    core.clear_pause()
    extra = ["--min-views", str(min_views), "--max-duration", str(max_duration)]

    def worker():
        core.run_step("trends", extra_args=extra)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "keywords": keywords, "min_views": min_views,
                    "max_duration_sec": max_duration})


@app.route("/api/trends/select", methods=["POST"])
def api_trends_select():
    """전체 후보 목록에서 사용자가 고른 후보를 선택 + 확정한다."""
    if core.is_running():
        return jsonify({"error": "실행 중에는 후보를 바꿀 수 없습니다."}), 409
    payload = request.get_json(silent=True) or {}
    target_id = str(payload.get("id") or "").strip()
    if not target_id:
        return jsonify({"error": "후보 id 가 필요합니다."}), 400

    trends = core.read_json("output/trends.json") or {}
    candidates = trends.get("candidates") or []
    idx, chosen = -1, None
    for i, c in enumerate(candidates):
        if str(c.get("id") or "") == target_id:
            idx, chosen = i, c
            break
    if chosen is None:
        return jsonify({"error": "해당 후보를 trends.json 에서 찾지 못했습니다. 트렌드를 다시 수집하세요."}), 404

    try:
        sft = importlib.import_module("scripts_fetch_trends")
        keyword = sft.pick_keyword(chosen)
        reason = sft.describe_reason(chosen) if chosen.get("score_breakdown") else (chosen.get("reason") or "")
    except Exception:
        keyword = chosen.get("title") or target_id
        reason = chosen.get("reason") or ""

    selected = dict(chosen)
    selected["keyword"] = keyword
    selected["reason"] = reason
    selected["content_excerpt"] = (chosen.get("description") or "").strip()
    trends["selected"] = selected
    trends["selected_id"] = str(chosen.get("id") or "")
    trends["selected_keyword"] = keyword
    trends["selection_mode"] = f"manual (웹에서 사용자가 {idx + 1}순위 후보를 선택)"
    trends["user_selected_at"] = datetime.now().isoformat(timespec="seconds")
    core._atomic_write(OUTPUT_DIR / "trends.json", trends)  # noqa: SLF001
    try:
        importlib.import_module("scripts_fetch_trends").write_report(trends, OUTPUT_DIR / "trends_report.md")
    except Exception:
        pass

    core._invalidate_downstream("trends")  # noqa: SLF001 (선택이 바뀌면 대본 이후는 다시 만들어야 함)
    conf = core.confirm_trends(trends["selected_id"], keyword, note="웹에서 후보 선택")
    return jsonify({"ok": True, "selected": selected, "confirmed": conf})


@app.route("/api/trends/confirm", methods=["POST"])
def api_trends_confirm():
    """현재 선택을 확정(또는 확정 취소)한다. 대본 키워드도 함께 수정할 수 있다."""
    if core.is_running():
        return jsonify({"error": "실행 중에는 확정할 수 없습니다."}), 409
    payload = request.get_json(silent=True) or {}
    confirmed = payload.get("confirmed", True)

    if not confirmed:
        core.clear_trends_confirmation()
        core.set_pause("트렌드 확정 대기", "trends",
                       "트렌드 확정이 취소되었습니다. 후보를 고르고 다시 확정하세요.")
        return jsonify({"ok": True, "confirmed": False})

    trends = core.read_json("output/trends.json") or {}
    selected = trends.get("selected") or {}
    if not selected:
        return jsonify({"error": "선택된 트렌드가 없습니다. 먼저 트렌드를 수집하세요."}), 400

    keyword = (payload.get("keyword") or "").strip()
    if keyword:
        trends.setdefault("selected", {})["keyword"] = keyword
        trends["selected_keyword"] = keyword
        core._atomic_write(OUTPUT_DIR / "trends.json", trends)  # noqa: SLF001

    conf = core.confirm_trends(trends.get("selected_id", ""),
                              keyword or trends.get("selected_keyword", ""),
                              note="웹에서 확정")
    return jsonify({"ok": True, "confirmed": True, "confirmation": conf,
                    "keyword": keyword or trends.get("selected_keyword", "")})


# ----------------------------------------------------------------------
# 음성(TTS) 설정: ElevenLabs 음성/모델, edge-tts 대체 음성
# ----------------------------------------------------------------------
@app.route("/api/llm")
def api_llm():
    return jsonify(llm_client.provider_status(core.load_config()))


@app.route("/api/llm/config", methods=["POST"])
def api_llm_config():
    payload = request.get_json(silent=True) or {}
    cfg = core.load_config()
    llm = cfg.setdefault("llm", {})

    provider = (payload.get("provider") or llm.get("provider") or "auto").strip().lower()
    if provider not in ("auto", "template", *llm_client.PROVIDER_DEFAULTS):
        return jsonify({"error": f"알 수 없는 프로바이더: {provider}"}), 400
    llm["provider"] = provider

    if "ollama_enabled" in payload:
        llm["ollama_enabled"] = bool(payload.get("ollama_enabled"))

    model = (payload.get("model") or "").strip()
    if model and provider not in ("auto", "template"):
        provs = llm.setdefault("providers", {})
        provs.setdefault(provider, {})["model"] = model

    core.CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    core.reset_step("script")
    core._invalidate_downstream("script")  # noqa: SLF001
    return jsonify({"ok": True, "settings": llm_client.provider_status(cfg)})


@app.route("/api/llm/models", methods=["POST"])
def api_llm_models():
    payload = request.get_json(silent=True) or {}
    provider = (payload.get("provider") or "").strip().lower()
    try:
        models = llm_client.list_models(provider, core.load_config())
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 400
    return jsonify({"ok": True, "provider": provider, "models": models})


@app.route("/api/llm/test", methods=["POST"])
def api_llm_test():
    payload = request.get_json(silent=True) or {}
    provider = (payload.get("provider") or "").strip().lower()
    model = (payload.get("model") or "").strip() or None
    if provider in ("", "auto"):
        return jsonify({"error": "테스트할 프로바이더를 고르세요"}), 400
    try:
        result = llm_client.test_provider(provider, core.load_config(), model)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 400
    return jsonify({"ok": True, "result": result})


@app.route("/api/tts")
def api_tts():
    return jsonify(tts_settings())


@app.route("/api/tts/config", methods=["POST"])
def api_tts_config():
    payload = request.get_json(silent=True) or {}
    cfg = core.load_config()
    tts_cfg = cfg.setdefault("tts", {})
    el = cfg.setdefault("elevenlabs", {})

    provider = (payload.get("provider") or tts_cfg.get("provider") or "auto").strip().lower()
    if provider not in ("auto", "elevenlabs", "edge"):
        return jsonify({"error": "provider 는 auto / elevenlabs / edge 중 하나입니다."}), 400
    tts_cfg["provider"] = provider

    voice_id = (payload.get("voice_id") or "").strip()
    if voice_id:
        el["default_voice_id"] = voice_id
        el["voice_id"] = voice_id
        if not any(v.get("voice_id") == voice_id for v in (el.get("voices") or [])):
            el.setdefault("voices", []).append({
                "voice_id": voice_id, "name": f"직접 지정 ({voice_id[:8]}…)",
                "category": "custom", "language": "multi",
            })

    model_id = (payload.get("model_id") or "").strip()
    if model_id:
        el["model_id"] = model_id

    edge_voice = (payload.get("edge_voice") or "").strip()
    if edge_voice:
        tts_cfg["edge_voice"] = edge_voice

    rate_raw = payload.get("rate_percent", payload.get("edge_rate"))
    if rate_raw not in (None, ""):
        pct = _parse_rate_percent(rate_raw)
        tts_cfg["rate_percent"] = pct
        tts_cfg["edge_rate"] = f"{'+' if pct >= 0 else ''}{pct}%"

    core.CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    # 음성이 바뀌면 기존 voice.mp3/타이밍/초안은 낡은 결과가 된다
    core.reset_step("tts")
    core._invalidate_downstream("tts")  # noqa: SLF001
    return jsonify({"ok": True, "settings": tts_settings()})


@app.route("/api/tts/reload", methods=["POST"])
def api_tts_reload():
    """서버 재시작 없이 .env 를 다시 읽고 키 상태를 확인한다."""
    reload_env(force=True)
    st = elevenlabs_status()
    return jsonify({"ok": True, "key": st, "settings": tts_settings()})


@app.route("/api/tts/voices", methods=["POST"])
def api_tts_voices():
    """ElevenLabs 계정의 실제 음성 목록을 받아 config.json 에 저장한다."""
    reload_env()
    key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    try:
        sgs = importlib.import_module("scripts_generate_script")
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"모듈 로드 실패: {e}"}), 500

    if not sgs.is_real_key(key):
        st = elevenlabs_status()
        return jsonify({"error": "ElevenLabs 실제 API 키가 필요합니다. " + st.get("detail", "")}), 400
    try:
        voices = sgs.get_available_voices(key)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"음성 목록 조회 실패: {type(e).__name__}: {e}"}), 400
    if not voices:
        return jsonify({"error": "ElevenLabs 가 빈 음성 목록을 반환했습니다."}), 400

    cfg = core.load_config()
    el = cfg.setdefault("elevenlabs", {})
    el["voices"] = voices
    ids = [v["voice_id"] for v in voices]
    if el.get("default_voice_id") not in ids:
        el["default_voice_id"] = ids[0]
    if el.get("voice_id") not in ids:
        el["voice_id"] = el["default_voice_id"]
    core.CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "count": len(voices), "settings": tts_settings()})


@app.route("/api/tts/sample", methods=["POST"])
def api_tts_sample():
    """현재 설정으로 짧은 샘플 음성을 만들어 실제 목소리를 확인한다."""
    reload_env()
    payload = request.get_json(silent=True) or {}
    cfg = core.load_config()
    tts_cfg = cfg.get("tts", {}) or {}
    provider = (payload.get("provider") or tts_cfg.get("provider") or "auto").strip().lower()
    text = (payload.get("text") or "안녕! 나는 테크 너구리야. 오늘도 신나는 소식 알려줄게.").strip()
    try:
        sgs = importlib.import_module("scripts_generate_script")
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"모듈 로드 실패: {e}"}), 500

    key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    if provider == "elevenlabs" and not sgs.is_real_key(key):
        return jsonify({"error": "ElevenLabs 샘플은 실제 API 키가 필요합니다. (.env ELEVENLABS_API_KEY)"}), 400

    # 드롭다운에서 고른 음성/모델을 '저장 전'에도 샘플에 바로 적용한다
    # (저장 안 된 선택을 무시해서 항상 같은 목소리가 나오던 문제 수정)
    sample_voice = (payload.get("voice_id") or "").strip()
    sample_model = (payload.get("model_id") or "").strip()
    rate_raw = payload.get("rate_percent", payload.get("edge_rate"))
    sample_rate = _parse_rate_percent(rate_raw) if rate_raw not in (None, "") else None

    TTS_SAMPLE.unlink(missing_ok=True)
    errors: list[str] = []
    info: dict = {}
    try:
        if provider == "elevenlabs":
            info = sgs.synth_elevenlabs(text, cfg, TTS_SAMPLE,
                                        voice_id=sample_voice, model_id=sample_model,
                                        rate_percent=sample_rate)
        elif provider == "edge":
            info = sgs.synth_edge(text, cfg, TTS_SAMPLE, rate_percent=sample_rate)
        else:
            if sgs.is_real_key(key):
                try:
                    info = sgs.synth_elevenlabs(text, cfg, TTS_SAMPLE,
                                                voice_id=sample_voice, model_id=sample_model,
                                                rate_percent=sample_rate)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"elevenlabs: {type(e).__name__}: {e}")
                    print(f"[WARN] 샘플 ElevenLabs 실패 → edge-tts 시도: {e}")
            if not info:
                info = sgs.synth_edge(text, cfg, TTS_SAMPLE, rate_percent=sample_rate)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"샘플 생성 실패: {type(e).__name__}: {e}", "errors": errors}), 500

    if not TTS_SAMPLE.exists() or TTS_SAMPLE.stat().st_size < 1000:
        return jsonify({"error": "샘플 음성이 생성되지 않았습니다.", "errors": errors}), 500
    duration = sgs.probe_duration(TTS_SAMPLE)
    meta = {
        **info,
        "text": text,
        "selected_voice_ignored": bool(sample_voice) and sample_voice != info.get("voice_id"),
        "duration_sec": round(duration, 2),
        "size_bytes": TTS_SAMPLE.stat().st_size,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "fallback": bool(errors),
        "skipped_providers": errors,
        "key_state": elevenlabs_status().get("state", ""),
    }
    (OUTPUT_DIR / "voice_sample_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return jsonify({"ok": True, "meta": meta})


@app.route("/api/tts/sample")
def api_tts_sample_file():
    if not TTS_SAMPLE.exists():
        abort(404)
    return send_file(TTS_SAMPLE, mimetype="audio/mpeg", conditional=True)


# ----------------------------------------------------------------------
# 장면 이미지 · 말풍선(B-Roll 없이 만들 때)
# ----------------------------------------------------------------------
def _scene_broll_view(scene: dict) -> dict:
    """장면별 B-Roll 설정(없으면 상속/기본)."""
    b = scene.get("broll") or {}
    clip = str(b.get("clip") or "").strip()
    path = Path(core.expand(clip)) if clip else None
    return {
        "enabled": b.get("enabled"),          # None=기본, True=사용, False=안 함
        "clip": clip,
        "label": (broll_lib.label_for(path) if (path and path.exists()) else ""),
        "exists": bool(path and path.exists()),
    }


def generated_videos_dir() -> Path:
    paths = (core.load_config().get("paths", {}) or {})
    return Path(core.expand(paths.get("generated_videos") or str(OUTPUT_DIR / "generated_videos")))


def motion_clip_path(scene: dict, videos_dir: Path | None = None) -> Path | None:
    """이 장면의 WanGP 모션 클립 파일(기록 우선, 없으면 관례 경로)."""
    recorded = str(scene.get("motion_clip") or "").strip()
    if recorded:
        p = Path(core.expand(recorded))
        if p.exists():
            return p
    idx = int(scene.get("index") or 0)
    if not idx:
        return None
    p = (videos_dir or generated_videos_dir()) / f"scene_{idx:02d}_wangp.mp4"
    return p if p.exists() else None


def motion_clip_stale(scene: dict, videos_dir: Path | None = None, tolerance: float = 60.0) -> bool:
    """모션 클립이 옛 장면 이미지로 만든 것인지(이미지보다 오래됨)."""
    clip = motion_clip_path(scene, videos_dir)
    if clip is None:
        return False
    image = str(scene.get("image") or "")
    if not image:
        return False
    img = Path(core.expand(image))
    try:
        return img.exists() and clip.stat().st_mtime + tolerance < img.stat().st_mtime
    except Exception:
        return False


def clip_ready(scene: dict, videos_dir: Path | None = None) -> bool:
    """이 장면의 WanGP 모션 클립을 지금 쓸 수 있는지(옛 이미지 기준이면 False)."""
    if scene.get("motion") is False:
        return False
    return motion_clip_path(scene, videos_dir) is not None and not motion_clip_stale(scene, videos_dir)


def scenes_view() -> dict:
    data = core.read_json("output/scenes/scenes.json") or {}
    sc = core.load_config().get("scenes", {}) or {}
    plan = motion_plan.planned_indices(WANGP_JOBS)
    plan_set = set(plan)
    items = []
    for s in data.get("scenes") or []:
        idx = int(s.get("index") or 0)
        image = str(s.get("image") or "")
        p = Path(core.expand(image)) if image else None
        items.append({
            "index": idx,
            "text": s.get("text", ""),
            "start": s.get("start"),
            "end": s.get("end"),
            "duration": s.get("duration"),
            "bubble_text": s.get("bubble_text", ""),
            "has_image": bool(p and p.exists()),
            "mtime": round(p.stat().st_mtime, 3) if (p and p.exists()) else 0,
            "motion": clip_ready(s),
            "motion_stale": motion_clip_stale(s),
            "motion_planned": idx in plan_set,
            "broll": _scene_broll_view(s),
        })
    return {
        "count": len(items),
        "voice_duration": data.get("voice_duration"),
        "bubble": sc.get("bubble") or {},
        "motion_plan": plan,
        "items": items,
    }


@app.route("/api/scenes")
def api_scenes():
    return jsonify(scenes_view())


@app.route("/api/scenes/image/<int:index>")
def api_scene_image(index: int):
    path = SCENES_DIR / f"scene_{index:02d}.png"
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="image/png", conditional=True, max_age=0)


@app.route("/api/scenes/bubble", methods=["POST"])
def api_scenes_bubble():
    """장면별 말풍선 문구 저장 → 이미지를 다시 생성하지 않고 말풍선만 다시 그린다."""
    payload = request.get_json(silent=True) or {}
    items = payload.get("items")
    if items is None:
        items = [{"index": payload.get("index"), "text": payload.get("text", "")}]
    try:
        data = json.loads(SCENES_JSON.read_text(encoding="utf-8"))
    except Exception:
        return jsonify({"error": "장면이 없습니다. 5단계(장면 생성)를 먼저 실행하세요."}), 400

    wanted = {}
    for it in items or []:
        try:
            wanted[int(it.get("index") or 0)] = str(it.get("text") or "").strip()
        except Exception:
            continue
    if not wanted:
        return jsonify({"error": "적용할 장면 번호가 없습니다."}), 400

    changed = 0
    repaired: list[int] = []
    for scene in data.get("scenes") or []:
        idx = int(scene.get("index") or 0)
        if idx in wanted:
            text = wanted[idx]
            if str(scene.get("bubble_text") or "") != text:
                changed += 1
            # 이전 문구가 비어 있었다면 지금 이미지에는 말풍선이 없다 →
            # 옛 회차의 clean 원본을 지금 이미지로 교체해 둔다(옛 이미지로 되돌아가는 사고 방지)
            if not str(scene.get("bubble_text") or "").strip():
                img = Path(core.expand(str(scene.get("image") or "")))
                clean = img.parent / "clean" / img.name
                try:
                    if (img.exists() and clean.exists()
                            and not bubble_overlay.clean_matches(img, clean)):
                        clean.write_bytes(img.read_bytes())
                        repaired.append(idx)
                except Exception:
                    pass
            scene["bubble_text"] = text
    SCENES_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if repaired:
        print(f"[BUBBLE] 옛 clean 원본을 현재 이미지로 교체: {next(iter(repaired))}" if len(repaired) == 1
              else f"[BUBBLE] 옛 clean 원본을 현재 이미지로 교체: {repaired}")

    step = core.STEP_BY_ID.get("scenes") or {}
    interp = core.step_python(step)
    cmd = [interp, str(PROJECT / "scripts_generate_scenes.py"), "--bubbles-only"]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(cmd, cwd=str(PROJECT), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=180)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"말풍선 적용 실패: {e}"}), 500
    log = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    if r.returncode != 0:
        return jsonify({"error": "말풍선 적용 실패", "log": log[-20:] or [str(r.returncode)]}), 500

    core.reset_step("assemble")   # 이미지가 바뀌었으니 미리보기를 다시 만들어야 한다
    return jsonify({"ok": True, "changed": changed, "scenes": scenes_view(), "log": log[-20:]})


@app.route("/api/scenes/regenerate", methods=["POST"])
def api_scenes_regenerate():
    """장면 이미지 1장만 새 시드로 다시 생성한다.

    같은 시드로는 항상 같은 그림이 나오기 때문에(예: 머리 두 개),
    시드를 옮겨 다시 그린다. 말풍선 문구·모션 계획·B-Roll 설정은 그대로 유지된다.
    """
    payload = request.get_json(silent=True) or {}
    try:
        idx = int(payload.get("index") or 0)
    except Exception:
        idx = 0
    if idx <= 0:
        return jsonify({"error": "다시 만들 장면 번호가 없습니다."}), 400
    if core.is_running():
        return jsonify({"error": "다른 단계가 실행 중입니다. 끝난 뒤에 다시 시도하세요."}), 409

    step = core.STEP_BY_ID.get("scenes") or {}
    interp = core.step_python(step)
    seed_shift = int(time.time()) % 100000
    cmd = [interp, str(PROJECT / "scripts_generate_scenes.py"),
           "--only", str(idx), "--seed-shift", str(seed_shift)]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(cmd, cwd=str(PROJECT), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=900)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"이미지 다시 생성 실패: {e}"}), 500
    log = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    if r.returncode != 0:
        return jsonify({"error": f"{idx}번 이미지 다시 생성 실패", "log": log[-20:] or [str(r.returncode)]}), 500

    core.reset_step("assemble")   # 이미지가 바뀌었으니 미리보기를 다시 만들어야 한다
    core.reset_step("capcut")
    return jsonify({"ok": True, "index": idx, "seed_shift": seed_shift,
                    "scenes": scenes_view(), "log": log[-20:]})


@app.route("/api/scenes/reuse", methods=["POST"])
def api_scenes_reuse():
    """이미 만들어 둔 장면 이미지를 그대로 쓰고 5단계를 '완료'로 표시한다(재생성 없음).

    SDXL 을 다시 돌리면 수 분 + GPU 메모리를 다시 쓰기 때문에, 지금 있는 장면이
    마음에 들면 그대로 6/7/10단계로 넘어갈 수 있게 하는 통로다.
    이미지·말풍선·모션 계획·B-Roll 설정은 하나도 건드리지 않는다.
    """
    if core.is_running():
        return jsonify({"error": "다른 단계가 실행 중입니다. 끝난 뒤에 다시 시도하세요."}), 409

    data = core.read_json("output/scenes/scenes.json") or {}
    scenes = data.get("scenes") or []
    if not scenes:
        return jsonify({"error": "재사용할 장면이 없습니다. 5단계를 한 번은 실행하세요."}), 400

    reused, missing = [], []
    for s in scenes:
        img = str(s.get("image") or "")
        p = Path(core.expand(img)) if img else None
        idx = int(s.get("index") or 0)
        if p and p.exists():
            reused.append(idx)
        else:
            missing.append(idx)
    if not reused:
        return jsonify({"error": "장면 이미지 파일이 하나도 없습니다. 5단계를 실행해 이미지를 만드세요."}), 400

    # 음성이 장면보다 나중에 만들어졌다면 대본이 바뀐 것일 수 있다(막지는 않고 알려만 준다).
    warnings = []
    voice = OUTPUT_DIR / "voice.mp3"
    gen = str(data.get("generated_at") or "").strip()
    try:
        if voice.exists() and gen:
            if voice.stat().st_mtime > datetime.fromisoformat(gen).timestamp() + 2:
                warnings.append("음성(voice.mp3)이 장면 이미지보다 나중에 만들어졌습니다. 대본이 바뀌었다면 장면이 음성과 안 맞을 수 있습니다.")
    except Exception:
        pass

    state = core.load_state()
    st = state["steps"]["scenes"]
    now = datetime.now()
    st["status"] = "done"
    st["returncode"] = 0
    st["error"] = ""
    st["finished_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    if not st.get("started_at"):
        st["started_at"] = st["finished_at"]
    st.setdefault("log", []).append(
        f"[{now.strftime('%H:%M:%S')}] ♻ 기존 장면 이미지 {len(reused)}장 재사용 (새로 생성하지 않음)"
    )
    core.save_state(state)

    return jsonify({
        "ok": True,
        "reused": len(reused),
        "missing": missing,
        "warnings": warnings,
        "scenes": scenes_view(),
        "log": st.get("log", [])[-20:],
    })


# ----------------------------------------------------------------------
# B-Roll 설정 / 창 목록 / MP4 업로드
# ----------------------------------------------------------------------
@app.route("/api/broll/windows")
def api_broll_windows():
    try:
        mod = importlib.import_module("scripts_capture_broll")
        windows = mod.list_windows()
    except Exception as e:  # noqa: BLE001
        return jsonify({"windows": [], "error": str(e)})
    return jsonify({"windows": windows,
                    "current": core.read_json("output/broll_source.json") or {}})


@app.route("/api/broll/config", methods=["POST"])
def api_broll_config():
    payload = request.get_json(silent=True) or {}
    mode = (payload.get("mode") or "desktop").strip()
    if mode not in ("desktop", "window", "file", "none"):
        return jsonify({"error": "mode 는 desktop / window / file / none 중 하나입니다."}), 400

    if mode == "none":
        # B-Roll 을 아예 쓰지 않는다: 6단계 건너뛰고 조립은 장면 전체 화면(scenes 레이아웃)
        cfg = core.load_config()
        cfg.setdefault("broll", {})["enabled"] = False
        asm = cfg.setdefault("assembly", {})
        asm["layout"] = "scenes"
        asm["top_source"] = "none"
        try:
            core.CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"config.json 저장 실패: {e}"}), 500
        data = {
            "mode": "none",
            "window_title": "",
            "process": "",
            "source": "",
            "duration_sec": 0,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        core._atomic_write(BROLL_SOURCE, data)  # noqa: SLF001
        core.reset_step("broll")
        core.reset_step("assemble")
        # 6단계는 '건너뜀'으로 표시 → '다음 단계 실행'이 6단계에서 막히지 않는다
        st = core.load_state()
        st["steps"]["broll"].update(status="skipped", error="B-Roll 사용 안 함 (장면 전체 화면 + 말풍선으로 진행)")
        core.save_state(st)
        return jsonify({"ok": True, "config": data, "broll_enabled": False,
                        "assembly": asm, "message": "B-Roll 사용 안 함 · 조립은 장면 전체 화면"})

    current = core.read_json("output/broll_source.json") or {}
    cfg = core.load_config()
    cfg.setdefault("broll", {})["enabled"] = True
    asm = cfg.setdefault("assembly", {})
    if asm.get("layout") == "scenes":
        asm["layout"] = "split"
    if asm.get("top_source") == "none":
        asm["top_source"] = "broll"
    try:
        core.CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"config.json 저장 실패: {e}"}), 500
    window_title = (payload.get("window_title") or "").strip()
    process = (payload.get("process") or "").strip()
    source = (current.get("source") or "") if mode == "file" else ""
    if mode == "window" and not window_title:
        return jsonify({"error": "녹화할 창을 선택하세요."}), 400
    if mode == "file" and not source:
        return jsonify({"error": "먼저 MP4를 업로드하세요."}), 400

    try:
        duration = max(1, min(120, int(payload.get("duration_sec") or 5)))
    except Exception:
        duration = 5

    if mode == "window" and not process:
        # 프로세스 이름은 창 제목이 바뀌었을 때(브라우저 탭 등) 다시 찾는 데 쓴다
        try:
            mod = importlib.import_module("scripts_capture_broll")
            for w in mod.list_windows():
                if window_title and window_title.lower() in (w.get("title") or "").lower():
                    process = w.get("process") or ""
                    window_title = w.get("title") or window_title
                    break
        except Exception:
            pass

    data = {
        "mode": mode,
        "window_title": window_title,
        "process": process if mode == "window" else "",
        "source": source,
        "duration_sec": duration,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    core._atomic_write(BROLL_SOURCE, data)  # noqa: SLF001
    core.reset_step("broll")
    core.reset_step("assemble")
    return jsonify({"ok": True, "config": data, "broll_enabled": True, "assembly": asm})


@app.route("/api/broll/upload", methods=["POST"])
def api_broll_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "파일이 없습니다."}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in (".mp4", ".mov", ".webm", ".mkv"):
        return jsonify({"error": "MP4/MOV/WEBM/MKV 만 업로드할 수 있습니다."}), 400

    destdir = OUTPUT_DIR / "broll_uploads"
    destdir.mkdir(parents=True, exist_ok=True)
    stem = Path(f.filename).stem[:24] or "clip"
    dest = destdir / f"{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
    f.save(dest)
    if dest.stat().st_size < 10_000:
        dest.unlink(missing_ok=True)
        return jsonify({"error": "파일이 너무 작습니다."}), 400

    current = core.read_json("output/broll_source.json") or {}
    data = {
        "mode": "file",
        "window_title": "",
        "process": "",
        "source": str(dest),
        "duration_sec": int(current.get("duration_sec") or 5),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    core._atomic_write(BROLL_SOURCE, data)  # noqa: SLF001
    core.reset_step("broll")
    clip = None
    try:
        clip = broll_lib.register(dest, label=f"업로드 · {Path(f.filename).stem}", source="file")
    except Exception:
        clip = None
    return jsonify({"ok": True, "path": str(dest), "size": dest.stat().st_size,
                    "config": data, "clip": clip,
                    "clips": broll_lib.list_clips()})


@app.route("/api/broll/clips")
def api_broll_clips():
    """장면별로 고를 수 있는 B-Roll 클립 목록."""
    try:
        clips = broll_lib.list_clips()
    except Exception as e:  # noqa: BLE001
        return jsonify({"clips": [], "error": str(e)})
    return jsonify({"clips": clips, "dir": str(broll_lib.CLIPS_DIR)})


@app.route("/api/broll/clip/record", methods=["POST"])
def api_broll_clip_record():
    """지금 화면에 있는 창을 짧게 녹화해 클립 라이브러리에 넣는다(장면별 B-Roll 용)."""
    payload = request.get_json(silent=True) or {}
    title = (payload.get("window_title") or "").strip()
    if not title:
        return jsonify({"error": "녹화할 창을 선택하세요."}), 400
    try:
        duration = max(1, min(30, int(payload.get("duration_sec") or 5)))
    except Exception:
        duration = 5

    try:
        mod = importlib.import_module("scripts_capture_broll")
        resolved = mod.resolve_window_title(title, (payload.get("process") or "").strip())
        broll_lib.CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        out = broll_lib.CLIPS_DIR / f"clip_{datetime.now().strftime('%Y%m%d_%H%M%S')}_rec.mp4"
        rect = mod.window_rect(resolved)
        path = ""
        if rect:
            path = mod.record_region(rect, duration, str(out))
        if not path:
            fps = int((core.load_config().get("ffmpeg", {}) or {}).get("framerate", 30))
            path = mod.record_screen(duration, str(out), fps, resolved)
        if not path:
            return jsonify({"error": "녹화 실패 — 그 창을 화면에 보이게 두고 다시 시도하세요."}), 500
        clip = broll_lib.register(path, label=f"창 녹화 · {resolved}", source="window")
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"녹화 실패: {type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, "clip": clip, "clips": broll_lib.list_clips()})


@app.route("/api/scenes/broll", methods=["POST"])
def api_scenes_broll():
    """장면별 B-Roll 설정 저장(enabled: true/false/null=기본, clip: 경로)."""
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        return jsonify({"error": "items(장면별 설정)가 필요합니다."}), 400
    try:
        data = json.loads(SCENES_JSON.read_text(encoding="utf-8"))
    except Exception:
        return jsonify({"error": "장면이 없습니다. 5단계(장면 생성)를 먼저 실행하세요."}), 400

    wanted: dict[int, dict] = {}
    for it in items:
        try:
            idx = int(it.get("index") or 0)
        except Exception:
            continue
        if not idx:
            continue
        raw = it.get("enabled")
        enabled = None if raw in (None, "", "inherit") else bool(raw)
        wanted[idx] = {"enabled": enabled, "clip": str(it.get("clip") or "").strip()}
    if not wanted:
        return jsonify({"error": "적용할 장면 번호가 없습니다."}), 400

    changed = 0
    for scene in data.get("scenes") or []:
        idx = int(scene.get("index") or 0)
        if idx in wanted:
            new = wanted[idx]
            old = scene.get("broll") or {}
            if (old.get("enabled"), str(old.get("clip") or "")) != (new["enabled"], new["clip"]):
                changed += 1
            scene["broll"] = new
    SCENES_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    core.reset_step("assemble")
    return jsonify({"ok": True, "changed": changed, "scenes": scenes_view()})


@app.route("/api/scenes/broll-mode", methods=["POST"])
def api_scenes_broll_mode():
    """전체 동일 B-Roll / 장면마다 B-Roll 전환."""
    payload = request.get_json(silent=True) or {}
    if (payload.get("per_scene") is None) and ("apply" not in payload):
        return jsonify({"error": "per_scene(true/false) 가 필요합니다."}), 400
    per_scene = payload.get("per_scene")
    if per_scene is None:
        per_scene = str(payload.get("apply")).lower() == "per_scene"
    cfg = core.load_config()
    asm = cfg.setdefault("assembly", {})
    asm["per_scene_broll"] = bool(per_scene)
    if asm["per_scene_broll"]:
        cfg.setdefault("broll", {})["enabled"] = True
    try:
        core.CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"config.json 저장 실패: {e}"}), 500
    core.reset_step("assemble")
    return jsonify({"ok": True, "assembly": asm,
                    "message": "장면마다 B-Roll" if asm["per_scene_broll"] else "전체에 같은 B-Roll"})


# ----------------------------------------------------------------------
# WanGP: 서버 제어 + 영상 생성
# ----------------------------------------------------------------------
_WANGP_LOG_HANDLE = {"fh": None}


# ----------------------------------------------------------------------
# 유튜브 업로드 문구 (제목/설명/태그/썸네일 추천)
# ----------------------------------------------------------------------
def youtube_view() -> dict:
    cfg = core.load_config()
    yt = cfg.get("youtube", {}) or {}
    final_dir = Path(core.expand((cfg.get("paths", {}) or {}).get(
        "final_output", str(OUTPUT_DIR / "final"))))
    video = None
    if final_dir.exists():
        files = [p for p in final_dir.glob("*.mp4") if p.is_file()]
        if files:
            latest = max(files, key=lambda p: p.stat().st_mtime)
            video = {"name": latest.name, "mtime": latest.stat().st_mtime,
                     "size": latest.stat().st_size}
    meta = youtube_meta.load() or {}
    token_path = core.expand(yt.get("token_path", "") or "")
    auth = core.read_json("output/youtube_auth.json") or {}
    return {
        "enabled": bool(yt.get("enabled", False)),
        "privacy": yt.get("default_privacy", "private"),
        "meta": meta or None,
        "video": video,
        "published": bool(meta.get("created_at")),
        "token": bool(token_path) and Path(token_path).exists(),
        "token_path": str(token_path) if token_path else "",
        # 마지막 인증 확인 결과 (upload/reauth 가 남긴다) — 파일 존재만으로는 만료를 알 수 없다
        "auth_ok": (auth.get("ok") if auth else None),
        "auth_detail": (auth.get("detail") or "") if auth else "",
        "auth_checked_at": (auth.get("checked_at") or "") if auth else "",
    }


@app.route("/api/youtube")
def api_youtube():
    return jsonify(youtube_view())


@app.route("/api/youtube/auth", methods=["POST"])
def api_youtube_auth():
    """만료·폐기(invalid_grant)된 YouTube OAuth 토큰을 새로 받는다.

    승인은 브라우저에서 사람이 해야 하고 인증 서버가 localhost 포트를 잡으므로,
    Flask 스레드에서 돌리지 않고 **별도 콘솔 창**에서 실행한다(로그도 그 창에 보임).
    """
    script = PROJECT / "scripts_publish_youtube.py"
    flags = 0
    if os.name == "nt":
        flags = 0x00000010  # CREATE_NEW_CONSOLE: 사용자가 승인 안내를 볼 수 있게
    try:
        subprocess.Popen(
            [sys.executable, str(script), "--reauth", "--auth-check"],
            cwd=str(PROJECT),
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                 "PYTHONUNBUFFERED": "1"},
            creationflags=flags,
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"인증 창을 열지 못했습니다: {type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True,
                    "hint": "열린 콘솔 창의 안내대로 브라우저에서 '허용'을 누르면 끝납니다."})


@app.route("/api/youtube/meta/generate", methods=["POST"])
def api_youtube_meta_generate():
    """대본과 별도로 설명문구·제목·태그·썸네일 추천을 LLM 으로 생성한다."""
    payload = request.get_json(silent=True) or {}
    use_llm = payload.get("llm", True) is not False
    try:
        data = youtube_meta.generate(core.load_config(), use_llm=use_llm)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"문구 생성 실패: {type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, "meta": data, "youtube": youtube_view()})


@app.route("/api/youtube/meta", methods=["POST"])
def api_youtube_meta_save():
    payload = request.get_json(silent=True) or {}
    try:
        data = youtube_meta.update(
            title=payload.get("title"),
            description=payload.get("description"),
            tags=payload.get("tags"),
            thumbnail=payload.get("thumbnail"),
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"저장 실패: {type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, "meta": data, "youtube": youtube_view()})


@app.route("/api/assembly", methods=["POST"])
def api_assembly():
    """조립 레이아웃 / WanGP·B-Roll 역할 설정"""
    payload = request.get_json(silent=True) or {}
    cfg = core.load_config()
    asm = cfg.setdefault("assembly", {})
    if payload.get("layout") in ("split", "full", "scenes"):
        asm["layout"] = payload["layout"]
        if payload["layout"] == "scenes":
            cfg.setdefault("broll", {})["enabled"] = False
            asm["top_source"] = "none"
        elif not bool((cfg.get("broll", {}) or {}).get("enabled", True)):
            cfg.setdefault("broll", {})["enabled"] = True
    if payload.get("top_source") in ("broll", "wangp", "none"):
        asm["top_source"] = payload["top_source"]
    if payload.get("bottom_source") in ("scenes", "wangp", "broll", "assets", "auto"):
        asm["bottom_source"] = payload["bottom_source"]

    # 장면 화면 맞춤 + B-Roll 칸 높이 (9단계 CapCut 초안도 같은 값으로 만든다)
    raw_fit = (payload.get("scene_fit") or "").strip().lower()
    if raw_fit in ("cover_top", "cover_center", "contain"):
        asm["scene_fit"], asm["scene_anchor"] = raw_fit.split("_", 1) if raw_fit.startswith("cover_") else ("contain", "center")
    else:
        if raw_fit in ("cover", "contain"):
            asm["scene_fit"] = raw_fit
        anchor = (payload.get("scene_anchor") or "").strip().lower()
        if anchor in ("top", "center"):
            asm["scene_anchor"] = anchor
    if payload.get("top_ratio") is not None:
        try:
            ratio = float(payload["top_ratio"])
            if ratio > 1:               # 50 같은 퍼센트로 들어와도 받는다
                ratio = ratio / 100.0
            asm["top_ratio"] = round(max(0.3, min(0.75, ratio)), 2)
        except Exception:
            pass
    try:
        core.CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"config.json 저장 실패: {e}"}), 500
    # 화면 배치가 바뀌면 미리보기 MP4(10단계)와 CapCut 초안(9단계) 둘 다 다시 만들어야 한다
    core.reset_step("assemble")
    core.reset_step("capcut")
    return jsonify({"ok": True, "assembly": asm})


@app.route("/api/layout/preview", methods=["POST"])
def api_layout_preview():
    """저장하기 전에도 화면 배치를 확인할 수 있게 샘플 프레임 1장을 만든다.

    10단계 영상과 **같은 필터**(scripts_assemble_shorts.scene_fit_chains)를 쓰기 때문에
    여기서 보이는 그대로 MP4/CapCut 초안이 나온다. 설정은 바꾸지 않는다.
    """
    payload = request.get_json(silent=True) or {}
    cfg = core.load_config()

    fit = (payload.get("scene_fit") or "").strip().lower() or None
    anchor = None
    if fit and fit.startswith("cover_"):
        anchor = fit.split("_", 1)[1]
        fit = "cover"
    elif fit == "contain":
        anchor = "center"

    ratio = payload.get("top_ratio")
    try:
        ratio_val = float(ratio) if ratio is not None else None
        if ratio_val is not None and ratio_val > 1:
            ratio_val = ratio_val / 100.0
    except Exception:
        ratio_val = None

    try:
        idx = int(payload["index"]) if payload.get("index") else None
    except Exception:
        idx = None

    try:
        mod = importlib.import_module("scripts_assemble_shorts")
        info = mod.render_layout_sample(cfg, LAYOUT_PREVIEW, index=idx, fit=fit,
                                        anchor=anchor, top_ratio=ratio_val)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"샘플 생성 실패: {type(e).__name__}: {e}"}), 500

    info = {k: v for k, v in info.items() if k != "path"}
    info["ok"] = True
    info["url"] = f"/api/layout/preview/image?t={int(time.time() * 1000)}"
    return jsonify(info)


@app.route("/api/layout/preview/image")
def api_layout_preview_image():
    if not LAYOUT_PREVIEW.exists():
        abort(404)
    resp = send_file(LAYOUT_PREVIEW, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/api/wangp")
def api_wangp():
    return jsonify(wangp_status())


@app.route("/api/wangp/video/<int:index>")
def api_wangp_video(index: int):
    gen_dir = Path(core.expand((core.load_config().get("paths", {}) or {}).get(
        "generated_videos", str(OUTPUT_DIR / "generated_videos"))))
    clips = (list(gen_dir.glob("wangp_*.mp4")) + list(gen_dir.glob("scene_*_wangp.mp4"))) if gen_dir.exists() else []
    files = sorted(clips, key=lambda x: x.stat().st_mtime, reverse=True)
    if index < 0 or index >= len(files):
        abort(404)
    return send_file(files[index])


@app.route("/api/wangp/motion", methods=["POST"])
def api_wangp_motion():
    """7단계에서 움직일 장면을 웹에서 고른다 → output/wangp_jobs.json 재생성.

    모션 입력 이미지는 말풍선 없는 clean 원본이라, 영상 위에 말풍선이 섞이지 않는다.
    """
    payload = request.get_json(silent=True) or {}
    if not SCENES_JSON.exists():
        return jsonify({"error": "장면이 없습니다. 5단계(장면 생성)를 먼저 실행하세요."}), 400
    raw = payload.get("indices")
    if raw is None:
        return jsonify({"error": "indices(장면 번호 목록)가 필요합니다."}), 400
    if not isinstance(raw, list):
        return jsonify({"error": "indices 는 배열이어야 합니다."}), 400

    indices: list[int] = []
    for v in raw:
        try:
            indices.append(int(v))
        except Exception:
            continue
    try:
        plan = motion_plan.write_plan(SCENES_JSON, WANGP_JOBS, core.load_config(),
                                     sorted(set(indices)))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"모션 계획 저장 실패: {type(e).__name__}: {e}"}), 500

    # 계획이 바뀌면 7단계(모션)와 10단계(조립)를 다시 실행해야 한다
    core.reset_step("wangp")
    core.reset_step("assemble")
    return jsonify({"ok": True, "count": plan["count"], "indices": plan["indices"],
                    "scenes": scenes_view(), "wangp": wangp_status()})


@app.route("/api/wangp/motion/plan")
def api_wangp_motion_plan():
    return jsonify({"plan": motion_plan.planned_indices(WANGP_JOBS),
                    "jobs": motion_plan.load_plan(WANGP_JOBS)})


@app.route("/api/wangp/server/start", methods=["POST"])
def api_wangp_server_start():
    s = wangp_settings()
    pid = _read_pid(WANGP_SERVER_PID)
    if _pid_alive(pid):
        return jsonify({"ok": True, "pid": pid, "already": True, "url": s["base_url"]})
    if not s["python"].exists():
        return jsonify({"error": f"WanGP 파이썬이 없습니다: {s['python']}"}), 400
    if not (s["root"] / "wgp.py").exists():
        return jsonify({"error": f"WanGP 경로가 올바르지 않습니다: {s['root']}"}), 400

    from urllib.parse import urlparse

    port = urlparse(s["base_url"]).port or 7860
    cmd = [str(s["python"]), str(s["root"] / "wgp.py"),
           "--listen", "--server-name", "127.0.0.1", "--server-port", str(port)]
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        WANGP_SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
        fh = open(WANGP_SERVER_LOG, "ab")
        proc = subprocess.Popen(cmd, cwd=str(s["root"]), stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags)
        _WANGP_LOG_HANDLE["fh"] = fh
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"서버 시작 실패: {e}"}), 500

    WANGP_SERVER_PID.write_text(str(proc.pid), encoding="utf-8")
    return jsonify({"ok": True, "pid": proc.pid, "url": s["base_url"], "log": str(WANGP_SERVER_LOG)})


@app.route("/api/wangp/server/stop", methods=["POST"])
def api_wangp_server_stop():
    pid = _read_pid(WANGP_SERVER_PID)
    if not _pid_alive(pid):
        WANGP_SERVER_PID.unlink(missing_ok=True)
        return jsonify({"ok": True, "already": True})
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=40)
    WANGP_SERVER_PID.unlink(missing_ok=True)
    fh = _WANGP_LOG_HANDLE.get("fh")
    if fh:
        try:
            fh.close()
        except Exception:
            pass
        _WANGP_LOG_HANDLE["fh"] = None
    return jsonify({"ok": True, "pid": pid})


@app.route("/api/wangp/generate", methods=["POST"])
def api_wangp_generate():
    payload = request.get_json(silent=True) or {}
    prompt = (payload.get("prompt") or "").strip()
    if len(prompt) < 5:
        return jsonify({"error": "프롬프트를 5자 이상 입력하세요."}), 400

    s = wangp_settings()
    if not s["python"].exists():
        return jsonify({"error": f"WanGP 파이썬이 없습니다: {s['python']}"}), 400

    if _refresh_wangp_job_state().get("status") == "running" or core.is_running("wangp"):
        return jsonify({"error": "이미 WanGP 생성이 진행 중입니다."}), 409

    wp = s["cfg"]
    try:
        video_length = max(25, min(241, int(payload.get("video_length") or wp.get("video_length", 81))))
        steps = max(1, min(30, int(payload.get("num_inference_steps") or wp.get("num_inference_steps", 4))))
    except Exception:
        return jsonify({"error": "프레임/스텝 값이 올바르지 않습니다."}), 400

    job = {
        "prompt": prompt,
        "resolution": (payload.get("resolution") or wp.get("resolution") or "480x832").strip(),
        "video_length": video_length,
        "num_inference_steps": steps,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    core._atomic_write(WANGP_JOB, job)  # noqa: SLF001
    core.reset_step("wangp")
    try:
        pid = _start_wangp_generation()
    except Exception as e:  # noqa: BLE001
        _mark_step("wangp", "failed", f"WanGP 실행 실패: {e}")
        return jsonify({"error": f"WanGP 실행 실패: {e}"}), 500
    return jsonify({"ok": True, "job": job, "pid": pid})


@app.route("/api/open_folder", methods=["POST"])
def api_open_folder():
    payload = request.get_json(silent=True) or {}
    target = (payload.get("path") or "").strip()
    if not target:
        return jsonify({"error": "path 가 필요합니다."}), 400
    p = Path(core.expand(target))
    allowed = [PROJECT, Path(core.expand(r"%USERPROFILE%\릴스자료")), Path(core.expand(r"%LOCALAPPDATA%\CapCut"))]
    if not any(str(p).lower().startswith(str(r).lower()) for r in allowed):
        return jsonify({"error": "허용되지 않은 경로입니다."}), 403
    if not p.exists():
        return jsonify({"error": "경로가 없습니다."}), 404
    try:
        subprocess.Popen(["explorer.exe", str(p)])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _port_in_use(host: str, port: int) -> bool:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


if __name__ == "__main__":
    core.recover_interrupted()
    host = os.getenv("RACCOON_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("RACCOON_WEB_PORT", "5000"))

    # Windows 는 SO_REUSEADDR 때문에 같은 포트에 여러 번 바인딩되어
    # 응답이 간헐적으로 끊기는 문제가 생긴다 → 이미 떠 있으면 친절하게 종료.
    if _port_in_use(host, port):
        print(f"[ERROR] {host}:{port} 에 이미 서버가 실행 중입니다.")
        print(f"        브라우저에서 http://{host}:{port} 를 열어 사용하세요.")
        print(f"        다른 포트로 띄우려면: set RACCOON_WEB_PORT=5001")
        sys.exit(1)
    print("=" * 60)
    print(f" 너구리 쇼츠 웹 파이프라인 → http://{host}:{port}")
    print(f" 프로젝트: {PROJECT}")
    print("=" * 60)
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
