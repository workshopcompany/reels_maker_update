"""
[단계 7] WanGP 장면 모션 생성 (하이브리드, 선택)
==================================================
output/wangp_jobs.json 의 장면 이미지를 Wan 2.2 TI2V 로 짧게 움직인다.
(이미지 → 영상: image_start, image_prompt_type="S")

- 결과: output/generated_videos/scene_XX_wangp.mp4
- output/scenes/scenes.json 의 해당 장면에 motion_clip 을 기록
- 입력 이미지는 말풍선이 없는 clean 원본(output/scenes/clean/)이다. 말풍선을 넣은 이미지로
  영상을 만들면 WanGP 가 다시 그리는 과정에서 글자가 뭉개지므로, 말풍선은 10단계 조립에서
  영상 위에 투명 레이어로 따로 얹는다.
- 어떤 장면을 움직일지는 output/wangp_jobs.json 이 결정한다(웹 '장면별 모션'에서 변경).
- 실패해도 치명적이지 않다. 미리보기(10단계)는 정지 이미지 + 줌/팬으로 계속 진행한다.

실행은 WanGP 전용 파이썬으로 한다.
  venv/Scripts/python.exe scripts_generate_wangp_motion.py
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import scripts_generate_wangp as sgw

PROJECT = sgw.PROJECT
OUTPUT_DIR = sgw.OUTPUT_DIR
JOBS_FILE = OUTPUT_DIR / "wangp_jobs.json"
RESULT_FILE = OUTPUT_DIR / "wangp_result.json"
SCENES_FILE = OUTPUT_DIR / "scenes" / "scenes.json"
VIDEO_DIR = OUTPUT_DIR / "generated_videos"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass


def load_jobs() -> list[dict]:
    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return [j for j in (jobs or []) if isinstance(j, dict) and j.get("image")]


def update_scene_clip(scene_index: int, clip: Path | str) -> None:
    try:
        data = json.loads(SCENES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    for scene in data.get("scenes") or []:
        if int(scene.get("index") or 0) == int(scene_index):
            scene["motion_clip"] = str(clip)
            scene["motion"] = True
    SCENES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_failure(message: str, jobs: list[dict], done: list[dict]) -> None:
    sgw.write_result({
        "success": bool(done),
        "error": message,
        "jobs": jobs,
        "files": [d.get("file") for d in done if d.get("file")],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })


def run_job(session, config: dict, job: dict) -> Path:
    idx = int(job.get("scene_index") or 0)
    image = Path(sgw.expand(str(job.get("image") or "")))
    if not image.exists():
        raise RuntimeError(f"{idx}번 장면 이미지가 없습니다: {image}")

    settings = sgw.build_settings(session, config, job)
    settings["image_start"] = [str(image)]
    settings["image_prompt_type"] = "S"
    settings["seed"] = int(job.get("seed") or -1)
    if job.get("negative_prompt"):
        settings["negative_prompt"] = str(job["negative_prompt"])
    print(f"[MOTION {idx}] image={image.name} · {settings.get('resolution')} · "
          f"{settings.get('video_length')}f · {settings.get('num_inference_steps')} steps")

    handle = session.submit_task(settings)
    last_pct = -10
    for event in handle.events.iter(timeout=0.5):
        kind = getattr(event, "kind", "")
        if kind == "progress":
            p = event.data
            pct = int((getattr(p, "progress", 0) or 0) * 100)
            if pct >= last_pct + 10:
                last_pct = pct
                print(f"[MOTION {idx}] {pct}% "
                      f"({getattr(p, 'current_step', '?')}/{getattr(p, 'total_steps', '?')})")
        elif kind == "error":
            print(f"[MOTION {idx}] ERROR {event.data}")
        elif kind == "stream":
            line = getattr(event.data, "text", "") or ""
            if any(k in line for k in ("Error", "CUDA", "Exception", "Traceback")):
                print(f"[MOTION {idx}] {line.strip()[:240]}")

    result = handle.result()
    errors = [getattr(e, "message", str(e)) for e in (getattr(result, "errors", []) or [])]
    if not getattr(result, "success", False):
        raise RuntimeError("; ".join(errors) or "WanGP 생성 실패")

    files = [Path(str(f)) for f in (getattr(result, "generated_files", []) or [])]
    videos = [f for f in files if f.suffix.lower() in (".mp4", ".webm", ".mov", ".mkv")]
    if not videos:
        raise RuntimeError("생성된 영상 파일이 없습니다.")

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    dest = VIDEO_DIR / f"scene_{idx:02d}_wangp.mp4"
    shutil.copy2(videos[0], dest)
    update_scene_clip(idx, dest)
    print(f"[MOTION {idx}] OK → {dest} ({dest.stat().st_size/1024:.0f}KB)")
    return dest


def main() -> int:
    jobs = load_jobs()
    if not jobs:
        print("[SKIP] output/wangp_jobs.json 에 모션 작업이 없습니다. (정지 이미지 + 줌/팬으로 진행)")
        sgw.write_result({
            "success": True,
            "skipped": True,
            "reason": "모션 작업 없음 — 장면 이미지 + 줌/팬으로 진행",
            "files": [],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
        return 0

    config = sgw.load_config()
    wp = config.get("wangp", {}) or {}
    root = Path(sgw.expand(wp.get("root", r"E:\WanGP")))
    if not (root / "wgp.py").exists():
        print(f"[ERROR] WanGP 경로가 올바르지 않습니다: {root}")
        save_failure(f"WanGP root 없음: {root}", jobs, [])
        return 1

    sys.path.insert(0, str(root))
    import os
    os.chdir(root)
    try:
        from shared.api import init  # noqa: WPS433
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] WanGP API import 실패: {e}")
        save_failure(f"WanGP shared.api import 실패: {e}", jobs, [])
        return 1

    cli_args: list[str] = []
    if wp.get("profile"):
        cli_args += ["--profile", str(wp["profile"])]
    if wp.get("reserved_mem_max"):
        cli_args += ["--perc-reserved-mem-max", str(wp["reserved_mem_max"])]
    if (wp.get("attention") or "").strip():
        cli_args += ["--attention", str(wp["attention"]).strip()]

    print("=" * 70)
    print(f"[MOTION] 장면 {len(jobs)}개 · WanGP img2video · "
          f"예정 {', '.join(str(j.get('scene_index')) for j in jobs)}")
    print("[MOTION] 입력: 말풍선 없는 clean 원본 · 말풍선은 10단계 조립에서 영상 위에 얹습니다")
    print(f"[MOTION] 루트 {root} · args={cli_args}")
    print("[HINT] WanGP 웹서버(7860)가 켜져 있으면 VRAM 부족으로 실패할 수 있습니다.")
    print("=" * 70)

    try:
        session = init(root=root, cli_args=cli_args or None,
                       console_output=os.getenv("RACCOON_WANGP_CONSOLE") == "1")
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] WanGP 초기화 실패: {e}")
        traceback.print_exc()
        save_failure(f"WanGP 초기화 실패: {e}", jobs, [])
        return 1

    done: list[dict] = []
    errors: list[str] = []
    started = time.time()
    for job in jobs:
        idx = int(job.get("scene_index") or 0)
        t0 = time.time()
        try:
            dest = run_job(session, config, job)
            done.append({"scene_index": idx, "file": str(dest),
                         "elapsed_sec": round(time.time() - t0, 1)})
        except Exception as e:  # noqa: BLE001
            msg = f"{idx}번: {type(e).__name__}: {e}"
            print(f"[WARN] {msg}")
            traceback.print_exc()
            errors.append(msg)

    elapsed = round(time.time() - started, 1)
    payload = {
        "success": bool(done),
        "mode": "scene_motion",
        "jobs": jobs,
        "done": done,
        "errors": errors,
        "files": [d.get("file") for d in done if d.get("file")],
        "elapsed_sec": elapsed,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    sgw.write_result(payload)
    print("=" * 70)
    print(f"[MOTION] 완료 {len(done)}/{len(jobs)} · {elapsed}s")
    if errors:
        print("[MOTION] 실패: " + " | ".join(errors))
    print("[NEXT] 10단계 미리보기에서 모션 클립이 자동으로 사용됩니다.")
    return 0 if done or not jobs else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        print(f"[ERROR] 모션 생성 실패: {type(e).__name__}: {e}")
        sys.exit(1)
