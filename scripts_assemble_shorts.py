"""
[단계 10] FFmpeg 쇼츠 조립 (9:16 MP4)
=====================================
  python scripts_assemble_shorts.py                  # 기본: 장면마다 [상단 B-Roll + 하단 너구리]
  python scripts_assemble_shorts.py --layout scenes   # B-Roll 없이 장면 이미지/영상 전체 화면(9:16)
  python scripts_assemble_shorts.py --layout full     # 예전처럼 배경 전체 + 상단 B-Roll
  python scripts_assemble_shorts.py --top D:\\clip.mp4 --no-broll

입력
- output/voice.mp3                    (길이 기준)
- 상단(top): output/generated_videos/wangp_*.mp4 → 없으면 B-Roll(output/broll_meta.json / 최신 broll_*.mp4)
- 하단(bottom): config.assembly.bottom_source (auto|wangp|broll|assets|경로)
                 WanGP 결과 → assets/raccoon/anim_*.mp4 → assets/raccoon/host.png(느린 줌) 순서
- output/subtitles.srt                (config.assembly.include_subtitles=true 일 때만 번인)

레이아웃
- split : 장면마다 [상단 B-Roll 칸 + 하단 장면 칸] 을 미리 합성해 이어 붙인다(기본).
          B-Roll 은 칸을 꽉 채우고 가운데를 보여준다.
- scenes: B-Roll 을 아예 쓰지 않는다. 장면 이미지(줌/팬)·WanGP 모션을 1080x1920 전체 화면으로 깐다.
          화면 안내는 '장면 말풍선'(bubble_overlay.py)이 담당한다.
- full  : 배경 이미지 위에 B-Roll 을 얹는 예전 방식.

장면 맞춤(config.assembly.scene_fit / scene_anchor)
- cover+top    : 칸을 꽉 채우고 위쪽(머리) 기준 → 아래(다리)만 잘린다. 기본값.
- cover+center : 꽉 채우되 가운데 기준.
- contain      : 그림 전체를 넣고 남는 여백을 같은 그림의 흐린 배경으로 채운다.
   * 예전에는 세로 그림(1080x1920)을 가로 칸(1080x960)에 '가운데 크롭'해서 장면마다
     머리가 잘리기도 했다. 지금은 장면마다 같은 규칙으로 맞춘다.

세그먼트 공유
- 장면 프레임은 output/scenes/segments/ 에 캐시되고 manifest.json 에 기록된다.
  CapCut 초안(9단계)도 이 파일을 그대로 쓴다 → 미리보기 MP4 와 배치가 같다.

자막 정책
- Whisper tiny 는 '발화 위치(타이밍)' 확인용으로만 쓴다. tiny 받아쓰기는 정확도가 낮아
  영상에 글자로 굽지 않는다(기본 include_subtitles=false). 쇼츠/릴스 앱의 자동 자막을 쓴다.

원칙
- 정지 이미지는 반드시 loop 시켜 음성 길이만큼 인코딩한다(1프레임 영상 방지).
- 결과 길이가 음성보다 짧으면 실패로 처리한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT / "config.json"
OUTPUT_DIR = PROJECT / "output"

sys.path.insert(0, str(PROJECT))
try:
    import bubble_overlay          # 말풍선 투명 레이어(PIL)
except Exception:                  # noqa: BLE001  (없으면 말풍선 없이 진행)
    bubble_overlay = None
try:
    import broll_lib               # 장면별 B-Roll 클립 목록
except Exception:                  # noqa: BLE001
    broll_lib = None

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


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


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


def is_video(path: Path) -> bool:
    return path.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv", ".avi")


def latest_file(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    files = [p for p in directory.glob(pattern) if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def probe_size(path: str | Path) -> tuple[int, int]:
    """영상/이미지의 원본 크기(px). 실패하면 (0, 0)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        nums = [int(x) for x in (out.stdout or "").split() if x.strip().isdigit()]
        if len(nums) >= 2 and nums[0] > 0 and nums[1] > 0:
            return nums[0], nums[1]
    except Exception:
        pass
    return 0, 0


# ----------------------------------------------------------------------
# 화면 배치 (10단계 MP4 · 9단계 CapCut 초안이 같은 값을 쓴다)
# ----------------------------------------------------------------------
def band_heights(height: int, top_ratio: float, with_broll: bool) -> tuple[int, int]:
    """세로 캔버스를 [상단 B-Roll 칸, 하단 장면 칸] 으로 나눈다."""
    if not with_broll:
        return 0, height // 2 * 2
    top_h = int(round(height * max(0.3, min(0.75, float(top_ratio))))) // 2 * 2
    return top_h, height - top_h


