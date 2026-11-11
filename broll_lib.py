"""
[공용] B-Roll 클립 라이브러리
==============================
장면마다 다른 B-Roll 을 고를 수 있게, 녹화/업로드한 클립을 한 폴더에 모아 관리한다.

- 저장 위치: output/broll_clips/*.mp4
- 색인:      output/broll_clips/index.json  (라벨·길이·출처·생성시각)
- 6단계 녹화/업로드(scripts_capture_broll.py)와 웹의 '창 녹화' 버튼이 register() 로 등록하고,
  조립(scripts_assemble_shorts.py)과 웹 UI 가 list_clips() 로 읽는다.

ffprobe 외에는 표준 라이브러리만 쓴다(웹 서버에서 바로 import).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT / "output"
CLIPS_DIR = OUTPUT_DIR / "broll_clips"
INDEX_FILE = CLIPS_DIR / "index.json"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def probe_duration(path: str | Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return round(float((out.stdout or "0").strip() or 0), 2)
    except Exception:
        return 0.0


def _read_index() -> dict:
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"clips": []}


def _write_index(data: dict) -> None:
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_clips() -> list[dict]:
    """저장된 클립 목록(최신 순). 폴더의 mp4 와 색인을 맞춘다."""
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    data = _read_index()
    known = {str(Path(c.get("path") or "")): c for c in data.get("clips") or [] if c.get("path")}

    clips: list[dict] = []
    changed = False
    for path in sorted(CLIPS_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        item = known.get(str(path)) or {}
        stat = path.stat()
        if not item or item.get("size") != stat.st_size:
            item = {**item,
                    "path": str(path),
                    "name": path.name,
                    "size": stat.st_size,
                    "duration_sec": item.get("duration_sec") or probe_duration(path),
                    "created_at": item.get("created_at") or
                    datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")}
            changed = True
        item.setdefault("label", path.stem)
        item.setdefault("source", "file")
        clips.append(item)
    if changed:
        _write_index({"clips": clips})
    return clips


def get_clip(path: str | Path) -> dict | None:
    target = str(Path(path))
    for clip in list_clips():
        if str(clip.get("path")) == target:
            return clip
    return None


def register(path: str | Path, *, label: str = "", source: str = "file",
             copy_into_library: bool = True) -> dict | None:
    """클립을 라이브러리에 등록한다. 외부 파일이면 라이브러리로 복사해 경로가 바뀌지 않게 한다."""
    src = Path(path)
    if not src.exists() or not src.is_file() or src.stat().st_size < 2000:
        return None
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)

    if copy_into_library and src.parent.resolve() != CLIPS_DIR.resolve():
        suffix = src.suffix.lower() or ".mp4"
        base = f"clip_{_stamp()}_{src.stem[:24]}"
        dest = CLIPS_DIR / f"{base}{suffix}"
        n = 2
        while dest.exists():
            dest = CLIPS_DIR / f"{base}_{n}{suffix}"
            n += 1
        try:
            shutil.copy2(src, dest)
            src = dest
        except Exception:
            pass

    data = _read_index()
    clips = [c for c in data.get("clips") or [] if str(c.get("path")) != str(src)]
    item = {
        "path": str(src),
        "name": src.name,
        "label": (label or src.stem).strip(),
        "source": source,
        "size": src.stat().st_size,
        "duration_sec": probe_duration(src),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    clips.insert(0, item)
    _write_index({"clips": clips})
    return item


def label_for(path: str | Path) -> str:
    clip = get_clip(path)
    return str((clip or {}).get("label") or Path(path).stem)
