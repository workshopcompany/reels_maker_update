"""
[공용] WanGP 장면 모션 계획 (어떤 장면을 img2video 로 움직일지)
================================================================
- 5단계(scripts_generate_scenes.py)가 기본 계획을 만들고,
  웹 UI('🎬 WanGP → 장면별 모션')에서 장면을 골라 다시 계획할 수 있다.
  7단계(scripts_generate_wangp_motion.py)는 output/wangp_jobs.json 만 보고 작업한다.

중요
- 모션은 **말풍선이 없는 clean 원본**(output/scenes/clean/scene_XX.png)으로 만든다.
  WanGP 는 입력 이미지를 다시 그리기 때문에, 말풍선을 넣은 이미지로 영상을 만들면
  글자가 뭉개진다. 말풍선은 10단계 조립에서 영상 위에 따로 얹어 선명하게 유지한다.
- torch/PIL 없이 표준 라이브러리만 사용한다(웹 서버에서 바로 import).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

MOTION_PROMPT = (
    "The cute chubby 2D webtoon raccoon mascot blinks, smiles, and gestures naturally, "
    "subtle camera push-in, smooth motion, no text, no speech bubble"
)


FRESH_TOLERANCE_SEC = 300.0   # 같은 회차(5분 이내)에서 만든 파일로 본다


def clean_image_for(scene: dict) -> str:
    """장면의 말풍선 없는 원본 경로. 없으면(또는 옛 회차면) 장면 이미지 그대로.

    clean/ 은 이미지를 만들 때마다 새로 써지므로, 현재 장면 이미지와 시간이
    비슷할 때만 쓴다. 옛 clean 원본을 쓰면 **이전 이미지**로 영상이 만들어진다.
    """
    image = str(scene.get("image") or "").strip()
    if not image:
        return ""
    p = Path(image)
    clean = p.parent / "clean" / p.name
    try:
        # clean/ 은 이미지보다 더 새것이어도 된다(복사한 경우). 옛 회차(오래된 원본)만 걸러낸다.
        if clean.exists() and clean.stat().st_mtime + FRESH_TOLERANCE_SEC >= p.stat().st_mtime:
            return str(clean)
    except Exception:
        pass
    return str(p)


def default_indices(scenes: list[dict], cfg: dict) -> list[int]:
    """기본 선택: 훅(첫 장면) + 중간. config.scenes.motion_count 개까지."""
    sc = cfg.get("scenes", {}) or {}
    count = int(sc.get("motion_count", 2) or 0)
    if count <= 0 or not scenes:
        return []
    n = len(scenes)
    wanted = [1]
    if n > 3:
        wanted.append(max(2, n // 2 + 1))
    if n > 7 and count >= 3:
        wanted.append(n - 1)
    valid = {int(s.get("index") or i + 1) for i, s in enumerate(scenes)}
    picked = sorted({i for i in wanted if i in valid})[:count]
    return picked


def build_jobs(scenes: list[dict], cfg: dict, indices: list[int] | None = None) -> list[dict]:
    """선택한 장면들로 WanGP 작업 목록을 만든다."""
    sc = cfg.get("scenes", {}) or {}
    if not bool(sc.get("motion_enabled", True)):
        return []
    if not scenes:
        return []
    wp = cfg.get("wangp", {}) or {}
    if indices is None:
        indices = default_indices(scenes, cfg)

    by_index = {int(s.get("index") or 0): s for s in scenes}
    jobs: list[dict] = []
    for i in sorted({int(x) for x in indices or [] if str(x).strip() != ""}):
        scene = by_index.get(i)
        if not scene:
            continue
        image = clean_image_for(scene)
        if not image:
            continue
        jobs.append({
            "scene_index": i,
            "prompt": MOTION_PROMPT,
            "image": image,                     # 말풍선 없는 clean 원본
            "scene_image": str(scene.get("image") or ""),   # 참고용(말풍선 포함)
            "bubble_text": str(scene.get("bubble_text") or ""),
            "negative_prompt": sc.get("negative_prompt", ""),
            "resolution": str(sc.get("motion_resolution") or wp.get("resolution") or "480x832"),
            "video_length": int(sc.get("motion_frames") or wp.get("video_length") or 81),
            "num_inference_steps": int(wp.get("num_inference_steps") or 4),
        })
    return jobs


def frames_for(scene: dict, cfg: dict) -> int:
    return int((cfg.get("scenes", {}) or {}).get("motion_frames") or
               (cfg.get("wangp", {}) or {}).get("video_length") or 81)


def load_plan(jobs_path: Path) -> list[dict]:
    try:
        data = json.loads(Path(jobs_path).read_text(encoding="utf-8"))
    except Exception:
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return [j for j in (jobs or []) if isinstance(j, dict)]


def planned_indices(jobs_path: Path) -> list[int]:
    out = []
    for job in load_plan(jobs_path):
        try:
            out.append(int(job.get("scene_index") or 0))
        except Exception:
            continue
    return sorted({i for i in out if i})


def write_plan(scenes_json: Path, jobs_path: Path, cfg: dict,
               indices: list[int] | None = None) -> dict:
    """작업 목록 파일 + scenes.json 의 motion 플래그를 함께 갱신한다.

    - 선택된 장면: motion=True
    - 빠진 장면: motion=False → 조립은 정지 이미지+줌/팬을 쓴다.
    - 이미 만든 클립(motion_clip)은 지우지 않는다. 다시 선택하면 재생성 없이 그대로 쓴다.
    """
    scenes_json = Path(scenes_json)
    jobs_path = Path(jobs_path)
    data = json.loads(scenes_json.read_text(encoding="utf-8"))
    scenes = data.get("scenes") or []
    jobs = build_jobs(scenes, cfg, indices)

    chosen = {int(j["scene_index"]) for j in jobs}
    job_by_index = {int(j["scene_index"]): j for j in jobs}
    for scene in scenes:
        idx = int(scene.get("index") or 0)
        scene["motion"] = idx in chosen
        scene["motion_image"] = (job_by_index.get(idx) or {}).get("image", "")
    scenes_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(jobs),
        "frames": frames_for(scenes[0] if scenes else {}, cfg),
        "note": "image 는 말풍선 없는 clean 원본입니다. 말풍선은 10단계 조립에서 영상 위에 얹습니다.",
        "jobs": jobs,
    }
    jobs_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"count": len(jobs), "indices": sorted(chosen), "jobs": jobs}