def scene_fit_chains(inp: str, out: str, width: int, height: int, fps: int,
                     fit: str = "cover", anchor: str = "top", tag: str = "s") -> str:
    """장면 이미지/영상을 width×height 칸에 맞춘다.

    cover   : 칸을 꽉 채우고 남는 쪽만 자른다(anchor=top 이면 위쪽=머리 기준, 아래=다리만 잘림)
    contain : 전체가 보이게 넣고 남는 여백은 같은 그림의 흐린 배경으로 채운다
    """
    if str(fit).lower().startswith("contain"):
        # 배경: 칸을 채우게 늘린 뒤 블러, 앞: 전체가 보이게 축소 → 좌우(또는 위아래) 여백은 블러
        return (
            f"{inp}split=2[{tag}bg][{tag}fg];"
            f"[{tag}bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:(iw-ow)/2:(ih-oh)/2,"
            f"boxblur=luma_radius=28:luma_power=2,eq=brightness=-0.06:saturation=0.9[{tag}b];"
            f"[{tag}fg]scale={width}:{height}:force_original_aspect_ratio=decrease[{tag}f];"
            f"[{tag}b][{tag}f]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps}{out}"
        )
    y = "0" if str(anchor).lower() != "center" else "(ih-oh)/2"
    return (
        f"{inp}scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}:(iw-ow)/2:{y},setsar=1,fps={fps}{out}"
    )


def _zoom_chains(inp: str, out: str, width: int, height: int, fps: int, idx: int) -> str:
    """칸과 그림의 비율이 같을 때(자를 것이 없을 때) 느린 줌/팬."""
    zw, zh = width * 2, height * 2
    # 3장면마다 줌인 → 줌아웃으로 바꿔 단조롭지 않게
    if idx % 3 == 2:
        zexpr = "max(1.06-0.00045*on,1.0)"
    else:
        zexpr = "min(1.0+0.00045*on,1.06)"
    return (
        f"{inp}scale={zw}:{zh}:force_original_aspect_ratio=increase,"
        f"crop={zw}:{zh}:(iw-ow)/2:(ih-oh)/2,"
        f"zoompan=z='{zexpr}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)*0.30'"
        f":d=1:s={width}x{height}:fps={fps}{out}"
    )


def subs_filter(srt: str | None, style: str = "") -> str:
    """자막 필터 문자열. style 은 libass force_style (예: MarginV=28,Outline=2)."""
    if not srt:
        return ""
    base = f"subtitles='{escape_filter_path(srt)}'"
    if style:
        base += f":force_style='{style}'"
    return f",{base}"


def escape_filter_path(path: str) -> str:
    # FFmpeg filtergraph 문자열 안에서 경로에 쓰이는 특수문자 이스케이프
    p = path.replace("\\", "/")
    for ch in (":", "'", "[", "]", ","):
        p = p.replace(ch, "\\" + ch)
    return p


# ----------------------------------------------------------------------
# 소스 선택
# ----------------------------------------------------------------------
def latest_wangp(config: dict) -> Path | None:
    gen = Path(expand((config.get("paths", {}) or {}).get("generated_videos",
                                                         str(OUTPUT_DIR / "generated_videos"))))
    wan = latest_file(gen, "wangp_*.mp4")
    return wan if wan and probe_duration(wan) > 0.4 else None


def latest_broll(config: dict) -> Path | None:
    meta = read_json(OUTPUT_DIR / "broll_meta.json")
    if meta.get("output"):
        p = Path(expand(meta["output"]))
        if p.exists() and probe_duration(p) > 0.4:
            return p
    broll_dir = Path(expand((config.get("paths", {}) or {}).get("broll_dir", r"%USERPROFILE%\릴스자료")))
    cand = latest_file(broll_dir, "broll_*.mp4")
    return cand if cand and probe_duration(cand) > 0.4 else None


def broll_enabled(config: dict) -> bool:
    return bool((config.get("broll", {}) or {}).get("enabled", True))


def pick_top(config: dict, override: str | None) -> Path | None:
    if override:
        p = Path(expand(override))
        return p if p.exists() else None

    mode = ((config.get("assembly", {}) or {}).get("top_source") or "auto").strip().lower()
    if mode not in ("auto", "wangp", "broll", "none"):
        p = Path(expand(mode))            # 직접 경로
        return p if p.exists() else None
    if mode == "none":
        return None

    # 선호 소스 → 없으면 다른 소스로 폴백
    order = {"auto": ["broll", "wangp"], "broll": ["broll", "wangp"], "wangp": ["wangp", "broll"]}[mode]
    if not broll_enabled(config):
        order = [k for k in order if k != "broll"]   # B-Roll 사용 안 함
    for kind in order:
        found = latest_broll(config) if kind == "broll" else latest_wangp(config)
        if found:
            print(f"[TOP] {kind} 사용: {found.name}")
            return found
    return None


def load_scenes() -> dict:
    data = read_json(OUTPUT_DIR / "scenes" / "scenes.json")
    scenes = data.get("scenes") or []
    return data if scenes else {}


def clip_matches_image(clip: Path, scene: dict, tolerance: float = 60.0) -> bool:
    """모션 클립이 지금 장면 이미지로 만든 것인지(파일 시각으로 판단).

    5단계로 이미지를 새로 만들면 옛 장면의 모션 클립(scene_XX_wangp.mp4)이
    그대로 남는다. 그걸 그대로 쓰면 **예전 그림(예전 말풍선 포함)**이 영상에
    나오므로, 이미지보다 오래된 클립은 쓰지 않는다.
    """
    try:
        image = Path(expand(str(scene.get("image") or "")))
        if not image.exists() or not Path(clip).exists():
            return True
        return Path(clip).stat().st_mtime + tolerance >= image.stat().st_mtime
    except Exception:
        return True


