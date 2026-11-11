r"""
[단계 9] pycapcut 기반 CapCut 초안(draft) 생성
================================================
미리보기 MP4(10단계)와 **같은 장면 프레임**으로 초안을 만든다.
  - 장면마다 이미 완성된 프레임(output/scenes/segments/seg_XX_*.mp4)을 한 트랙에 순서대로 넣는다.
  - 그래서 "CapCut 초안은 B-Roll 전체 동일 / MP4 는 장면마다" 처럼 서로 어긋나지 않는다.
  - 장면 프레임을 만드는 규칙(상단 B-Roll 칸 비율, 장면 맞춤 설정)은 10단계와 공용이다.

입력
- output/voice.mp3                 (4단계 결과, 길이 계산에 사용)
- output/scenes/scenes.json        (5단계 결과 — 있으면 장면별 프레임 사용)
- output/subtitles.json            (8단계 결과, 텍스트 트랙은 기본 미생성)
- config.assembly / config.scenes  (B-Roll 사용 여부·비율·맞춤 설정)

출력
- CapCut 드래프트: %LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft\<이름>_<타임스탬프>\
- output/capcut_result.json        (경로/트랙/장면 요약 — 웹 표시용)
- output/capcut_export/<이름>/      (draft 파일 사본 — 바로 확인/다운로드용)

  python scripts_build_capcut_draft.py
  python scripts_build_capcut_draft.py --broll "C:\...\broll_x.mp4" --no-broll
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
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


def expand(path: str) -> str:
    return os.path.normpath(os.path.expandvars(path))


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def probe_duration(path: str | Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float((out.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def latest_file(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    files = [p for p in directory.glob(pattern) if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def ensure_background(config: dict) -> tuple[str, str]:
    """(예전 방식) 배경 이미지 경로. 없으면 ffmpeg로 단색 배경을 만든다."""
    assets_dir = Path(expand((config.get("paths", {}) or {}).get("assets_dir", str(PROJECT / "assets"))))
    host = assets_dir / "raccoon" / "host.png"
    if host.exists():
        return str(host), "assets/raccoon/host.png"

    img_dir = Path(expand((config.get("paths", {}) or {}).get("generated_images", str(OUTPUT_DIR / "generated_images"))))
    latest = latest_file(img_dir, "*.png")
    if latest:
        return str(latest), f"generated_images/{latest.name}"

    fallback = OUTPUT_DIR / "capcut_assets" / "background.png"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    width = int((config.get("ffmpeg", {}) or {}).get("shorts_width", 1080))
    height = int((config.get("ffmpeg", {}) or {}).get("shorts_height", 1920))
    print("[INFO] 배경 이미지가 없어 단색 배경을 생성합니다.")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0x101418:s={width}x{height}",
         "-frames:v", "1", str(fallback)],
        capture_output=True, text=True, timeout=60,
    )
    return (str(fallback), "generated:fallback") if fallback.exists() else ("", "")


def load_subtitles() -> list[dict]:
    path = OUTPUT_DIR / "subtitles.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("subtitles") or []
    except Exception:
        return []


def scene_segments(config: dict, width: int, height: int, fps: int, use_broll: bool) -> list[dict]:
    """10단계 조립과 같은 장면 프레임을 준비한다(같은 캐시를 쓰므로 두 번 렌더하지 않는다)."""
    try:
        import scripts_assemble_shorts as assemble   # 같은 프로젝트 모듈
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 장면 조립 모듈을 불러오지 못했습니다: {type(e).__name__}: {e}")
        return []
    asm = config.get("assembly", {}) or {}
    top_ratio = float(asm.get("top_ratio", 0.5))
    try:
        return assemble.render_scene_segments(
            config, width, height, top_ratio, fps,
            per_scene_broll=use_broll, full=not use_broll,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 장면 프레임 준비 실패 → 예전 단일 배경 방식으로 만듭니다: {type(e).__name__}: {e}")
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description="CapCut 초안 생성 (pycapcut)")
    ap.add_argument("--broll", default=None, help="(예전 방식) 사용할 B-Roll 파일")
    ap.add_argument("--no-broll", action="store_true", help="B-Roll 없이 장면 전체 화면으로 초안 만들기")
    ap.add_argument("--name", default=None, help="드래프트 이름")
    ap.add_argument("--draft-root", default=None, help="드래프트를 만들 폴더(기본: CapCut 드래프트 폴더)")
    ap.add_argument("--with-text", dest="with_text", action="store_true", default=None,
                    help="Whisper 텍스트를 CapCut 텍스트 트랙으로 넣기 (기본: config.capcut.include_text_track=false)")
    ap.add_argument("--no-text", dest="with_text", action="store_false",
                    help="텍스트 트랙을 만들지 않기 (권장)")
    args = ap.parse_args()

    try:
        import pycapcut as pc
    except ImportError:
        print("[ERROR] pycapcut 미설치: pip install pycapcut==0.0.4")
        return 1

    config = load_config()
    voice_path = OUTPUT_DIR / "voice.mp3"
    if not voice_path.exists():
        print(f"[ERROR] 음성 파일이 없습니다: {voice_path}")
        return 1

    duration = probe_duration(voice_path)
    if duration < 0.5:
        print(f"[ERROR] 음성 길이가 비정상입니다({duration:.2f}초). TTS를 다시 실행하세요.")
        return 1

    include_text = bool((config.get("capcut", {}) or {}).get("include_text_track", False))
    if args.with_text is not None:
        include_text = bool(args.with_text)

    subtitles = load_subtitles()
    if include_text and not subtitles:
        print("[ERROR] 텍스트 트랙을 켰는데 output/subtitles.json 이 없거나 비어 있습니다. 8단계(타이밍)를 먼저 완료하세요.")
        return 1
    if not include_text:
        print("[INFO] 텍스트 트랙 생략: Whisper tiny 받아쓰기는 정확도가 낮아 영상에 넣지 않습니다.")
        print("       (필요하면 --with-text 또는 config.capcut.include_text_track=true)")

    width = int((config.get("ffmpeg", {}) or {}).get("shorts_width", 1080))
    height = int((config.get("ffmpeg", {}) or {}).get("shorts_height", 1920))
    fps = int((config.get("ffmpeg", {}) or {}).get("shorts_fps", 30))
    asm = config.get("assembly", {}) or {}
    top_ratio = float(asm.get("top_ratio", 0.5))
    layout = str(asm.get("layout") or "split")
    broll_on = bool((config.get("broll", {}) or {}).get("enabled", True))
    use_broll = broll_on and layout != "scenes" and not args.no_broll

    draft_name = args.name or (config.get("paths", {}) or {}).get("capcut_draft_name", "너구리쇼츠_초안")
    draft_root = Path(expand(args.draft_root or (config.get("paths", {}) or {}).get(
        "capcut_draft_root", r"%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft")))
    capcut_missing = not draft_root.exists()
    draft_root.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{draft_name}_{stamp}"

    # ---- 1) 장면 프레임 준비 (10단계와 동일한 규칙/캐시) ----
    segments = scene_segments(config, width, height, fps, use_broll)
    if segments:
        print(f"[CapCut] 미리보기 MP4 와 같은 장면 프레임 {len(segments)}개를 사용합니다 "
              f"(상단 비율 {int(round(top_ratio * 100))}%, 맞춤 {asm.get('scene_fit') or 'cover'})")
    else:
        print("[CapCut] 장면 프레임이 없어 예전 방식(배경 + B-Roll 오버레이)으로 만듭니다.")

    print(f"[CapCut] 드래프트 생성: {name} ({width}x{height}, {fps}fps, {duration:.1f}초)")
    folder = pc.DraftFolder(str(draft_root))
    script = folder.create_draft(name, width, height, fps=fps, allow_replace=True)

    broll = None          # 예전 방식에서만 사용
    broll_span = 0.0
    background, bg_source = "", ""

    if segments:
        # ---- 2) 장면 트랙: 완성 프레임을 순서대로 (10단계 MP4 와 같은 화면) ----
        script.add_track(pc.TrackType.video, track_name="장면")
        t = 0.0
        placed = 0
        for seg in segments:
            d = max(0.2, float(seg.get("duration") or 0))
            # 실제 파일 길이보다 길게 잡으면 pycapcut 이 거부한다 → 살짝 짧게 자른다
            actual = max(0.2, probe_duration(seg["file"]) - 0.03)
            end = min(t + d, duration, t + actual)
            if end - t < 0.12:
                continue
            script.add_segment(
                pc.VideoSegment(pc.VideoMaterial(seg["file"]),
                                pc.trange_seconds(round(t, 3), end=round(end, 3))),
                track_name="장면",
            )
            t = end
            placed += 1
        print(f"[CapCut] 장면 클립 {placed}개 배치 (0 ~ {t:.1f}초)")
    else:
        # ---- 예전 방식: 배경 1장 + B-Roll 오버레이 ----
        background, bg_source = ensure_background(config)
        if not background:
            print("[ERROR] 배경 이미지를 준비하지 못했습니다.")
            return 1
        if use_broll:
            candidate = Path(expand(args.broll)) if args.broll else None
            if candidate is None:
                broll_dir = Path(expand((config.get("paths", {}) or {}).get("broll_dir", r"%USERPROFILE%\릴스자료")))
                candidate = latest_file(broll_dir, "broll_*.mp4")
            if candidate and candidate.exists():
                broll = str(candidate)
                broll_span = min(probe_duration(broll) or duration, duration)
            else:
                print("[INFO] B-Roll 파일 없음 → 배경+음성만으로 초안을 만듭니다.")

        script.add_track(pc.TrackType.video, track_name="Main Track")
        script.add_segment(
            pc.VideoSegment(pc.VideoMaterial(background), pc.trange_seconds(0, end=duration)),
            track_name="Main Track",
        )
        if broll:
            script.add_track(pc.TrackType.video, track_name="B-Roll Track")
            script.add_segment(
                pc.VideoSegment(
                    pc.VideoMaterial(broll),
                    pc.trange_seconds(0, end=broll_span),
                    clip_settings=pc.ClipSettings(transform_y=0.55, scale_x=1.0, scale_y=1.0),
                ),
                track_name="B-Roll Track",
            )

    # ---- 3) 음성 트랙 ----
    script.add_track(pc.TrackType.audio, track_name="Audio Track 1")
    script.add_segment(
        pc.AudioSegment(str(voice_path), pc.trange_seconds(0, end=duration), volume=1.0),
        track_name="Audio Track 1",
    )

    # ---- 4) 효과음 트랙 (에셋이 있을 때만) ----
    sfx_dir = Path(expand((config.get("paths", {}) or {}).get("assets_dir", str(PROJECT / "assets")))) / "sfx"
    sfx_used = ""
    for sfx_name in ("pop.mp3", "whoosh.mp3"):
        sfx_file = sfx_dir / sfx_name
        if sfx_file.exists() and duration > 3.5:
            script.add_track(pc.TrackType.audio, track_name="Audio Track 2")
            script.add_segment(
                pc.AudioSegment(str(sfx_file), pc.trange_seconds(3.0, end=4.0), volume=0.5),
                track_name="Audio Track 2",
            )
            sfx_used = str(sfx_file)
            break
    if not sfx_used:
        print("[INFO] assets/sfx 에 pop.mp3/whoosh.mp3 가 없어 효과음 트랙은 생략합니다.")

    # ---- 5) 텍스트 트랙 (기본 생략 — 필요할 때만) ----
    text_count = 0
    timing_blocks = len(subtitles)
    if include_text and subtitles:
        script.add_track(pc.TrackType.text, track_name="Text Track")
        style = pc.TextStyle(size=9.0, bold=True, color=(1.0, 1.0, 1.0))
        border = pc.TextBorder(color=(0.0, 0.0, 0.0), width=35.0)
        clip = pc.ClipSettings(transform_y=-0.55)
        prev_end = 0.0  # pycapcut 은 같은 트랙의 겹침을 거부한다 → 자막 구간을 눌러서 배치
        for block in subtitles:
            start = round(max(float(block.get("start", 0)), prev_end), 2)
            end = round(min(float(block.get("end", start + 0.5)), duration), 2)
            if end - start < 0.15 or start >= duration:
                continue
            script.add_segment(
                pc.TextSegment(
                    block.get("text", ""),
                    pc.trange_seconds(start, end=end),
                    style=style, border=border, clip_settings=clip,
                ),
                track_name="Text Track",
            )
            prev_end = end
            text_count += 1

    script.save()

    draft_dir = draft_root / name
    files = sorted(p.name for p in draft_dir.iterdir()) if draft_dir.exists() else []
    if not files:
        print(f"[ERROR] 드래프트 파일이 생성되지 않았습니다: {draft_dir}")
        return 1

    # 사본 (프로젝트 안에서 확인/다운로드 가능하게)
    export_dir = OUTPUT_DIR / "capcut_export" / name
    export_dir.mkdir(parents=True, exist_ok=True)
    for f in draft_dir.iterdir():
        if f.is_file():
            shutil.copy2(f, export_dir / f.name)

    scene_rows = [{
        "index": s.get("index"),
        "duration": s.get("duration"),
        "broll": Path(expand(s["broll"])).name if s.get("broll") else "",
        "top_h": s.get("top_h"),
        "bottom_h": s.get("bottom_h"),
        "file": s.get("file"),
    } for s in segments]

    result = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "draft_name": name,
        "draft_dir": str(draft_dir),
        "export_dir": str(export_dir),
        "capcut_root_missing": capcut_missing,
        "resolution": {"width": width, "height": height, "fps": fps},
        "duration_sec": round(duration, 2),
        "composition": {
            "mode": "segments" if segments else "legacy",
            "same_as_preview": bool(segments),
            "note": ("10단계 미리보기 MP4 와 같은 장면 프레임을 씁니다." if segments
                     else "장면 프레임 없이 배경 1장 + B-Roll 오버레이로 만든 예전 방식입니다."),
            "layout": layout,
            "fit": asm.get("scene_fit") or "cover",
            "anchor": asm.get("scene_anchor") or "top",
            "top_ratio": top_ratio,
            "top_px": round(height * top_ratio),
            "bottom_px": height - round(height * top_ratio),
            "per_scene_broll": use_broll,
            "segments_dir": str(OUTPUT_DIR / "scenes" / "segments"),
            "scenes": scene_rows,
        },
        "audio": str(voice_path),
        "sfx": sfx_used,
        "include_text_track": include_text,
        "timing_blocks": timing_blocks,
        "tracks": (
            [{"name": "장면", "type": "video", "clips": len(scene_rows)}]
            if segments else
            [{"name": "Main Track", "type": "video", "clips": 1},
             {"name": "B-Roll Track", "type": "video", "clips": 1 if broll else 0}]
        ) + [
            {"name": "Audio Track 1", "type": "audio", "clips": 1},
            {"name": "Audio Track 2", "type": "audio", "clips": 1 if sfx_used else 0},
        ] + ([{"name": "Text Track", "type": "text", "clips": text_count}] if include_text else []),
        "files": files,
        "warnings": (["CapCut 드래프트 폴더가 없어서 새로 만들었습니다. CapCut 설치/버전을 확인하세요."] if capcut_missing else []),
    }
    if not segments:
        result["background"] = {"path": background, "source": bg_source}
        result["broll"] = {"path": broll or "", "duration_sec": round(broll_span, 2)}
    (OUTPUT_DIR / "capcut_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("")
    print("=" * 60)
    print(f"[OK] CapCut 초안: {draft_dir}")
    print(f"     파일: {', '.join(files)}")
    if segments:
        print(f"     트랙: 장면 {len(scene_rows)} / 음성 1 / 효과음 {'1' if sfx_used else '0'} "
              f"/ 텍스트 {text_count if include_text else '생략'}")
        print(f"     배치: 10단계 MP4 와 동일 (상단 {round(height * top_ratio)}px · 하단 {height - round(height * top_ratio)}px, "
              f"맞춤 {asm.get('scene_fit') or 'cover'})")
    else:
        print(f"     트랙: 배경 1 / B-Roll {'1' if broll else '0'} / 음성 1 / 효과음 {'1' if sfx_used else '0'} "
              f"/ 텍스트 {text_count if include_text else '생략'}")
    if not include_text:
        print(f"     (Whisper 타이밍 블록 {timing_blocks}개는 output/subtitles.json 에만 보관 — 영상에 글자 없음)")
    print(f"     사본: {export_dir}")
    if capcut_missing:
        print("[WARN] CapCut 드래프트 폴더가 원래 없었습니다 → CapCut에서 목록이 안 보일 수 있습니다.")
    print("     CapCut 앱을 열어 드래프트를 확인하고 내보내기 하세요.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
