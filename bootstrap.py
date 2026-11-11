"""
bootstrap.py - 설치 폴더 기준으로 경로를 정리하고 작업 폴더를 만든다.
=====================================================================
install.bat / run.bat 이 자동으로 호출합니다. 직접 실행해도 됩니다.

  venv\\Scripts\\python.exe bootstrap.py

하는 일
- config.json 의 상대 경로를 '이 파일이 있는 폴더' 기준 절대 경로로 바꾼다.
  (폴더를 D:\\, E:\\, 바탕화면 어디에 풀어도 그대로 동작하게 만들기 위함)
- output / assets / scripts/logs / scripts/.secrets 같은 작업 폴더를 만든다.
- GPU 선택 기능(scenes / wangp) 경로는 비어 있으면 건드리지 않는다.
- 바뀐 내용이 없으면 config.json 을 다시 쓰지 않는다.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT / "config.json"

# 프로젝트 폴더 안에 있어야 하는 경로 (상대 경로 → 절대 경로)
PROJECT_PATH_KEYS = (
    "project_root",
    "venv",
    "whisper_model",
    "output_dir",
    "assets_dir",
    "trending_cache",
    "generated_images",
    "generated_videos",
    "final_output",
)
# 프로젝트 밖(사용자 폴더 / CapCut / B-Roll) 경로: 환경변수만 풀어서 사용
EXTERNAL_PATH_KEYS = ("broll_dir", "capcut_draft_root")
# 중첩된 경로 (섹션, 키)
NESTED_PATH_KEYS = (
    ("youtube", "client_secret_path"),
    ("youtube", "token_path"),
    ("assembly", "final_output"),
    ("image_generation", "output_dir"),
    ("video_generation", "output_dir"),
    ("scenes", "model_path"),
    ("wangp", "root"),
    ("wangp", "python"),
    ("scenes", "python"),
)


def to_abs(value: str, *, keep_external: bool = False) -> str:
    """상대 경로를 설치 폴더 기준 절대 경로로 바꾼다. 빈 값은 그대로 둔다."""
    raw = (value or "").strip()
    if not raw:
        return ""
    expanded = os.path.expandvars(raw)
    path = Path(expanded)
    if path.is_absolute():
        return str(path)
    if keep_external and expanded != raw:
        # %USERPROFILE% 처럼 환경변수로 시작한 경로는 이미 절대 경로가 된다.
        return str(Path(os.path.abspath(expanded)))
    resolved = (PROJECT / path).resolve()
    if keep_external:
        return str(resolved)
    return str(resolved)


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def write_config(cfg: dict) -> None:
    text = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, CONFIG_FILE)
    except OSError:
        CONFIG_FILE.write_text(text, encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass


def ensure_dirs(cfg: dict) -> list[Path]:
    paths = cfg.get("paths", {}) or {}
    wanted = [
        paths.get("output_dir", "output"),
        paths.get("trending_cache", "output/trending"),
        paths.get("generated_images", "output/generated_images"),
        paths.get("generated_videos", "output/generated_videos"),
        paths.get("final_output", "output/final"),
        str(PROJECT / "output" / "scenes"),
        str(PROJECT / "output" / "scenes" / "clean"),
        str(PROJECT / "output" / "scenes" / "bubbles"),
        str(PROJECT / "output" / "scenes" / "segments"),
        str(PROJECT / "output" / "broll_clips"),
        str(PROJECT / "output" / "broll_uploads"),
        str(PROJECT / "output" / "capcut_export"),
        str(PROJECT / "output" / "layout_preview"),
        paths.get("assets_dir", "assets"),
        str(PROJECT / "assets" / "raccoon"),
        str(PROJECT / "assets" / "sfx"),
        str(PROJECT / "models" / "whisper"),
        str(PROJECT / "scripts" / "logs"),
        str(PROJECT / "scripts" / ".secrets" / "youtube"),
        paths.get("capcut_draft_root", ""),
        paths.get("broll_dir", ""),
    ]
    made: list[Path] = []
    for item in wanted:
        if not item:
            continue
        try:
            target = Path(os.path.expandvars(str(item)))
            target.mkdir(parents=True, exist_ok=True)
            made.append(target)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] 폴더를 만들지 못했습니다: {item} ({exc})")
    return made


def main() -> int:
    if not CONFIG_FILE.exists():
        print(f"[ERROR] config.json 이 없습니다: {CONFIG_FILE}")
        return 1

    cfg = load_config()
    before = json.dumps(cfg, ensure_ascii=False, sort_keys=True)

    paths = cfg.setdefault("paths", {})
    for key in PROJECT_PATH_KEYS:
        if key in paths:
            paths[key] = to_abs(paths[key])
    for key in EXTERNAL_PATH_KEYS:
        if key in paths:
            paths[key] = to_abs(paths[key], keep_external=True)
    for section, key in NESTED_PATH_KEYS:
        node = cfg.get(section)
        if isinstance(node, dict) and key in node:
            node[key] = to_abs(node[key], keep_external=(section in {"wangp", "scenes"}))

    after = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
    if before != after:
        write_config(cfg)
        print(f"[OK] config.json 경로를 이 폴더 기준으로 갱신했습니다: {PROJECT}")
    else:
        print(f"[OK] config.json 경로 확인 완료: {PROJECT}")

    made = ensure_dirs(cfg)
    print(f"[OK] 작업 폴더 {len(made)}개 준비")
    venv = (paths.get("venv") or "").strip()
    if venv:
        print(f"[OK] 가상환경: {venv}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