def scene_clip(scene: dict) -> Path | None:
    """장면에 붙은 WanGP 모션 클립(있으면)을 찾는다.

    웹에서 이 장면의 모션을 끄면(motion=False) 조용히 정지 이미지+줌/팬을 쓴다.
    (클립 파일이 남아 있어도 쓰지 않는다)
    """
    if scene.get("motion") is False:
        return None
    candidates = [scene.get("motion_clip") or ""]
    idx = int(scene.get("index") or 0)
    if idx:
        candidates.append(str(OUTPUT_DIR / "generated_videos" / f"scene_{idx:02d}_wangp.mp4"))
    for c in candidates:
        if not c:
            continue
        p = Path(expand(c))
        if p.exists() and probe_duration(p) > 0.3:
            if not clip_matches_image(p, scene):
                print(f"[MOTION] {idx:02d}번 모션 클립이 옛 이미지 기준 → 무시({p.name}) · 정지 이미지 사용")
                continue
            return p
    return None


def scene_broll_clip(scene: dict, default_clip: Path | None) -> Path | None:
    """장면별 B-Roll 클립.

    - scene.broll.enabled=false → 이 장면은 B-Roll 없음(장면이 화면 전체)
    - scene.broll.clip 있음     → 그 클립
    - 지정 없음               → 기본(전체) B-Roll
    """
    b = scene.get("broll") or {}
    if b.get("enabled") is False:
        return None
    raw = str(b.get("clip") or "").strip()
    if b.get("enabled") is True and raw:
        p = Path(expand(raw))
        if p.exists() and probe_duration(p) > 0.3:
            return p
    return default_clip


