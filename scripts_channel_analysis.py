"""
[단계 1] 내 유튜브 채널 분석
================================
- 1순위: YouTube Data API v3 (OAuth token, channels.list(mine=True) + 업로드 목록 + 영상 통계)
- 2순위: yt-dlp (config.channel.channel_url 이 있을 때)
결과: output/channel_analysis.json, output/channel_analysis.md
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
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

from dotenv import load_dotenv

load_dotenv(PROJECT / ".env", override=False)

STOPWORDS = {
    # 한국어
    "그리고", "하지만", "그래서", "이거", "저거", "것은", "있는", "없는", "합니다", "해요", "하는",
    "영상", "진짜", "정말", "너무", "다시", "이제", "우리", "여러분", "오늘", "내일", "어떻게",
    "왜", "이런", "그런", "저런", "때문에", "대한", "위한", "에서", "으로", "하고", "까지",
    "등장", "이유", "두번째", "이야기", "번째", "그것", "이걸", "저걸", "정도", "경우",
    "사실", "부분", "생각", "사람", "때문", "그냥", "다들", "여기", "거의", "하는데",
    # 영어
    "the", "and", "for", "with", "this", "that", "you", "your", "how", "what", "why", "new",
    "shorts", "video", "youtube", "ai", "vs", "ep", "part", "full", "best", "top",
}


def expand(path: str) -> str:
    return os.path.normpath(os.path.expandvars(path))


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def parse_iso_duration(value: str) -> int:
    """PT1M30S -> 90"""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9+.#-]{2,}", text or "")
    out = []
    for t in tokens:
        low = t.lower()
        if low in STOPWORDS or len(low) < 2:
            continue
        out.append(low)
    return out


def pick_thumbnail(snippet: dict) -> str:
    thumbs = (snippet or {}).get("thumbnails", {}) or {}
    for key in ("maxres", "standard", "high", "medium", "default"):
        if key in thumbs:
            return thumbs[key].get("url", "")
    return ""


# ----------------------------------------------------------------------
# 1) YouTube Data API
# ----------------------------------------------------------------------
def get_credentials(config: dict):
    token_path = (config.get("youtube", {}) or {}).get("token_path") or os.getenv("YOUTUBE_TOKEN_PICKLE")
    if not token_path:
        return None, "토큰 경로 미설정"
    token_path = expand(token_path)
    if not Path(token_path).exists():
        return None, f"OAuth 토큰 없음: {token_path}"
    try:
        import pickle

        with open(token_path, "rb") as f:
            creds = pickle.load(f)
        return creds, ""
    except Exception as e:
        return None, f"토큰 읽기 실패: {e}"


def collect_via_api(config: dict) -> dict | None:
    creds, err = get_credentials(config)
    if not creds:
        print(f"[INFO] YouTube API 사용 불가: {err}")
        return None

    try:
        from googleapiclient.discovery import build

        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        resp = yt.channels().list(part="snippet,statistics,contentDetails", mine=True).execute()
        items = resp.get("items", [])
        if not items:
            print("[WARN] OAuth 계정에 채널이 없습니다.")
            return None
        ch = items[0]
        uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
        max_videos = int((config.get("channel", {}) or {}).get("max_videos", 25))

        video_ids: list[str] = []
        page_token = None
        while len(video_ids) < max_videos:
            pl = yt.playlistItems().list(
                part="contentDetails", playlistId=uploads,
                maxResults=min(50, max_videos - len(video_ids)), pageToken=page_token,
            ).execute()
            for it in pl.get("items", []):
                video_ids.append(it["contentDetails"]["videoId"])
            page_token = pl.get("nextPageToken")
            if not page_token:
                break

        videos = []
        for i in range(0, len(video_ids), 50):
            chunk = video_ids[i:i + 50]
            vr = yt.videos().list(
                part="snippet,statistics,contentDetails", id=",".join(chunk)
            ).execute()
            for v in vr.get("items", []):
                sn = v.get("snippet", {})
                stats = v.get("statistics", {})
                cd = v.get("contentDetails", {})
                videos.append(
                    {
                        "id": v["id"],
                        "title": sn.get("title", ""),
                        "published_at": sn.get("publishedAt", ""),
                        "duration_sec": parse_iso_duration(cd.get("duration", "")),
                        "views": int(stats.get("viewCount", 0) or 0),
                        "likes": int(stats.get("likeCount", 0) or 0),
                        "comments": int(stats.get("commentCount", 0) or 0),
                        "tags": sn.get("tags", []) or [],
                        "thumbnail": pick_thumbnail(sn),
                        "url": f"https://www.youtube.com/watch?v={v['id']}",
                    }
                )
        return {
            "source": "youtube_data_api",
            "channel": {
                "id": ch["id"],
                "title": ch["snippet"]["title"],
                "description": ch["snippet"].get("description", ""),
                "subscribers": int(ch["statistics"].get("subscriberCount", 0) or 0),
                "total_views": int(ch["statistics"].get("viewCount", 0) or 0),
                "video_count": int(ch["statistics"].get("videoCount", 0) or 0),
                "url": f"https://www.youtube.com/channel/{ch['id']}",
            },
            "videos": videos,
        }
    except Exception as e:
        print(f"[WARN] YouTube API 수집 실패: {type(e).__name__}: {e}")
        return None


# ----------------------------------------------------------------------
# 2) yt-dlp 폴백 (채널 URL 필요)
# ----------------------------------------------------------------------
def collect_via_ytdlp(config: dict) -> dict | None:
    url = ((config.get("channel", {}) or {}).get("channel_url") or "").strip()
    if not url:
        print("[INFO] config.channel.channel_url 미설정 → yt-dlp 폴백 건너뜀")
        return None
    try:
        import yt_dlp

        max_videos = int((config.get("channel", {}) or {}).get("max_videos", 25))
        base = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True,
                "playlistend": max_videos, "ignoreerrors": True}
        with yt_dlp.YoutubeDL(base) as ydl:
            info = ydl.extract_info(url.rstrip("/") + "/videos", download=False)
        entries = [e for e in (info.get("entries") or []) if e]
        videos = [
            {
                "id": e.get("id", ""),
                "title": e.get("title", ""),
                "published_at": "",
                "duration_sec": int(e.get("duration") or 0),
                "views": int(e.get("view_count") or 0),
                "likes": 0,
                "comments": 0,
                "tags": [],
                "thumbnail": (e.get("thumbnails") or [{}])[-1].get("url", ""),
                "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id','')}",
            }
            for e in entries
        ]
        if not videos:
            return None
        return {
            "source": "yt_dlp",
            "channel": {
                "id": info.get("channel_id", ""),
                "title": info.get("channel") or info.get("title") or "",
                "description": info.get("description", "") or "",
                "subscribers": int(info.get("channel_follower_count") or 0),
                "total_views": 0,
                "video_count": len(videos),
                "url": url,
            },
            "videos": videos,
        }
    except Exception as e:
        print(f"[WARN] yt-dlp 채널 분석 실패: {e}")
        return None


# ----------------------------------------------------------------------
# 분석 요약
# ----------------------------------------------------------------------
def summarize(payload: dict, config: dict) -> dict:
    videos = payload.get("videos", [])
    ch = payload.get("channel", {})
    lookup_days = int((config.get("channel", {}) or {}).get("lookback_days", 180))
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookup_days)

    recent = []
    for v in videos:
        published = v.get("published_at") or ""
        dt = None
        if published:
            try:
                dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                dt = None
        if dt and dt < cutoff:
            continue
        recent.append({**v, "_dt": dt})

    # 업로드 주기
    dates = sorted([v["_dt"] for v in recent if v.get("_dt")])
    cadence_days = None
    if len(dates) >= 2:
        gaps = [(b - a).total_seconds() / 86400 for a, b in zip(dates, dates[1:])]
        cadence_days = round(sum(gaps) / len(gaps), 1)

    shorts = [v for v in recent if 0 < v.get("duration_sec", 0) <= 60]
    longform = [v for v in recent if v.get("duration_sec", 0) > 60]

    keyword_counter: Counter[str] = Counter()
    for v in recent:
        keyword_counter.update(tokenize(v.get("title", "")))
        for tag in v.get("tags", []) or []:
            keyword_counter.update(tokenize(tag))

    top_n = int((config.get("channel", {}) or {}).get("top_keywords", 12))
    # 채널 이름/설명 토큰도 주제 신호로 포함 (예: "Vibe Coding in my Workshop" -> vibe/coding/workshop)
    channel_tokens = tokenize(f"{ch.get('title', '')} {ch.get('description', '')}")
    if keyword_counter:
        boost = max(keyword_counter.values())
        for t in channel_tokens:
            keyword_counter[t] = max(keyword_counter.get(t, 0), boost)
    else:
        for t in channel_tokens:
            keyword_counter[t] += 1
    top_keywords = [{"keyword": k, "count": c} for k, c in keyword_counter.most_common(top_n)]

    top_videos = sorted(recent, key=lambda v: v.get("views", 0), reverse=True)[:10]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": payload.get("source"),
        "channel": ch,
        "stats": {
            "analyzed_videos": len(recent),
            "shorts_count": len(shorts),
            "longform_count": len(longform),
            "avg_duration_sec": round(sum(v.get("duration_sec", 0) for v in recent) / len(recent), 1) if recent else 0,
            "avg_views": round(sum(v.get("views", 0) for v in recent) / len(recent)) if recent else 0,
            "upload_cadence_days": cadence_days,
            "best_upload_weekday": Counter(
                v["_dt"].strftime("%A") for v in recent if v.get("_dt")
            ).most_common(1)[0][0] if dates else "",
            "lookback_days": lookup_days,
        },
        "top_keywords": top_keywords,
        "top_videos": [{k: v for k, v in item.items() if k != "_dt"} for item in top_videos],
        "recent_videos": [{k: v for k, v in item.items() if k != "_dt"} for item in recent],
        # 다음 단계(트렌드 중복 필터)에서 사용
        "covered_topics": [k["keyword"] for k in top_keywords],
    }


def write_markdown(analysis: dict, out: Path) -> None:
    ch = analysis["channel"]
    st = analysis["stats"]
    lines = [
        f"# 📊 채널 분석 리포트 — {ch.get('title', '(이름 없음)')}",
        "",
        f"- 생성: {analysis['generated_at']}  /  수집 방법: `{analysis['source']}`",
        f"- 채널: {ch.get('url', '')}",
        f"- 구독자: {ch.get('subscribers', 0):,}  |  총 조회수: {ch.get('total_views', 0):,}  |  영상 수: {ch.get('video_count', 0):,}",
        f"- 분석 기간: 최근 {st['lookback_days']}일 · 분석 영상 {st['analyzed_videos']}개 (쇼츠 {st['shorts_count']} / 롱폼 {st['longform_count']})",
        f"- 평균 길이: {st['avg_duration_sec']}초  |  평균 조회수: {st['avg_views']:,}  |  업로드 간격: {st['upload_cadence_days']}일  |  최다 업로드 요일: {st['best_upload_weekday'] or '정보 없음'}",
        "",
        "## 자주 쓰는 주제 키워드",
        "",
        "| 키워드 | 등장 횟수 |",
        "|---|---|",
    ]
    for k in analysis["top_keywords"]:
        lines.append(f"| {k['keyword']} | {k['count']} |")
    lines += ["", "## 조회수 상위 영상", "", "| 제목 | 조회수 | 좋아요 | 길이 | 게시일 |", "|---|---|---|---|---|"]
    for v in analysis["top_videos"]:
        lines.append(
            f"| [{v['title']}]({v['url']}) | {v['views']:,} | {v['likes']:,} | {v['duration_sec']}초 | {v['published_at'][:10]} |"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    config = load_config()
    out_dir = PROJECT / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 64)
    print("[1/1] 내 유튜브 채널 분석")
    print("=" * 64)

    payload = collect_via_api(config)
    if not payload:
        payload = collect_via_ytdlp(config)
    if not payload or not payload.get("videos"):
        print("[ERROR] 채널 데이터를 가져오지 못했습니다.")
        print("  - scripts/.secrets/youtube/token.pickle (OAuth) 또는")
        print("  - config.json channel.channel_url (yt-dlp 폴백) 을 확인하세요.")
        return 1

    analysis = summarize(payload, config)
    (out_dir / "channel_analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_markdown(analysis, out_dir / "channel_analysis.md")

    ch = analysis["channel"]
    st = analysis["stats"]
    print(f"[OK] 채널: {ch.get('title')} (구독자 {ch.get('subscribers', 0):,}, 영상 {st['analyzed_videos']}개 분석)")
    print(f"     업로드 간격 {st['upload_cadence_days']}일 / 평균 조회수 {st['avg_views']:,} / 쇼츠 {st['shorts_count']}개")
    print(f"     주요 키워드: {', '.join(k['keyword'] for k in analysis['top_keywords'][:8])}")
    print(f"[OK] 저장: {out_dir / 'channel_analysis.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
