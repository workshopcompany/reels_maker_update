"""
[단계 9, 선택] YouTube Shorts 업로드
======================================
  python scripts_publish_youtube.py --latest            # output/final 최신 MP4 (확인 프롬프트)
  python scripts_publish_youtube.py --latest --yes      # 확인 없이 업로드 (자동 모드)
  python scripts_publish_youtube.py --latest --dry      # 실제 업로드 없이 미리보기

필요 조건
- scripts/.secrets/youtube/client_secret.json
- 첫 실행 시 브라우저 OAuth 동의 (token.pickle 생성)
- config.youtube.default_privacy (기본 private)

주의: 업로드는 되돌리기 어려운 작업입니다. config.youtube.enabled=false 이면
자동 모드에서 이 단계는 건너뜁니다. 수동 실행은 항상 확인 프롬프트를 띄웁니다(--yes 제외).
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT / "config.json"
OUTPUT_DIR = PROJECT / "output"
AUTH_STATE = OUTPUT_DIR / "youtube_auth.json"


def write_auth_state(ok: bool, detail: str = "") -> None:
    """마지막 인증 확인 결과를 남긴다(웹 배지가 '만료'를 구분해 보여줄 수 있게)."""
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        AUTH_STATE.write_text(json.dumps({
            "ok": bool(ok), "detail": detail,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, str(PROJECT))
import youtube_meta  # noqa: E402  (제목·설명문구·태그·썸네일 추천)

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


def latest_final_video(final_dir: Path) -> Path | None:
    if not final_dir.exists():
        return None
    files = [p for p in final_dir.glob("*.mp4") if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def get_youtube_service(config: dict, reauth: bool = False):
    """YouTube API 서비스 객체를 만든다.

    저장된 토큰이 만료·폐기(invalid_grant)면 자동 갱신이 실패하면서 그대로 예외가
    올라와 업로드가 영영 실패한다. 여기서는 갱신 실패를 잡아 **브라우저 승인을 다시**
    받아 token.pickle 을 새로 쓴다. reauth=True 면 토큰을 무시하고 무조건 재승인.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    yt_cfg = config.get("youtube", {}) or {}
    client_secret = expand(yt_cfg.get("client_secret_path") or os.getenv("YOUTUBE_CLIENT_SECRET", ""))
    token_path = expand(yt_cfg.get("token_path") or os.getenv("YOUTUBE_TOKEN_PICKLE", ""))
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]

    creds = None
    if token_path and Path(token_path).exists() and not reauth:
        try:
            with open(token_path, "rb") as f:
                creds = pickle.load(f)
        except Exception as e:  # noqa: BLE001  손상된 pickle
            print(f"[AUTH] 토큰 파일을 읽지 못했습니다({type(e).__name__}). 다시 승인합니다.")
            creds = None

    if creds is not None and not getattr(creds, "valid", False):
        if getattr(creds, "refresh_token", None):
            from google.auth.transport.requests import Request

            try:
                creds.refresh(Request())
            except Exception as e:  # noqa: BLE001  invalid_grant: revoked/expired
                print(f"[AUTH] 토큰 자동 갱신 실패: {type(e).__name__}: {e}")
                print("[AUTH] 토큰이 만료/폐기되었습니다 → 브라우저에서 다시 승인합니다.")
                creds = None
        else:
            creds = None

    if creds is None or not getattr(creds, "valid", False):
        if not Path(client_secret).exists():
            raise FileNotFoundError(
                f"client_secret.json 없음: {client_secret} — Google Cloud Console에서 OAuth 클라이언트를 만들고 배치하세요."
            )
        print("[AUTH] 구글 계정 승인 창을 엽니다. 브라우저에서 '허용'을 누르면 자동으로 이어집니다.")
        print("[AUTH] (브라우저가 안 열리면 콘솔에 출력되는 주소를 직접 여세요)")
        flow = InstalledAppFlow.from_client_secrets_file(client_secret, scopes)
        # access_type=offline + prompt=consent → refresh_token 을 확실히 다시 받는다
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
        if token_path:
            Path(token_path).parent.mkdir(parents=True, exist_ok=True)
            with open(token_path, "wb") as f:
                pickle.dump(creds, f)
            print(f"[AUTH] 새 토큰 저장: {token_path}")
    write_auth_state(True, "인증 성공")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(video: Path, title: str, description: str, privacy: str, dry_run: bool,
           tags: list[str] | None = None) -> int:
    published_log = OUTPUT_DIR / "_published.json"
    published = {}
    if published_log.exists():
        try:
            published = json.loads(published_log.read_text(encoding="utf-8"))
        except Exception:
            published = {}
    key = str(video)
    if key in published:
        print(f"[SKIP] 이미 업로드한 파일입니다: {key}")
        print(f"        https://youtu.be/{published[key].get('youtube_id', '')}")
        return 0

    if dry_run:
        print(f"[DRY] 업로드 예정: {video}")
        print(f"      제목: {title}")
        print(f"      태그: {', '.join(tags or [])}")
        print(f"      공개설정: {privacy}")
        print("      설명문구:")
        for line in (description or "").splitlines():
            print(f"        {line}")
        return 0

    from googleapiclient.http import MediaFileUpload

    youtube = get_youtube_service(load_config())
    body = {
        "snippet": {"title": title, "description": description,
                    "tags": (tags or ["shorts", "ai", "raccoon"])[:30], "categoryId": "28"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=-1, resumable=True)
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    res = req.execute()

    published[key] = {"youtube_id": res["id"], "uploaded_at": datetime.now().isoformat(timespec="seconds"),
                      "privacy": privacy}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    published_log.write_text(json.dumps(published, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 업로드 완료: https://youtu.be/{res['id']} ({privacy})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="YouTube Shorts 업로드")
    ap.add_argument("--video", default=None)
    ap.add_argument("--latest", action="store_true", help="output/final 최신 MP4 사용")
    ap.add_argument("--title", default=None)
    ap.add_argument("--description", default=None)
    ap.add_argument("--privacy", default=None, choices=["private", "unlisted", "public"])
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--yes", action="store_true", help="확인 프롬프트 생략")
    ap.add_argument("--meta-refresh", action="store_true",
                    help="설명문구/제목/썸네일 추천을 LLM 으로 다시 생성")
    ap.add_argument("--reauth", action="store_true",
                    help="저장된 OAuth 토큰을 버리고 브라우저 승인을 다시 받는다")
    ap.add_argument("--auth-check", action="store_true",
                    help="업로드 없이 인증만 확인/갱신하고 종료")
    args = ap.parse_args()

    config = load_config()
    yt_cfg = config.get("youtube", {}) or {}
    final_dir = Path(expand((config.get("paths", {}) or {}).get("final_output", str(OUTPUT_DIR / "final"))))

    # 인증만 확인/재승인 (영상이 없어도 실행 가능)
    if args.reauth or args.auth_check:
        try:
            get_youtube_service(config, reauth=args.reauth)
        except Exception as e:  # noqa: BLE001
            write_auth_state(False, f"{type(e).__name__}: {e}")
            print(f"[ERROR] 유튜브 인증 실패: {type(e).__name__}: {e}")
            return 1
        print("[OK] 유튜브 인증 준비 완료 (token.pickle 갱신)")
        if args.auth_check:
            return 0

    video: Path | None = None
    if args.video:
        video = Path(expand(args.video))
    elif args.latest:
        video = latest_final_video(final_dir)
    if not video or not video.exists():
        print(f"[ERROR] 업로드할 MP4가 없습니다. (--video 또는 --latest) 확인: {final_dir}")
        return 1

    # 제목/설명/태그: output/youtube_meta.json (없으면 생성). 대본을 그대로 붙이지 않는다.
    try:
        if args.meta_refresh:
            youtube_meta.generate(config)
        meta_payload = youtube_meta.publish_payload(config)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 유튜브 문구 생성 실패: {type(e).__name__}: {e}")
        return 1

    title = args.title or meta_payload["title"]
    description = args.description or meta_payload["description"]
    tags = meta_payload.get("tags") or []

    privacy = args.privacy or yt_cfg.get("default_privacy", "private")

    print(f"[업로드] {video.name} ({video.stat().st_size/1048576:.1f}MB)")
    print(f"         제목: {title}")
    print(f"         문구: {meta_payload.get('source')} · {len(description)}자 · 태그 {len(tags)}개")
    print(f"         공개: {privacy}{' / DRY-RUN' if args.dry else ''}")

    if not args.dry and not args.yes:
        ans = input("이대로 업로드할까요? (y/n): ").strip().lower()
        if ans != "y":
            print("[CANCEL] 업로드를 취소했습니다.")
            return 0

    try:
        return upload(video, title, description, privacy, args.dry, tags)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        # 토큰 만료/폐기 → 웹 배지에 '재연결 필요'로 표시되게 기록
        if "invalid_grant" in str(e) or "RefreshError" in type(e).__name__:
            write_auth_state(False, msg)
            print("[HINT] 웹 화면의 '🔑 유튜브 다시 연결' 버튼을 누르거나,"
                  " python scripts_publish_youtube.py --reauth --auth-check 를 실행하세요.")
        print(f"[ERROR] 업로드 실패: {msg}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