def _render_scene_visual(scene: dict, idx: int, width: int, px_height: int, fps: int,
                         dur: float, out_path: Path, bubble_style: dict,
                         bubble_dir: Path, fit: str = "cover",
                         anchor: str = "top") -> tuple[Path | None, str]:
    """장면 하나의 영상(모션 클립 또는 이미지)을 width×px_height 칸에 맞춰 렌더한다.

    예전에는 칸 비율(가로 1080x960)에 세로 그림(1080x1920)을 '가운데 크롭'해서
    머리와 다리가 잘렸다. 지금은 cover(위쪽 기준) 또는 contain(전체+블러 여백)으로
    장면마다 같은 규칙을 쓴다.
    """
    clip = scene_clip(scene)
    image = Path(expand(str(scene.get("image") or "")))
    if clip is not None:
        src_path: Path | None = clip
    elif image.exists():
        src_path = image
    else:
        print(f"[WARN] {idx}번 장면 소스가 없습니다: {image}")
        return None, ""

    layer = None
    bubble_text = str(scene.get("bubble_text") or "").strip()
    if bubble_text and bubble_overlay is not None:
        try:
            layer = bubble_overlay.bubble_layer_path(
                bubble_text, (width, px_height), bubble_style, bubble_dir)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] {idx}번 말풍선 레이어 실패: {type(e).__name__}: {e}")

    # 칸 비율과 그림 비율이 같으면 자를 것이 없으므로 느린 줌/팬을 쓴다
    sw, sh = probe_size(src_path)
    band_aspect = width / px_height
    same_aspect = bool(sw and sh) and abs((sw / sh) - band_aspect) <= band_aspect * 0.03
    if same_aspect:
        chains = [_zoom_chains("[0:v]", "[vfit]", width, px_height, fps, idx)]
        motion = "줌/팬"
    else:
        fit_mode = "contain" if str(fit).lower().startswith("contain") else "cover"
        chains = [scene_fit_chains("[0:v]", "[vfit]", width, px_height, fps,
                                   fit_mode, anchor, tag=f"t{idx}")]
        motion = "전체 보이기(블러 여백)" if fit_mode == "contain" else "꽉 채우기(위 기준)"

    inputs: list[str] = []
    if clip is not None:
        inputs += ["-stream_loop", "-1", "-i", str(clip)]
        src_name = f"WanGP 모션({clip.name})"
    else:
        inputs += ["-loop", "1", "-i", str(src_path)]
        src_name = f"이미지({image.name})"

    if layer is not None:
        inputs += ["-loop", "1", "-i", str(layer)]
        graph = ";".join(chains) + ";[1:v]format=rgba[ly];[vfit][ly]overlay=0:0:format=auto[ov]"
        out_label = "[ov]"
        src_desc = f"{src_name} · {motion} + 말풍선"
    else:
        graph = ";".join(chains)
        out_label = "[vfit]"
        src_desc = f"{src_name} · {motion}"

    cmd = ["ffmpeg", "-y", "-v", "error"] + inputs + [
        "-t", f"{dur:.3f}", "-filter_complex", graph, "-map", out_label, "-an",
        "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=240)
    if r.returncode != 0 or not out_path.exists():
        print(f"[WARN] {idx}번 장면 렌더 실패: {(r.stderr or '')[-300:]}")
        return None, ""
    return out_path, src_desc


def _segment_key(scene: dict, idx: int, dur: float, width: int, height: int, fps: int,
                 top_h: int, bclip: Path | None, fit: str, anchor: str,
                 bubble_style: dict) -> str:
    """입력이 같으면 다시 렌더하지 않도록 만드는 짧은 해시(9·10단계 공용 캐시 키)."""
    def stamp(p) -> str:
        try:
            st = Path(p).stat()
            return f"{st.st_size}:{int(st.st_mtime)}"
        except Exception:
            return ""

    clip = scene_clip(scene)
    payload = {
        "idx": idx, "dur": round(dur, 3), "w": width, "h": height, "fps": fps,
        "top_h": top_h, "fit": fit, "anchor": anchor,
        "img": str(scene.get("image") or ""), "img_stamp": stamp(scene.get("image") or ""),
        "clip": str(clip or ""), "clip_stamp": stamp(clip) if clip else "",
        "broll": str(bclip or ""), "broll_stamp": stamp(bclip) if bclip else "",
        "bubble": str(scene.get("bubble_text") or ""),
        "bubble_style": {k: bubble_style.get(k) for k in ("font", "font_size", "position", "margin_ratio")},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def render_scene_segments(config: dict, width: int, height: int, top_ratio: float,
                          fps: int, per_scene_broll: bool = True,
                          full: bool = False, force: bool = False) -> list[dict]:
    """장면마다 '완성 프레임'(1080x1920) 세그먼트를 만들어 목록을 돌려준다.

    - per_scene_broll=True : 장면별 B-Roll(없으면 기본 클립) + 장면을 위/아래로 합성
    - full=True            : B-Roll 없이 장면 전체 화면
    - 둘 다 아니면         : 위에 따로 얹을 B-Roll 이 있다고 보고 장면만 하단 칸에 렌더

    10단계 MP4 와 9단계 CapCut 초안이 **같은 세그먼트 파일**을 쓰기 때문에
    화면 배치가 어긋나지 않는다. 캐시는 output/scenes/segments/ 에 남는다.
    """
    data = load_scenes()
    scenes = data.get("scenes") or []
    if not scenes:
        print("[BOTTOM] scenes.json 이 없어 기존 단일 소스를 사용합니다.")
        return []

    asm = config.get("assembly", {}) or {}
    fit = str(asm.get("scene_fit") or "cover")
    anchor = str(asm.get("scene_anchor") or "top")
    bubble_style = ((config.get("scenes", {}) or {}).get("bubble") or {})
    scene_dir = OUTPUT_DIR / "scenes"
    seg_dir = scene_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    bubble_dir = scene_dir / "bubbles"

    default_broll = None
    if per_scene_broll:
        default_broll = latest_broll(config)
        if default_broll is None and broll_lib is not None:
            try:
                clips = broll_lib.list_clips()
                if clips:
                    cand = Path(clips[0]["path"])
                    if cand.exists():
                        default_broll = cand
            except Exception:
                default_broll = None
        print(f"[BROLL] 장면별 모드 · 기본 클립: {default_broll.name if default_broll else '(없음)'}")

    entries: list[dict] = []
    keep: set[str] = set()
    for scene in scenes:
        idx = int(scene.get("index") or len(entries) + 1)
        dur = max(0.4, float(scene.get("duration") or 0))
        bclip = scene_broll_clip(scene, default_broll) if per_scene_broll else None
        if full:
            top_h, bottom_h = 0, height // 2 * 2
        elif per_scene_broll:
            top_h, bottom_h = band_heights(height, top_ratio, bclip is not None)
        else:
            top_h, bottom_h = band_heights(height, top_ratio, True)

        key = _segment_key(scene, idx, dur, width, height, fps, top_h, bclip, fit, anchor, bubble_style)
        seg = seg_dir / f"seg_{idx:02d}_{key}.mp4"
        vis = seg_dir / f"vis_{idx:02d}_{key}.mp4"
        keep.update({seg.name, vis.name})

        if seg.exists() and seg.stat().st_size > 5000 and not force:
            print(f"[BOTTOM] {idx:02d} {dur:4.1f}s · 캐시 재사용({seg.name}) · 상단 {top_h}px / 하단 {bottom_h}px")
            entries.append({
                "index": idx, "file": str(seg), "duration": round(dur, 3),
                "top_h": top_h, "bottom_h": bottom_h,
                "broll": str(bclip) if bclip else "",
                "source": "cache", "from_cache": True,
            })
            continue

        visual, src_desc = _render_scene_visual(
            scene, idx, width, bottom_h, fps, dur, vis, bubble_style, bubble_dir, fit, anchor)
        if visual is None:
            continue

        if bclip is not None:
            # B-Roll 은 16:9 화면 녹화 → 칸을 채우고 가운데를 보여준다(왼쪽만 보이던 문제 수정)
            bvf = (f"scale={width}:{top_h}:force_original_aspect_ratio=increase,"
                   f"crop={width}:{top_h}:(iw-ow)/2:(ih-oh)/2,setsar=1,fps={fps}")
            cmd = ["ffmpeg", "-y", "-v", "error",
                   "-stream_loop", "-1", "-i", str(bclip), "-i", str(visual),
                   "-t", f"{dur:.3f}", "-filter_complex",
                   f"[0:v]{bvf}[b];[1:v]setsar=1[s];[b][s]vstack=inputs=2[v]",
                   "-map", "[v]", "-an",
                   "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
                   "-pix_fmt", "yuv420p", str(seg)]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=240)
            if r.returncode != 0 or not seg.exists():
                print(f"[WARN] {idx}번 B-Roll 합성 실패 → 장면 전체 화면: {(r.stderr or '')[-200:]}")
                seg = visual
            else:
                vis.unlink(missing_ok=True)
                src_desc = f"B-Roll({bclip.name}) + {src_desc}"
        else:
            seg = visual
            src_desc = f"{src_desc} · B-Roll 없음(전체 화면)"

        print(f"[BOTTOM] {idx:02d} {dur:4.1f}s · {src_desc} · 상단 {top_h}px / 하단 {bottom_h}px")
        entries.append({
            "index": idx, "file": str(seg), "duration": round(dur, 3),
            "top_h": top_h, "bottom_h": bottom_h,
            "broll": str(bclip) if bclip else "",
            "source": src_desc, "from_cache": False,
        })

    # 이번에 안 쓰는 옛 세그먼트는 '일주일 지난 것만' 정리한다.
    # (CapCut 초안은 세그먼트 경로를 참조하므로 최근 파일은 남겨야 한다)
    cutoff = datetime.now().timestamp() - 7 * 24 * 3600
    for old in list(seg_dir.glob("seg_*.mp4")) + list(seg_dir.glob("vis_*.mp4")):
        if old.name in keep:
            continue
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    if not entries:
        return []

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "width": width, "height": height, "fps": fps,
        "top_ratio": round(float(top_ratio), 3), "fit": fit, "anchor": anchor,
        "per_scene_broll": bool(per_scene_broll), "full": bool(full),
        "scenes": entries,
    }
    (seg_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return entries


def render_layout_sample(config: dict, out_path: Path, index: int | None = None,
                         fit: str | None = None, anchor: str | None = None,
                         top_ratio: float | None = None) -> dict:
    """저장하기 전에도 배치를 확인할 수 있게, 영상과 **같은 필터**로 프레임 1장을 만든다.

    B-Roll 있는 첫 장면(또는 index)을 골라 [상단 B-Roll + 하단 장면] 또는 전체 화면을 그린다.
    말풍선이 있는 장면이면 말풍선도 같이 넣는다.
    """
    data = load_scenes()
    scenes = data.get("scenes") or []
    if not scenes:
        raise RuntimeError("장면이 없습니다. 5단계(장면 생성)를 먼저 실행하세요.")

    asm = config.get("assembly", {}) or {}
    ff = config.get("ffmpeg", {}) or {}
    width = int(ff.get("shorts_width", 1080))
    height = int(ff.get("shorts_height", 1920))
    fps = int(ff.get("shorts_fps", 30))
    fit_mode_raw = str(fit or asm.get("scene_fit") or "cover").lower()
    fit_mode = "contain" if fit_mode_raw.startswith("contain") else "cover"
    anchor_val = str(anchor or asm.get("scene_anchor") or "top").lower()
    ratio = float(top_ratio if top_ratio is not None else asm.get("top_ratio", 0.5))
    use_broll = broll_enabled(config) and str(asm.get("layout") or "split") != "scenes"
    default_broll = latest_broll(config) if use_broll else None

    scene = None
    if index:
        scene = next((s for s in scenes if int(s.get("index") or 0) == int(index)), None)
    if scene is None:
        # B-Roll 이 붙는 첫 장면을 고른다(배치를 제대로 보여주기 위해)
        scene = next((s for s in scenes if scene_broll_clip(s, default_broll)), None) or scenes[0]

    idx = int(scene.get("index") or 1)
    bclip = scene_broll_clip(scene, default_broll) if use_broll else None
    top_h, bottom_h = band_heights(height, ratio, bclip is not None)
    image = Path(expand(str(scene.get("image") or "")))
    if not image.exists():
        raise RuntimeError(f"{idx}번 장면 이미지가 없습니다: {image}")

    bubble_style = ((config.get("scenes", {}) or {}).get("bubble") or {})
    bubble_dir = OUTPUT_DIR / "scenes" / "bubbles"
    layer = None
    bubble_text = str(scene.get("bubble_text") or "").strip()
    if bubble_text and bubble_overlay is not None:
        try:
            layer = bubble_overlay.bubble_layer_path(
                bubble_text, (width, bottom_h), bubble_style, bubble_dir)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 말풍선 샘플 실패: {type(e).__name__}: {e}")

    chains: list[str] = [
        scene_fit_chains("[0:v]", "[bot]", width, bottom_h, fps, fit_mode, anchor_val, tag="smp")
    ]
    bot_label = "[bot]"
    inputs: list[str] = ["-loop", "1", "-i", str(image)]
    next_idx = 1
    if layer is not None:
        inputs += ["-loop", "1", "-i", str(layer)]
        chains.append(f"[{next_idx}:v]format=rgba[ly];{bot_label}[ly]overlay=0:0:format=auto[bot2]")
        bot_label = "[bot2]"
        next_idx += 1
    if bclip is not None:
        # B-Roll 은 1초 지점 화면을 가져온다(빈 첫 프레임 방지)
        inputs += ["-ss", "1.0", "-i", str(bclip)]
        chains.append(
            f"[{next_idx}:v]scale={width}:{top_h}:force_original_aspect_ratio=increase,"
            f"crop={width}:{top_h}:(iw-ow)/2:(ih-oh)/2,setsar=1[top];"
            f"[top]{bot_label}vstack=inputs=2[vout]"
        )
    else:
        chains.append(f"{bot_label}null[vout]")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    q = ["-q:v", "3"] if out_path.suffix.lower() in (".jpg", ".jpeg") else []
    cmd = ["ffmpeg", "-y", "-v", "error"] + inputs + [
        "-frames:v", "1", "-filter_complex", ";".join(chains), "-map", "[vout]"
    ] + q + [str(out_path)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    if r.returncode != 0 or not out_path.exists():
        raise RuntimeError((r.stderr or "ffmpeg 실패").strip()[-400:])

    return {
        "path": str(out_path),
        "index": idx,
        "width": width,
        "height": height,
        "top_h": top_h,
        "bottom_h": bottom_h,
        "fit": fit_mode,
        "anchor": anchor_val,
        "fit_text": ("전체 보이기(블러 여백)" if fit_mode == "contain"
                     else f"꽉 채우기({'가운데' if anchor_val == 'center' else '머리'} 기준)"),
        "broll": Path(bclip).name if bclip else "",
        "bubble": bool(layer),
    }


def concat_segments(entries: list[dict], out: Path | None = None) -> Path | None:
    """세그먼트 목록을 순서대로 이어 붙여 한 개 MP4 로 만든다."""
    files = [Path(e["file"]) for e in entries if Path(e["file"]).exists()]
    if not files:
        return None
    seg_dir = files[0].parent
    out = out or (OUTPUT_DIR / "scenes" / "bottom.mp4")
    list_file = seg_dir / "segments.txt"
    list_file.write_text("\n".join(f"file '{p.name}'" for p in files) + "\n", encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
           "-i", str(list_file), "-c", "copy", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    if r.returncode != 0 or not out.exists():
        print(f"[WARN] 세그먼트 합치기 실패: {(r.stderr or '')[-300:]}")
        return None
    return out


def pick_bottom(config: dict) -> Path | None:
    b = ((config.get("assembly", {}) or {}).get("bottom_source") or "auto").strip()
    if b.lower() not in ("", "auto", "wangp", "broll"):
        p = Path(expand(b))               # 직접 경로
        return p if p.exists() else None

    if b.lower() == "broll" and not broll_enabled(config):
        b = "assets"

    if b.lower() != "assets" and b.lower() in ("", "auto", "wangp"):
        wan = latest_wangp(config)
        if wan:
            print(f"[BOTTOM] WanGP 영상 사용: {wan.name}")
            return wan
        if b.lower() == "wangp":
            print("[BOTTOM] WanGP 결과가 없어 너구리 이미지/애니메이션으로 대체합니다.")

    assets = Path(expand((config.get("paths", {}) or {}).get("assets_dir", str(PROJECT / "assets"))))
    raccoon_dir = assets / "raccoon"
    for pattern in ("anim_*.mp4", "anim*.mp4", "raccoon_*.mp4"):
        vid = latest_file(raccoon_dir, pattern)
        if vid:
            return vid
    host = raccoon_dir / "host.png"
    if host.exists():
        return host
    return None


# ----------------------------------------------------------------------
# ffmpeg
# ----------------------------------------------------------------------
def build_full_command(background: str, voice: str, broll: str | None, srt: str | None,
                       out: str, duration: float, width: int, height: int, fps: int,
                       crf: int, sub_style: str = "") -> list[str]:
    base = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps={fps}"
    )
    subs = subs_filter(srt, sub_style)

    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", background]
    if broll:
        cmd += ["-stream_loop", "-1", "-i", broll]
        voice_index = 2
        filter_complex = (
            f"[0:v]{base}[bg];"
            f"[1:v]scale={width}:-2,setsar=1[br];"
            f"[bg][br]overlay=(W-w)/2:0[ov];"
            f"[ov]null{subs}[v]"
        )
        cmd += ["-i", voice, "-filter_complex", filter_complex, "-map", "[v]", "-map", f"{voice_index}:a"]
    else:
        voice_index = 1
        cmd += ["-i", voice, "-vf", f"{base}{subs}"]

    cmd += [
        "-t", f"{duration:.2f}",
        "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out,
    ]
    return cmd


def build_split_command(top: str | None, bottom: str | None, voice: str, srt: str | None,
                        out: str, duration: float, width: int, height: int, fps: int,
                        crf: int, top_ratio: float, sub_style: str = "") -> list[str]:
    """상단(top video) + 하단(bottom) 2분할. top 이 없으면 bottom 만 전체 화면(9:16)."""
    if top:
        top_h = int(round(height * max(0.3, min(0.75, top_ratio)))) // 2 * 2
        bot_h = height - top_h
    else:
        top_h = 0
        bot_h = height // 2 * 2
    crf = int(crf)

    inputs: list[str] = []
    chains: list[str] = []

    # ---- 하단 ----
    if bottom and is_video(Path(bottom)):
        inputs += ["-stream_loop", "-1", "-i", bottom]
        chains.append(
            f"[0:v]scale={width}:{bot_h}:force_original_aspect_ratio=increase,"
            f"crop={width}:{bot_h},setsar=1,fps={fps}[bot]"
        )
    else:
        inputs += ["-loop", "1", "-i", bottom]
        # 느린 줌인(정지 이미지가 살아 움직이게) — 화면 전체를 유지한다
        zoom_w, zoom_h = width * 2, bot_h * 2
        chains.append(
            f"[0:v]scale={zoom_w}:{zoom_h}:force_original_aspect_ratio=increase,"
            f"crop={zoom_w}:{zoom_h},"
            f"zoompan=z='min(1+0.00035*on,1.12)':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)*0.30'"
            f":d=1:s={width}x{bot_h}:fps={fps},setsar=1[bot]"
        )

    # ---- 상단 ----
    if top:
        inputs += ["-stream_loop", "-1", "-i", top]
        chains.append(
            f"[1:v]scale={width}:{top_h}:force_original_aspect_ratio=increase,"
            f"crop={width}:{top_h},setsar=1,fps={fps}[topv]"
        )
        stack = "[topv][bot]vstack=inputs=2"
        voice_index = 2
    else:
        stack = "[bot]null"
        voice_index = 1

    subs = subs_filter(srt, sub_style)
    graph = ";".join(chains) + f";{stack}{subs}[v]"

    cmd = ["ffmpeg", "-y"] + inputs + ["-i", voice,
                                        "-filter_complex", graph, "-map", "[v]", "-map", f"{voice_index}:a"]
    cmd += [
        "-t", f"{duration:.2f}",
        "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out,
    ]
    return cmd


def main() -> int:
    ap = argparse.ArgumentParser(description="FFmpeg 쇼츠 조립")
    ap.add_argument("--layout", choices=["split", "full", "scenes"], default=None,
                    help="split: 상단 영상 + 하단 너구리 / scenes: B-Roll 없이 장면 전체 화면 / full: 배경 + 상단 B-Roll")
    ap.add_argument("--top", default=None, help="상단에 넣을 영상 경로 직접 지정")
    ap.add_argument("--bottom", default=None, help="하단 너구리 이미지/영상 직접 지정")
    ap.add_argument("--no-broll", action="store_true", help="full 레이아웃에서 B-Roll 제외")
    ap.add_argument("--image", default=None, help="full 레이아웃 배경 이미지")
    ap.add_argument("--subs", dest="subs", action="store_true", default=None,
                    help="Whisper 자막을 영상에 굽기 (기본: config.assembly.include_subtitles)")
    ap.add_argument("--no-subs", dest="subs", action="store_false",
                    help="자막을 굽지 않기 (권장)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    config = load_config()
    ff = config.get("ffmpeg", {}) or {}
    asm = config.get("assembly", {}) or {}
    width = int(ff.get("shorts_width", 1080))
    height = int(ff.get("shorts_height", 1920))
    fps = int(ff.get("shorts_fps", 30))
    crf = int(asm.get("crf", 23))
    sub_style = (asm.get("subtitle_style") or "")
    layout = args.layout or asm.get("layout", "split")
    top_ratio = float(asm.get("top_ratio", 0.5))

    voice = OUTPUT_DIR / "voice.mp3"
    if not voice.exists():
        print("[ERROR] output/voice.mp3 가 없습니다. 4단계(TTS)를 먼저 완료하세요.")
        return 1
    duration = probe_duration(voice)
    if duration < 0.5:
        print(f"[ERROR] 음성 길이가 비정상입니다({duration:.2f}초).")
        return 1

    out = Path(expand(args.out)) if args.out else OUTPUT_DIR / "final" / f"shorts_final_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)

    # 자막: 기본은 굽지 않는다 (tiny 받아쓰기 오타 방지). 필요할 때만 --subs 또는 config 로 켠다.
    include_subs = bool(asm.get("include_subtitles", False)) if args.subs is None else bool(args.subs)
    srt = OUTPUT_DIR / "subtitles.srt"
    if not include_subs:
        srt = None
    elif not srt.exists() or srt.stat().st_size < 10:
        srt = None

    if layout in ("split", "scenes"):
        # 장면마다 '완성 프레임'을 렌더하고 이어 붙인다.
        #  - B-Roll 을 쓰면: 장면마다 [상단 B-Roll 칸 + 하단 장면 칸] (B-Roll 은 칸을 꽉 채우고 가운데)
        #  - B-Roll 을 안 쓰면: 장면 전체 화면 + 말풍선
        # 9단계 CapCut 초안도 같은 세그먼트를 쓰므로 두 결과의 화면 배치가 같다.
        top_source = str(asm.get("top_source") or "auto").strip().lower()
        bake_broll = (layout != "scenes" and broll_enabled(config)
                      and top_source not in ("none", "wangp") and not args.top)
        legacy_top = (not bake_broll) and layout != "scenes" and not args.bottom \
            and (top_source == "wangp" or bool(args.top))
        entries: list[dict] = []
        top_path: Path | None = None
        bottom: Path | None = None
        if args.bottom:
            bottom = Path(expand(args.bottom))
        elif str(asm.get("bottom_source", "")).strip().lower() in ("scenes", "auto"):
            entries = render_scene_segments(
                config, width, height, top_ratio, fps,
                per_scene_broll=bake_broll,
                full=(not bake_broll and not legacy_top),
            )
            if entries:
                bottom = concat_segments(entries)
        if legacy_top and entries:
            top_path = pick_top(config, args.top)
        if bottom is None or not bottom.exists():
            bottom = pick_bottom(config)
            top_path = pick_top(config, args.top) if layout != "scenes" else None
        if not bottom or not bottom.exists():
            print("[ERROR] 하단에 넣을 너구리 이미지/영상이 없습니다. assets/raccoon/host.png 를 확인하세요.")
            return 1

        if entries:
            print(f"[ASSEMBLE] 장면 {len(entries)}개 · {duration:.1f}초 / {width}x{height}@{fps}")
            print(f"    배치: {'B-Roll(상단) + 장면(하단)' if bake_broll else '장면 전체 화면'}"
                  f" · 맞춤={asm.get('scene_fit') or 'cover'}({asm.get('scene_anchor') or 'top'})"
                  f" · 상단 비율={int(round(top_ratio * 100))}%")
            if top_path:
                print(f"    상단: {top_path}")
        else:
            print(f"[ASSEMBLE] split · {duration:.1f}초 / {width}x{height}@{fps} (상단 {int(height*top_ratio)}px)")
            print(f"    상단: {top_path or '(없음 → 하단만 전체 화면)'}")
        print(f"    하단: {bottom} ({'영상' if is_video(bottom) else '이미지/영상'})")
        print(f"    음성: {voice}")
        print(f"    자막: {srt or '(굽지 않음 — 쇼츠/릴스 자동 자막 사용)'}")
        print(f"    출력: {out}")

        cmd = build_split_command(
            str(top_path) if top_path else None, str(bottom), str(voice),
            str(srt) if srt else None, str(out), duration, width, height, fps, crf,
            (top_ratio if top_path else 0.0), sub_style,
        )
    else:
        # ---- 예전(전체 화면) 레이아웃 ----
        image = Path(expand(args.image)) if args.image else None
        if not image or not image.exists():
            bottom = pick_bottom(config)
            if bottom and not is_video(bottom):
                image = bottom
        if not image or not Path(image).exists():
            image = OUTPUT_DIR / "capcut_assets" / "background.png"
            image.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0x101418:s={width}x{height}",
                            "-frames:v", "1", str(image)], capture_output=True, text=True, timeout=60)
            if not image.exists():
                print("[ERROR] 배경 이미지를 만들지 못했습니다.")
                return 1
            print("[INFO] 배경 이미지가 없어 단색 배경을 사용합니다.")

        broll = None
        if not args.no_broll:
            top_path = pick_top(config, args.top)
            if top_path:
                broll = str(top_path)

        print(f"[ASSEMBLE] full · {duration:.1f}초 / {width}x{height}@{fps}")
        print(f"    배경: {image}")
        print(f"    B-Roll: {broll or '(없음)'}")
        print(f"    자막: {srt or '(굽지 않음)'}")
        print(f"    출력: {out}")

        cmd = build_full_command(str(image), str(voice), broll, str(srt) if srt else None,
                                 str(out), duration, width, height, fps, crf, sub_style)

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=900)
    except FileNotFoundError:
        print("[ERROR] ffmpeg 없음. winget install Gyan.FFmpeg")
        return 1
    except subprocess.TimeoutExpired:
        print("[ERROR] ffmpeg 타임아웃")
        return 1

    if r.returncode != 0:
        print(f"[ERROR] ffmpeg 실패(코드 {r.returncode}): {(r.stderr or '')[-900:]}")
        return 1

    if not out.exists() or out.stat().st_size < 20_000:
        print("[ERROR] 결과 파일이 없거나 너무 작습니다.")
        return 1
    out_duration = probe_duration(out)
    if out_duration < duration * 0.8:
        print(f"[ERROR] 결과 길이가 음성보다 짧습니다({out_duration:.2f}s < {duration:.2f}s).")
        return 1

    print(f"[OK] 미리보기 완성: {out} ({out_duration:.1f}초, {out.stat().st_size/1024:.0f}KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
