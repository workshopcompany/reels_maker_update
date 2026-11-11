"""
[Module 0] 환경 검증 및 패키지 설치
=====================================
- winget 패키지 확인 (FFmpeg, yt-dlp)
- Python 가상환경 확인/생성 (config.paths.venv)
- 파이프라인 필수 패키지 설치 (faster-whisper, pycapcut, yt-dlp, Flask, Google API 등)
- Whisper 모델 존재 확인 (없으면 faster-whisper로 다운로드)

  python setup_environment.py
  python setup_environment.py --skip-winget
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT / "config.json"

# stdout 를 새 래퍼로 교체하면, 웹 서버가 이 모듈을 import 할 때 기존 스트림이 닫혀
# Werkzeug 로깅/소켓이 깨진다. 인코딩만 바꾸는 reconfigure 를 사용한다.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REQUIRED_PACKAGES = [
    "faster-whisper",
    "pycapcut==0.0.4",
    "yt-dlp",
    "elevenlabs",
    "edge-tts",
    "requests",
    "python-dotenv",
    "Flask",
    "mutagen",
    "google-api-python-client",
    "google-auth-oauthlib",
    "google-auth-httplib2",
    # 이미지/영상 생성(선택 기능)에 사용
    "opencv-python",
    "Pillow",
]


def expand(path: str) -> str:
    return os.path.normpath(os.path.expandvars(path))


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def verify_ffmpeg() -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def ensure_winget_packages() -> None:
    packages = [("Gyan.FFmpeg", "ffmpeg"), ("yt-dlp.yt-dlp", "yt-dlp")]
    for pkg_id, _cmd in packages:
        try:
            r = subprocess.run(["winget", "list", "--id", pkg_id], capture_output=True, text=True, timeout=90)
            installed = r.returncode == 0 and pkg_id in (r.stdout or "")
        except Exception:
            installed = False
        if installed:
            print(f"[OK] {pkg_id} 이미 설치됨")
            continue
        print(f"[INSTALL] {pkg_id} 설치 중...")
        subprocess.run(["winget", "install", "-e", "--id", pkg_id,
                        "--accept-source-agreements", "--accept-package-agreements"], timeout=600)


def resolve_venv(config: dict) -> Path:
    candidates = []
    cfg = ((config.get("paths", {}) or {}).get("venv") or "")
    if cfg:
        candidates.append(Path(expand(cfg)))
    candidates.append(Path(expand(r"%USERPROFILE%\.pycapcut")))
    candidates.append(PROJECT / ".venv")
    for c in candidates:
        if (c / "Scripts" / "python.exe").exists() or (c / "bin" / "python").exists():
            return c
    return candidates[0]


def venv_python(venv: Path) -> Path:
    win = venv / "Scripts" / "python.exe"
    return win if win.exists() else venv / "bin" / "python"


def create_venv(venv: Path) -> None:
    if venv_python(venv).exists():
        print(f"[OK] 가상환경 존재: {venv}")
        return
    print(f"[INSTALL] 가상환경 생성: {venv}")
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)


def install_python_packages(venv: Path) -> None:
    py = venv_python(venv)
    if not py.exists():
        print(f"[ERROR] 가상환경 python 없음: {py}")
        return
    print("[INSTALL] pip 업그레이드...")
    subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip", "--disable-pip-version-check"], check=False)
    print(f"[INSTALL] 패키지 설치: {', '.join(REQUIRED_PACKAGES)}")
    r = subprocess.run([str(py), "-m", "pip", "install", *REQUIRED_PACKAGES, "--disable-pip-version-check"])
    if r.returncode != 0:
        print("[WARN] 일부 패키지 설치 실패 — 위 pip 로그를 확인하세요.")


def ensure_whisper_model(config: dict, venv: Path) -> None:
    cfg_path = ((config.get("paths", {}) or {}).get("whisper_model") or "")
    if cfg_path:
        p = Path(expand(cfg_path))
        if p.exists():
            print(f"[OK] Whisper 모델 존재: {p}")
            return
    # 프로젝트 내 스냅샷 탐색
    snaps = list(PROJECT.glob("models/whisper/**/model.bin"))
    if snaps:
        print(f"[OK] Whisper 모델 발견: {snaps[0].parent}")
        return

    py = venv_python(venv)
    model = ((config.get("whisper", {}) or {}).get("fallback_model")
             or os.getenv("RACCOON_WHISPER_FALLBACK") or "small")
    print(f"[DOWNLOAD] faster-whisper '{model}' 모델을 내려받습니다 (수백 MB, 시간이 걸릴 수 있음)")
    code = (
        "from faster_whisper import WhisperModel\n"
        f"WhisperModel('{model}', device='cpu', compute_type='int8')\n"
        "print('[OK] whisper download complete')\n"
    )
    r = subprocess.run([str(py), "-c", code])
    if r.returncode != 0:
        print(f"[WARN] 자동 다운로드 실패. `{py} -c \"{code.strip()}\"` 를 직접 실행해 보세요.")


def ensure_directories(config: dict) -> None:
    paths = config.get("paths", {}) or {}
    dirs = [
        paths.get("output_dir", str(PROJECT / "output")),
        paths.get("generated_images", str(PROJECT / "output" / "generated_images")),
        paths.get("generated_videos", str(PROJECT / "output" / "generated_videos")),
        paths.get("final_output", str(PROJECT / "output" / "final")),
        paths.get("trending_cache", str(PROJECT / "output" / "trending")),
        paths.get("assets_dir", str(PROJECT / "assets")),
        str(Path(paths.get("assets_dir", str(PROJECT / "assets"))) / "raccoon"),
        str(Path(paths.get("assets_dir", str(PROJECT / "assets"))) / "sfx"),
        paths.get("broll_dir", r"%USERPROFILE%\릴스자료"),
        str(PROJECT / "scripts" / "logs"),
        str(PROJECT / "output" / "capcut_export"),
    ]
    for d in dirs:
        if not d:
            continue
        Path(expand(d)).mkdir(parents=True, exist_ok=True)
    print(f"[OK] 디렉토리 {len(dirs)}개 준비")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-winget", action="store_true")
    args = ap.parse_args()

    config = load_config()
    print("=" * 62)
    print("🦝 너구리 쇼츠 자동화 - 환경 설정")
    print("=" * 62)

    results: list[tuple[str, str, str]] = []

    def step(name, fn):
        try:
            fn()
            results.append((name, "OK", ""))
        except Exception as e:
            results.append((name, "FAIL", str(e)[:120]))

    def check_ffmpeg() -> None:
        if not verify_ffmpeg():
            raise RuntimeError("ffmpeg 없음 → winget install -e --id Gyan.FFmpeg")
        print("[OK] ffmpeg 사용 가능")

    venv = resolve_venv(config)
    step("ffmpeg 확인", check_ffmpeg)
    if not args.skip_winget:
        step("winget 패키지", ensure_winget_packages)
    step("디렉토리 준비", lambda: ensure_directories(config))
    step("가상환경", lambda: create_venv(venv))
    step("Python 패키지", lambda: install_python_packages(venv))
    step("Whisper 모델", lambda: ensure_whisper_model(config, venv))

    print("\n" + "=" * 62)
    print(f"{'단계':<18}{'상태':<8}메시지")
    print("-" * 62)
    for name, status, msg in results:
        print(f"{name:<18}{status:<8}{msg}")
    print("=" * 62)
    print(f"가상환경: {venv}")
    print("웹 UI 실행: " + str(venv_python(venv)) + " web_pipeline.py  → http://127.0.0.1:5000")
    return 0 if all(s == "OK" for _, s, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
