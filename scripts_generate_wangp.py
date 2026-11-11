"""
[단계 6] WanGP 영상 생성 (헤드리스 API)
=========================================
WanGP 의 Python API(shared.api)를 직접 호출한다. WanGP 웹UI(7860)를 켜지 않아도 된다.

실행은 반드시 WanGP 가상환경 파이썬으로 한다 (config.wangp.python).
  venv/Scripts/python.exe scripts_generate_wangp.py

입력
- output/wangp_job.json  (웹 UI가 저장. 없으면 config.wangp 기본값)
    {"prompt": "...", "resolution": "480x832", "video_length": 81, "num_inference_steps": 4}
- config.json > wangp

출력
- output/generated_videos/wangp_*.mp4   (WanGP outputs 에서 복사)
- output/wangp_result.json              (성공 여부/모델/설정/파일/소요시간)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT / "config.json"
OUTPUT_DIR = PROJECT / "output"
JOB_FILE = OUTPUT_DIR / "wangp_job.json"
RESULT_FILE = OUTPUT_DIR / "wangp_result.json"

try:
    # 웹 UI 가 로그를 실시간으로 보려면 line buffering 이 필수
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


def expand(path: str) -> str:
    return os.path.normpath(os.path.expandvars(path))


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_result(payload: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def pick_model_type(session, hint: str) -> str:
    """'TextImage2video 5B' 처럼 사람이 아는 이름으로 model_type 을 찾는다."""
    try:
        records = session.list_model_metadata()
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 모델 목록 조회 실패: {e}")
        return ""
    if isinstance(records, dict):
        records = list(records.values())

    def name_of(r: dict) -> str:
        return str(r.get("name") or (r.get("metadata") or {}).get("name") or "")

    matches = [r for r in records if hint.lower() in name_of(r).lower()]
    if not matches:
        matches = [r for r in records if "ti2v" in str(r.get("model_type", "")).lower()]
    if not matches:
        print("[WARN] 일치하는 모델을 찾지 못했습니다. 사용 가능한 예: "
              + ", ".join(str(r.get("model_type")) for r in records[:12]))
        return ""
    pref = [r for r in matches if "5B" in name_of(r).upper() or "5b" in str(r.get("model_type")).lower()]
    chosen = (pref or matches)[0]
    print(f"[MODEL] '{name_of(chosen)}' → model_type={chosen.get('model_type')}")
    return str(chosen.get("model_type") or "")


def build_settings(session, config: dict, job: dict) -> dict:
    wp = config.get("wangp", {}) or {}
    model_type = (job.get("model_type") or wp.get("model_type") or "auto").strip()
    if model_type in ("", "auto"):
        model_type = pick_model_type(session, wp.get("model_name_hint", "TextImage2video 5B"))
    if not model_type:
        raise RuntimeError("WanGP model_type 을 찾지 못했습니다. config.wangp.model_type 에 직접 지정하세요.")

    try:
        settings = session.get_default_settings(model_type) or {}
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 기본 설정 조회 실패({e}) → 최소 설정으로 진행")
        settings = {}
    settings["model_type"] = model_type

    prompt = (job.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("프롬프트가 비어 있습니다. 영어 프롬프트를 입력하세요.")
    settings["prompt"] = prompt
    settings["resolution"] = str(job.get("resolution") or wp.get("resolution") or "480x832")
    settings["video_length"] = int(job.get("video_length") or wp.get("video_length") or 81)
    settings["num_inference_steps"] = int(job.get("num_inference_steps") or wp.get("num_inference_steps") or 4)
    if job.get("guidance_scale") is not None or wp.get("guidance_scale") is not None:
        settings["guidance_scale"] = float(job.get("guidance_scale", wp.get("guidance_scale")))
    if job.get("flow_shift") is not None or wp.get("flow_shift") is not None:
        settings["flow_shift"] = float(job.get("flow_shift", wp.get("flow_shift")))

    # FastWan LoRA (파일이 실제로 있을 때만)
    lora = (wp.get("fastwan_lora") or "").strip()
    root = Path(expand(wp.get("root", r"E:\WanGP")))
    if lora:
        found = list((root / "loras").rglob(Path(lora).name)) if (root / "loras").exists() else []
        if found:
            settings["activated_loras"] = [str(found[0])]
            settings.setdefault("loras_multipliers", "1")
            print(f"[LORA] {Path(found[0]).name}")
        else:
            print(f"[WARN] LoRA 파일을 찾지 못했습니다: {lora} (기본 모델로 생성)")
    return settings


def main() -> int:
    ap = argparse.ArgumentParser(description="WanGP 영상 생성 (헤드리스)")
    ap.add_argument("--prompt", default=None)
    ap.add_argument("--resolution", default=None)
    ap.add_argument("--video-length", type=int, default=None)
    ap.add_argument("--steps", type=int, default=None)
    ap.add_argument("--model-type", default=None)
    args = ap.parse_args()

    config = load_config()
    wp = config.get("wangp", {}) or {}
    root = Path(expand(wp.get("root", r"E:\WanGP")))
    if not (root / "wgp.py").exists():
        print(f"[ERROR] WanGP 경로가 올바르지 않습니다: {root}")
        write_result({"success": False, "error": f"WanGP root 없음: {root}"})
        return 1

    job = read_json(JOB_FILE) if JOB_FILE.exists() else {}
    if args.prompt:
        job["prompt"] = args.prompt
    if args.resolution:
        job["resolution"] = args.resolution
    if args.video_length:
        job["video_length"] = args.video_length
    if args.steps:
        job["num_inference_steps"] = args.steps
    if args.model_type:
        job["model_type"] = args.model_type

    started = time.time()
    print(f"[WANGP] root={root}")
    print(f"[WANGP] prompt={job.get('prompt', '')[:120]}")
    print(f"[WANGP] resolution={job.get('resolution') or wp.get('resolution')} "
          f"frames={job.get('video_length') or wp.get('video_length')} "
          f"steps={job.get('num_inference_steps') or wp.get('num_inference_steps')}")

    # WanGP 모듈은 root 를 기준으로 import 되어야 한다.
    sys.path.insert(0, str(root))
    os.chdir(root)

    try:
        from shared.api import init  # noqa: WPS433 (WanGP 내부 모듈)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] WanGP API import 실패: {e}")
        write_result({"success": False, "error": f"WanGP shared.api import 실패: {e}"})
        return 1

    console_mode = os.getenv("RACCOON_WANGP_CONSOLE") == "1"
    cli_args: list[str] = []
    profile = wp.get("profile")
    if profile:
        cli_args += ["--profile", str(profile)]
    reserved = wp.get("reserved_mem_max")
    if reserved:
        # 16GB 환경에서 다른 앱과 같이 쓰면 기본 40% 로는 RAM 부족이 난다
        cli_args += ["--perc-reserved-mem-max", str(reserved)]
    attention = (wp.get("attention") or "").strip()
    if attention:
        cli_args += ["--attention", attention]
    print(f"[WANGP] cli_args={cli_args}")
    try:
        session = init(root=root, cli_args=cli_args or None, console_output=console_mode)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] WanGP 초기화 실패: {e}")
        traceback.print_exc()
        write_result({"success": False, "error": f"WanGP 초기화 실패: {e}"})
        return 1

    try:
        settings = build_settings(session, config, job)
        compact = {k: settings.get(k) for k in (
            "model_type", "resolution", "video_length", "num_inference_steps",
            "guidance_scale", "flow_shift", "seed", "activated_loras")}
        print(f"[WANGP] 최종 설정: {json.dumps(compact, ensure_ascii=False)}")
        job_handle = session.submit_task(settings)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 작업 생성 실패: {e}")
        traceback.print_exc()
        write_result({"success": False, "error": f"작업 생성 실패: {e}"})
        return 1

    last_pct = -10
    try:
        for event in job_handle.events.iter(timeout=0.5):
            kind = getattr(event, "kind", "")
            if kind == "progress":
                p = event.data
                pct = int((getattr(p, "progress", 0) or 0) * 100)
                if pct >= last_pct + 5:
                    last_pct = pct
                    step = getattr(p, "current_step", "?")
                    total = getattr(p, "total_steps", "?")
                    phase = getattr(p, "phase", "")
                    print(f"[PROGRESS] {pct}% ({step}/{total}) {phase}")
            elif kind == "status":
                print(f"[STATUS] {event.data}")
            elif kind == "error":
                print(f"[ERROR] {event.data}")
            elif kind == "stream":
                line = getattr(event.data, "text", "") or ""
                if any(k in line for k in ("Error", "error", "CUDA", "Exception", "Traceback")):
                    print(f"[LOG] {line.strip()[:300]}")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 이벤트 스트림 오류: {e}")

    try:
        result = job_handle.result()
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 결과 수신 실패: {e}")
        traceback.print_exc()
        write_result({"success": False, "error": f"결과 수신 실패: {e}"})
        return 1

    elapsed = round(time.time() - started, 1)
    errors = [getattr(e, "message", str(e)) for e in (getattr(result, "errors", []) or [])]
    if not getattr(result, "success", False):
        print("[FAIL] WanGP 생성 실패")
        for e in errors:
            print(f"  - {e}")
        write_result({"success": False, "error": "; ".join(errors) or "알 수 없는 오류",
                      "settings": {k: v for k, v in settings.items() if k != "prompt"},
                      "prompt": settings.get("prompt", ""), "elapsed_sec": elapsed})
        return 1

    files = [Path(str(f)) for f in (getattr(result, "generated_files", []) or [])]
    video_files = [f for f in files if f.suffix.lower() in (".mp4", ".webm", ".mov", ".mkv")]
    if not video_files:
        print(f"[FAIL] 생성 파일이 없습니다: {files}")
        write_result({"success": False, "error": "생성된 영상 파일이 없습니다.",
                      "settings": {k: v for k, v in settings.items() if k != "prompt"},
                      "elapsed_sec": elapsed})
        return 1

    out_dir = Path(expand((config.get("paths", {}) or {}).get("generated_videos",
                                                             str(OUTPUT_DIR / "generated_videos"))))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    copies = []
    for i, f in enumerate(video_files):
        if not f.exists():
            print(f"[WARN] 결과 파일이 없습니다: {f}")
            continue
        dest = out_dir / f"wangp_{stamp}{'' if i == 0 else f'_{i}'}{f.suffix.lower()}"
        shutil.copy2(f, dest)
        copies.append(str(dest))
        print(f"[OK] {dest} ({dest.stat().st_size/1024:.0f}KB)")

    write_result({
        "success": bool(copies),
        "model_type": settings.get("model_type"),
        "prompt": settings.get("prompt", ""),
        "settings": {k: v for k, v in settings.items() if k != "prompt"},
        "files": copies,
        "source_files": [str(f) for f in video_files],
        "elapsed_sec": elapsed,
        "errors": errors,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    if not copies:
        return 1
    print(f"[DONE] WanGP 생성 완료 · {elapsed}초")
    return 0


if __name__ == "__main__":
    sys.exit(main())
