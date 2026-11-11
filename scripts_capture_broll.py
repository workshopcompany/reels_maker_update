"""
[단계 5] B-Roll 준비 (화면 녹화 / 창 녹화 / MP4 업로드)
=========================================================
  python scripts_capture_broll.py                       # 설정값 사용 (config.broll / output/broll_source.json)
  python scripts_capture_broll.py --window "제목 일부"    # 특정 창만 녹화
  python scripts_capture_broll.py --source "D:\\clip.mp4" # 이미 있는 MP4 사용
  python scripts_capture_broll.py 8                      # 8초 녹화

설정 우선순위: CLI 인자 > output/broll_source.json(웹 UI 저장) > config.json

주의
- gdigrab 은 현재 로그온한 데스크톱을 캡처합니다. 최소화된 창/DRM 창/하드웨어 오버레이는 실패할 수 있습니다.
- 창 녹화는 그 창이 화면에 보이는 상태여야 하며, 다른 창에 가려져도 그 창의 내용이 나옵니다(Windows GDI 특성).
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
SOURCE_FILE = OUTPUT_DIR / "broll_source.json"
META_FILE = OUTPUT_DIR / "broll_meta.json"

sys.path.insert(0, str(PROJECT))
import broll_lib  # noqa: E402  (장면별 B-Roll 선택용 클립 라이브러리)

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
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def broll_dir(config: dict) -> Path:
    d = Path(expand((config.get("paths", {}) or {}).get("broll_dir", r"%USERPROFILE%\릴스자료")))
    d.mkdir(parents=True, exist_ok=True)
    return d


# ----------------------------------------------------------------------
# 창 목록 (웹 UI 드롭다운용)
# ----------------------------------------------------------------------
def _process_name(pid: int) -> str:
    """PID → 실행 파일 이름 (Win32 API, 서브프로세스 없음)."""
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return Path(buf.value).stem
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def _enum_windows() -> list[dict]:
    """제목이 있는 보이는 창을 Win32 EnumWindows 로 직접 열거한다.
    (PowerShell 서브프로세스를 쓰지 않아 느리거나 콘솔 인코딩에 깨지지 않는다)"""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    results: list[dict] = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):  # noqa: ANN001
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = (buf.value or "").strip()
            if not title:
                return True
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w < 80 or h < 80:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            results.append({
                "title": title,
                "pid": int(pid.value),
                "process": _process_name(int(pid.value)),
                "x": int(rect.left),
                "y": int(rect.top),
                "w": int(w),
                "h": int(h),
            })
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(EnumProc(_cb), 0)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 창 열거 실패: {e}")
    return results


def list_windows() -> list[dict]:
    """화면에 제목이 있는 창 목록. 실패하면 빈 리스트."""
    return [
        {"title": w["title"], "pid": w["pid"], "process": w["process"]}
        for w in _enum_windows()
    ]


# ----------------------------------------------------------------------
# ffmpeg
# ----------------------------------------------------------------------
def build_ffmpeg_command(duration_sec: int, output_path: str, framerate: int = 30,
                         window_title: str | None = None) -> list[str]:
    source = f"title={window_title}" if window_title else "desktop"
    return [
        "ffmpeg", "-y",
        "-f", "gdigrab",
        "-framerate", str(framerate),
        "-t", str(duration_sec),
        "-i", source,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        # gdigrab 은 홀수 해상도가 나올 수 있어 짝수로 보정한다(h264 요구사항)
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        output_path,
    ]


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def record_screen(duration_sec: int = 5, output_path: str | None = None, framerate: int = 30,
                  window_title: str | None = None) -> str:
    if sys.platform != "win32":
        print("[ERROR] gdigrab 은 Windows 전용입니다.")
        return ""

    config = load_config()
    if not output_path:
        output_path = str(broll_dir(config) / f"broll_{_stamp()}.mp4")

    cmd = build_ffmpeg_command(duration_sec, output_path, framerate, window_title)
    target = f"창 '{window_title}'" if window_title else "전체 데스크톱"
    print(f"[REC] {duration_sec}초 · {target} → {output_path}")
    print(f"[CMD] {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=duration_sec + 60)
    except FileNotFoundError:
        print("[ERROR] ffmpeg 를 찾을 수 없습니다. winget install Gyan.FFmpeg")
        return ""
    except subprocess.TimeoutExpired:
        print("[ERROR] ffmpeg 녹화 타임아웃")
        return ""

    if r.returncode != 0:
        print(f"[ERROR] ffmpeg 실패(코드 {r.returncode}): {(r.stderr or '')[-500:]}")
        if window_title:
            print("[HINT] 창 제목이 정확한지(대소문자/공백 포함) 확인하거나 전체 화면 모드로 다시 시도하세요.")
        return ""

    out = Path(output_path)
    if not out.exists() or out.stat().st_size < 1024:
        print("[ERROR] 녹화 파일이 없거나 비어 있습니다.")
        return ""
    print(f"[OK] B-Roll 저장: {out} ({out.stat().st_size/1024:.0f}KB)")
    return str(out)


def use_source_file(source: str) -> str:
    """사용자가 지정/업로드한 MP4 를 B-Roll 로 사용 (broll_dir 로 복사해 산출물 규칙에 맞춘다)."""
    src = Path(expand(source))
    if not src.exists() or not src.is_file():
        print(f"[ERROR] 소스 MP4 가 없습니다: {src}")
        return ""
    if src.stat().st_size < 10_000:
        print(f"[ERROR] 소스 MP4 가 너무 작습니다: {src.stat().st_size}B")
        return ""
    config = load_config()
    dest = broll_dir(config) / f"broll_upload_{_stamp()}{src.suffix.lower() or '.mp4'}"
    try:
        shutil.copy2(src, dest)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 파일 복사 실패: {e}")
        return ""
    print(f"[OK] 업로드 MP4 사용: {dest} ({dest.stat().st_size/1024:.0f}KB)")
    return str(dest)


def window_rect(needle: str) -> dict:
    """창 제목(부분 일치)으로 화면 좌표를 구한다. 없으면 {}
    ffmpeg gdigrab 의 title= 은 ANSI 변환 때문에 이모지/일부 한글 제목을 못 찾을 수 있어
    그 경우 데스크톱의 해당 영역을 잘라 녹화하는 fallback 에 사용한다."""
    needle = (needle or "").strip().lower()
    if not needle:
        return {}
    windows = _enum_windows()
    exact = [w for w in windows if w["title"].lower() == needle]
    cands = exact or [w for w in windows if needle in w["title"].lower()]
    if not cands:
        return {}
    w = cands[0]
    return {k: w[k] for k in ("title", "x", "y", "w", "h")}


def build_region_command(rect: dict, duration_sec: int, output_path: str, framerate: int = 30) -> list[str]:
    """창 영역만 녹화 (데스크톱 캡처 후 오프셋/크기 지정)."""
    x = max(0, int(rect.get("x") or 0))
    y = max(0, int(rect.get("y") or 0))
    w = int(rect.get("w") or 0) // 2 * 2
    h = int(rect.get("h") or 0) // 2 * 2
    return [
        "ffmpeg", "-y",
        "-f", "gdigrab",
        "-framerate", str(framerate),
        "-offset_x", str(x), "-offset_y", str(y),
        "-video_size", f"{w}x{h}",
        "-t", str(duration_sec),
        "-i", "desktop",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        output_path,
    ]


def record_region(rect: dict, duration_sec: int, output_path: str, framerate: int = 30) -> str:
    w, h = int(rect.get("w") or 0), int(rect.get("h") or 0)
    if w < 100 or h < 100:
        print(f"[ERROR] 창 크기가 비정상입니다({w}x{h}). 최소화된 창인지 확인하세요.")
        return ""
    cmd = build_region_command(rect, duration_sec, output_path, framerate)
    print(f"[REC] {duration_sec}초 · 창 영역 {w}x{h} @({rect.get('x')},{rect.get('y')}) → {output_path}")
    print(f"[CMD] {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=duration_sec + 60)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 영역 녹화 실패: {e}")
        return ""
    if r.returncode != 0:
        print(f"[ERROR] ffmpeg(영역) 실패(코드 {r.returncode}): {(r.stderr or '')[-400:]}")
        return ""
    out = Path(output_path)
    if not out.exists() or out.stat().st_size < 2000:
        print("[ERROR] 영역 녹화 파일이 없거나 비어 있습니다.")
        return ""
    print(f"[OK] B-Roll 저장(영역): {out} ({out.stat().st_size/1024:.0f}KB)")
    return str(out)


def resolve_window_title(title: str, process: str = "") -> str:
    """부분 제목으로도 고를 수 있게 정확한 창 제목으로 변환 (ffmpeg gdigrab 은 정확 매칭).
    브라우저 탭 제목처럼 제목이 바뀌는 앱은 process 이름으로 다시 찾는다."""
    title = (title or "").strip()
    windows = list_windows()
    if not windows:
        return title
    titles = [w["title"] for w in windows]
    if title in titles:
        return title
    matches = [t for t in titles if title.lower() in t.lower()]
    if not matches and process:
        by_proc = [w["title"] for w in windows if (w.get("process") or "").lower() == process.lower()]
        if by_proc:
            print(f"[INFO] 제목이 바뀐 것 같습니다 → 같은 프로그램({process}) 창 사용: {by_proc[0]}")
            return by_proc[0]
    if not matches:
        print(f"[WARN] 제목에 '{title}' 이(가) 들어간 창을 찾지 못했습니다. 입력한 그대로 시도합니다.")
        return title
    if len(matches) > 1:
        print(f"[WARN] '{title}' 와 일치하는 창이 {len(matches)}개 → 첫 번째 사용: {matches[0]}")
    return matches[0]


def resolve_settings(args, config: dict) -> dict:
    """CLI > output/broll_source.json(웹 저장) > config.json"""
    broll_cfg = config.get("broll", {}) or {}
    web = read_json(SOURCE_FILE) if SOURCE_FILE.exists() else {}
    settings = {
        "mode": broll_cfg.get("mode", "desktop"),
        "window_title": broll_cfg.get("window_title", ""),
        "process": broll_cfg.get("process", ""),
        "source": broll_cfg.get("source", ""),
        "duration_sec": int(broll_cfg.get("duration_sec", 5)),
    }
    for k in ("mode", "window_title", "process", "source", "duration_sec"):
        if web.get(k) not in (None, ""):
            settings[k] = web[k]
    if args.duration_opt or args.duration:
        settings["duration_sec"] = int(args.duration_opt or args.duration)
    if args.window:
        settings["mode"] = "window"
        settings["window_title"] = args.window
    if args.source:
        settings["mode"] = "file"
        settings["source"] = args.source
    if args.desktop:
        settings["mode"] = "desktop"
    return settings


def main() -> int:
    ap = argparse.ArgumentParser(description="B-Roll 준비 (녹화/창 녹화/MP4 사용)")
    ap.add_argument("duration", nargs="?", type=int, default=None, help="녹화 길이(초)")
    ap.add_argument("--duration", dest="duration_opt", type=int, default=None)
    ap.add_argument("--window", default=None, help="녹화할 창 제목(전체 또는 일부)")
    ap.add_argument("--source", default=None, help="사용할 MP4 경로")
    ap.add_argument("--desktop", action="store_true", help="전체 화면으로 강제")
    ap.add_argument("--output", default=None)
    ap.add_argument("--framerate", type=int, default=None)
    args = ap.parse_args()

    config = load_config()
    explicit = any([args.window, args.source, args.desktop, args.duration, args.duration_opt])
    if not bool((config.get("broll", {}) or {}).get("enabled", True)) and not explicit:
        print("[SKIP] config.broll.enabled=false (B-Roll 사용 안 함)")
        print("[HINT] 장면 이미지/영상 전체 화면으로 만들려면 그대로 두고 10단계를 실행하세요.")
        return 0
    settings = resolve_settings(args, config)
    framerate = args.framerate or int((config.get("ffmpeg", {}) or {}).get("framerate", 30))
    duration = int(settings["duration_sec"])
    mode = str(settings["mode"])
    window_title = (settings.get("window_title") or "").strip()
    source = (settings.get("source") or "").strip()

    print(f"[MODE] {mode}" + (f" · 창 '{window_title}'" if window_title and mode == "window" else "")
          + (f" · 파일 {source}" if source and mode == "file" else ""))

    if mode == "file":
        if not source:
            print("[ERROR] mode=file 인데 source(MP4 경로)가 없습니다.")
            return 1
        path = use_source_file(source)
    elif mode == "window":
        if not window_title:
            print("[ERROR] mode=window 인데 창 제목이 없습니다.")
            return 1
        window_title = resolve_window_title(window_title, str(settings.get("process") or ""))
        print(f"[WINDOW] 녹화 대상: {window_title}")
        out = args.output or str(broll_dir(config) / f"broll_{_stamp()}.mp4")

        # 1) 창 영역(데스크톱 픽셀) 우선: Chrome/Edge 처럼 GPU 합성 창은 title 캡처가 검게 나온다.
        rect = window_rect(window_title)
        path = ""
        if rect:
            print(f"[WINDOW] 창 영역: {rect.get('w')}x{rect.get('h')} @({rect.get('x')},{rect.get('y')})")
            print("[HINT] 대상 창을 다른 창에 가리지 말고 화면에 보이게 두세요.")
            path = record_region(rect, duration, out, framerate)
        if not path:
            # 2) 창 핸들 캡처(가려져도 내용이 나오지만 일부 앱은 검은 화면)
            print("[RETRY] 영역 캡처 실패 → 창 핸들(title) 캡처로 재시도")
            path = record_screen(duration, out, framerate, window_title)
        if not path:
            print("[HINT] 창이 닫혔거나 제목이 바뀌었습니다. 창 목록을 새로고침한 뒤 다시 선택하세요.")
    else:
        path = record_screen(duration, args.output, framerate)

    if not path:
        print("[FAIL] B-Roll 준비 실패")
        return 1

    # 장면마다 다른 B-Roll 을 고를 수 있게 라이브러리(output/broll_clips)에도 넣는다
    label = {
        "window": f"창 녹화 · {window_title}",
        "file": f"업로드 · {Path(source).stem}",
    }.get(mode, "전체 화면 녹화")
    try:
        clip = broll_lib.register(path, label=label, source=mode)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] B-Roll 라이브러리 등록 실패(무시): {e}")
        clip = None

    meta = {
        "mode": mode,
        "window_title": window_title if mode == "window" else "",
        "process": (settings.get("process") or "") if mode == "window" else "",
        "source": source if mode == "file" else "",
        "duration_sec": duration,
        "output": path,
        "clip_path": (clip or {}).get("path", path),
        "clip_label": (clip or {}).get("label", ""),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "size_bytes": Path(path).stat().st_size,
    }
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DONE] {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
