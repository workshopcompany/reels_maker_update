"""
[단계 2] 트렌드 수집 + 후보 선택 (투명성 강화판)
==================================================
무엇을, 어떤 도구로, 몇 개 모았고, 그중 무엇을 왜 골랐는지를 전부 파일로 남긴다.

수집 도구
- YouTube 쇼츠: yt-dlp (검색 키워드: config.trending.search_keywords + 채널 분석 키워드)
- GitHub: GitHub Search API (최근 생성 + 스타 순)
- HuggingFace: /api/models (trendingScore 정렬, 실패 시 downloads 순)

산출물
- output/trends.json        : 후보 전체 + 점수 근거 + 제외 사유 + 최종 선택 + 이유
- output/trends_report.md   : 사람이 읽는 리포트 (도구/쿼리/표/선택 근거)

사용 예
  python scripts_fetch_trends.py                  # 자동 선택
  python scripts_fetch_trends.py --select 3       # 3순위 후보를 수동 선택
  python scripts_fetch_trends.py --no-github --no-hf
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
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

from dotenv import load_dotenv

load_dotenv(PROJECT / ".env", override=False)


def expand(path: str) -> str:
    return os.path.normpath(os.path.expandvars(path))


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def now() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------
# 공통 점수 계산
# ----------------------------------------------------------------------
def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def popularity_score(value: int, scale: int) -> float:
    """log10 스케일 0~1"""
    if not value or value <= 0:
        return 0.0
    return clamp(math.log10(value) / math.log10(scale))


def recency_score(days_old: float | None, horizon_days: int) -> float:
    if days_old is None:
        return 0.3
    return clamp(1.0 - (days_old / horizon_days))


def tokenize(text: str) -> set[str]:
    return {
        t.lower()
        for t in re.findall(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9+.#-]{2,}", text or "")
    }


def keyword_fit(title: str, channel_keywords: list[str]) -> tuple[float, list[str]]:
    if not channel_keywords:
        return 0.0, []
    title_tokens = tokenize(title)
    hits = [k for k in channel_keywords if k.lower() in title_tokens]
    return clamp(len(hits) / min(4, len(channel_keywords))), hits


# ----------------------------------------------------------------------
# 1) YouTube 쇼츠
#    - 1순위: YouTube Data API search (videoDuration=short, order=viewCount, 최근 N일)
#    - 2순위: yt-dlp 검색 (API 토큰이 없을 때)
# ----------------------------------------------------------------------
def _video_item(vid: str, snippet: dict, stats: dict, content: dict, query: str, min_views: int, max_duration: int) -> dict:
    from_date = (snippet.get("publishedAt") or "")[:10]
    return {
        "source": "youtube",
        "id": vid,
        "title": snippet.get("title", ""),
        "url": f"https://www.youtube.com/watch?v={vid}",
        "author": snippet.get("channelTitle", ""),
        "views": int((stats or {}).get("viewCount", 0) or 0),
        "likes": int((stats or {}).get("likeCount", 0) or 0),
        "duration_sec": parse_iso_duration((content or {}).get("duration", "")),
        "published": from_date,
        "description": (snippet.get("description") or "")[:1200],
        "tags": (snippet.get("tags") or [])[:10],
        "thumbnail": pick_thumb(snippet),
        "query": query,
        "enriched": True,
        "raw_metrics": {"views": int((stats or {}).get("viewCount", 0) or 0),
                        "min_views": min_views, "max_duration_sec": max_duration},
    }


def parse_iso_duration(value: str) -> int:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def pick_thumb(snippet: dict) -> str:
    thumbs = (snippet or {}).get("thumbnails", {}) or {}
    for key in ("maxres", "standard", "high", "medium", "default"):
        if key in thumbs:
            return thumbs[key].get("url", "")
    return ""


def _youtube_credentials(config: dict):
    try:
        import pickle

        path = (config.get("youtube", {}) or {}).get("token_path") or os.getenv("YOUTUBE_TOKEN_PICKLE")
        if not path:
            return None
        path = expand(path)
        if not Path(path).exists():
            return None
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def collect_youtube_api(config: dict, queries: list[str], errors: list[str]) -> tuple[list[dict], str]:
    creds = _youtube_credentials(config)
    if not creds:
        return [], "OAuth 토큰 없음"
    try:
        from googleapiclient.discovery import build

        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        return [], f"YouTube API 초기화 실패: {e}"

    tr = config.get("trending", {}) or {}
    per_query = int(tr.get("max_results", 10))
    min_views = int(tr.get("min_views", 3000))
    max_duration = int(tr.get("max_duration_sec", 180))
    window_days = int(tr.get("window_days", 21))
    published_after = (now() - timedelta(days=window_days)).strftime("%Y-%m-%dT00:00:00Z")

    print(f"[YouTube] Data API 검색: 쿼리 {len(queries)}개 × {per_query}개 (최근 {window_days}일, Shorts, 조회수순)")
    results: list[dict] = []
    for q in queries:
        try:
            resp = yt.search().list(
                part="snippet", q=q, type="video", videoDuration="short",
                order="viewCount", publishedAfter=published_after, maxResults=per_query,
                regionCode="KR", relevanceLanguage="ko",
            ).execute()
        except Exception as e:
            errors.append(f"YouTube 검색 실패({q}): {e}")
            print(f"[WARN] YouTube 검색 실패({q}): {e}")
            continue
        items = resp.get("items", [])
        ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
        if not ids:
            print(f"  - '{q}' → 0개")
            continue
        try:
            vresp = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(ids)).execute()
        except Exception as e:
            errors.append(f"YouTube 통계 실패({q}): {e}")
            vresp = {"items": []}
        got = 0
        for v in vresp.get("items", []):
            results.append(_video_item(v["id"], v.get("snippet", {}), v.get("statistics", {}),
                                       v.get("contentDetails", {}), q, min_views, max_duration))
            got += 1
        print(f"  - '{q}' → {got}개")
    return results, ""


def collect_youtube_ytdlp(config: dict, queries: list[str], errors: list[str]) -> list[dict]:
    try:
        import yt_dlp
    except Exception as e:
        errors.append(f"yt-dlp 미설치: {e}")
        return []

    tr = config.get("trending", {}) or {}
    per_query = int(tr.get("max_results", 10))
    min_views = int(tr.get("min_views", 3000))
    max_duration = int(tr.get("max_duration_sec", 180))

    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": True, "ignoreerrors": True, "noplaylist": True}
    out: list[dict] = []
    print(f"[YouTube] yt-dlp 검색 (쿼리당 {per_query}개)")
    for q in queries:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{per_query}:{q} #shorts", download=False)
            entries = [e for e in (info.get("entries") or []) if e]
        except Exception as e:
            errors.append(f"yt-dlp 검색 실패({q}): {e}")
            print(f"[WARN] yt-dlp 검색 실패({q}): {e}")
            continue
        got = 0
        for e in entries:
            vid = e.get("id")
            if not vid:
                continue
            if not e.get("view_count") or (e.get("duration") or 0) > max_duration:
                continue  # 상세조회 비용을 줄이기 위해 후보만 남깁니다
            try:
                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
                    full = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False) or {}
            except Exception:
                continue
            d = full.get("upload_date") or ""
            out.append({
                "source": "youtube", "id": vid,
                "title": full.get("title", ""), "url": f"https://www.youtube.com/watch?v={vid}",
                "author": full.get("channel", ""),
                "views": int(full.get("view_count") or 0),
                "likes": int(full.get("like_count") or 0),
                "duration_sec": int(full.get("duration") or 0),
                "published": f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d else "",
                "description": (full.get("description") or "")[:1200],
                "tags": (full.get("tags") or [])[:10],
                "thumbnail": (full.get("thumbnails") or [{}])[-1].get("url", ""),
                "query": q, "enriched": True,
                "raw_metrics": {"views": int(full.get("view_count") or 0),
                                "min_views": min_views, "max_duration_sec": max_duration},
            })
            got += 1
        print(f"  - '{q}' → {got}개")
    return out


def collect_youtube(config: dict, queries: list[str], errors: list[str]) -> tuple[list[dict], list[dict], str]:
    """returns (kept, excluded, tool_name)"""
    tr = config.get("trending", {}) or {}
    min_views = int(tr.get("min_views", 3000))
    max_duration = int(tr.get("max_duration_sec", 180))

    items, api_error = collect_youtube_api(config, queries, errors)
    tool = "YouTube Data API"
    if api_error:
        print(f"[INFO] {api_error} → yt-dlp 폴백")
        items = collect_youtube_ytdlp(config, queries, errors)
        tool = "yt-dlp (폴백)"

    kept, excluded, seen = [], [], set()
    for c in items:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        if c["duration_sec"] and c["duration_sec"] > max_duration:
            excluded.append({**c, "exclude_reason": f"쇼츠 길이 초과({c['duration_sec']}초 > {max_duration}초)"})
        elif c["views"] < min_views:
            excluded.append({**c, "exclude_reason": f"조회수 미달({c['views']:,} < {min_views:,})"})
        else:
            kept.append(c)
    return kept, excluded, tool


# ----------------------------------------------------------------------
# 2) GitHub 트렌드
# ----------------------------------------------------------------------
def collect_github(config: dict, errors: list[str]) -> list[dict]:
    import urllib.parse
    import urllib.request

    days = 21
    since = (now() - timedelta(days=days)).strftime("%Y-%m-%d")
    q = f"AI in:name,description,readme created:>{since} stars:>50"
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
        {"q": q, "sort": "stars", "order": "desc", "per_page": int((config.get("trending", {}) or {}).get("max_results", 10))}
    )
    headers = {"User-Agent": "raccoon-shorts", "Accept": "application/vnd.github+json"}
    if os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = f"token {os.getenv('GITHUB_TOKEN')}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        errors.append(f"GitHub API 실패: {e}")
        print(f"[WARN] GitHub API 실패: {e}")
        return []

    out = []
    for it in data.get("items", []):
        out.append(
            {
                "source": "github",
                "id": it["full_name"],
                "title": it["full_name"],
                "url": it["html_url"],
                "author": it["owner"]["login"],
                "views": 0,
                "stars": int(it.get("stargazers_count") or 0),
                "likes": 0,
                "duration_sec": 0,
                "published": (it.get("created_at") or "")[:10],
                "description": (it.get("description") or "")[:600],
                "thumbnail": "",
                "query": q,
                "raw_metrics": {"stars": it.get("stargazers_count"), "forks": it.get("forks_count"),
                                "created_at": it.get("created_at")},
            }
        )
    print(f"[GitHub] {len(out)}개 수집 (created:>{since}, stars:>50)")
    return out


# ----------------------------------------------------------------------
# 3) HuggingFace 트렌드
# ----------------------------------------------------------------------
def collect_huggingface(config: dict, errors: list[str]) -> list[dict]:
    import urllib.parse
    import urllib.request

    limit = int((config.get("trending", {}) or {}).get("max_results", 10))
    attempts = [
        ("trendingScore", {"sort": "trendingScore", "direction": -1, "limit": limit, "full": "true"}),
        ("downloads", {"sort": "downloads", "direction": -1, "limit": limit, "full": "true"}),
    ]
    used_sort = ""
    data = None
    for sort_key, params in attempts:
        url = "https://huggingface.co/api/models?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "raccoon-shorts"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            used_sort = sort_key
            break
        except Exception as e:
            errors.append(f"HuggingFace({sort_key}) 실패: {e}")
            print(f"[WARN] HuggingFace {sort_key} 실패: {e}")
    if not data:
        return []

    out = []
    for m in data:
        mid = m.get("modelId") or m.get("id") or ""
        out.append(
            {
                "source": "huggingface",
                "id": mid,
                "title": mid,
                "url": f"https://huggingface.co/{mid}",
                "author": mid.split("/")[0] if "/" in mid else "",
                "views": 0,
                "stars": 0,
                "likes": int(m.get("likes") or 0),
                "downloads": int(m.get("downloads") or 0),
                "duration_sec": 0,
                "published": (m.get("createdAt") or "")[:10],
                "description": (m.get("cardData", {}) or {}).get("short_description", "") or "",
                "thumbnail": "",
                "query": f"sort={used_sort}",
                "raw_metrics": {"downloads": m.get("downloads"), "likes": m.get("likes"),
                                "sort": used_sort, "createdAt": m.get("createdAt")},
            }
        )
    print(f"[HuggingFace] {len(out)}개 수집 (정렬: {used_sort})")
    return out


# ----------------------------------------------------------------------
# 점수화 + 선택
# ----------------------------------------------------------------------
# 출처별 가중치: 영상 소재(YouTube)가 대본 재료로 가장 쓸모 있으므로 비중이 높다.
WEIGHTS = {
    "youtube": {"popularity": 0.30, "recency": 0.15, "fit": 0.25, "novelty": 0.10, "content": 0.20},
    "github": {"popularity": 0.40, "recency": 0.25, "fit": 0.20, "novelty": 0.15, "content": 0.00},
    "huggingface": {"popularity": 0.40, "recency": 0.20, "fit": 0.20, "novelty": 0.20, "content": 0.00},
}
SOURCE_MULTIPLIER = {"youtube": 1.0, "github": 0.90, "huggingface": 0.85}
POPULARITY_SCALE = {"youtube": 5_000_000, "github": 20_000, "huggingface": 5_000_000}
RECENCY_HORIZON = {"youtube": 14, "github": 21, "huggingface": 180}
# 걸러낼 주제 (성인/유해 콘텐츠가 대본 키워드로 올라오는 것을 방지)
BLOCKLIST = ("uncensored", "nsfw", "porn", "xxx", "18+", "성인", "야동", "gore", "nude")


def days_since(date_str: str) -> float | None:
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
            return max(0.0, (now() - dt).total_seconds() / 86400)
        except ValueError:
            continue
    return None


def content_score(c: dict) -> float:
    """대본 재료로 쓸 수 있는 내용이 얼마나 있는지 (제목/설명/태그)."""
    desc_len = len(c.get("description") or "") + 20 * len(c.get("tags", []) or [])
    if c["source"] == "youtube":
        return round(0.4 + 0.6 * clamp(desc_len / 400), 3)
    if c["source"] == "github":
        return round(clamp(desc_len / 200) * 0.6, 3)
    return round(clamp(desc_len / 200) * 0.4, 3)


def is_blocked(c: dict) -> bool:
    text = f"{c.get('title','')} {c.get('description','')}".lower()
    return any(bad in text for bad in BLOCKLIST)


def score_candidates(candidates: list[dict], channel_keywords: list[str], covered: list[str]) -> None:
    covered_tokens = {c.lower() for c in covered}
    for c in candidates:
        src = c["source"]
        w = WEIGHTS[src]
        pop_raw = c.get("views") if src == "youtube" else (
            c.get("stars") if src == "github" else c.get("downloads")
        )
        pop = popularity_score(int(pop_raw or 0), POPULARITY_SCALE[src])
        d_old = days_since(c.get("published", ""))
        rec = recency_score(d_old, RECENCY_HORIZON[src])
        fit, hits = keyword_fit(f"{c.get('title','')} {c.get('description','')}", channel_keywords)
        novelty = 0.0 if (tokenize(c.get("title", "")) & covered_tokens) else 1.0
        content = content_score(c)

        base = (
            pop * w["popularity"]
            + rec * w["recency"]
            + fit * w["fit"]
            + novelty * w["novelty"]
            + content * w["content"]
        )
        score = base * SOURCE_MULTIPLIER[src] * 100
        c["score"] = round(score, 1)
        c["score_breakdown"] = {
            "popularity": round(pop, 3),
            "recency": round(rec, 3),
            "channel_fit": round(fit, 3),
            "novelty": round(novelty, 3),
            "content": content,
            "weights": w,
            "source_multiplier": SOURCE_MULTIPLIER[src],
            "raw_popularity": int(pop_raw or 0),
            "days_old": None if d_old is None else round(d_old, 1),
        }
        c["fit_keywords"] = hits


def describe_reason(c: dict) -> str:
    """선택 이유를 사람이 읽을 수 있는 문장으로."""
    b = c["score_breakdown"]
    src_label = {"youtube": "YouTube 쇼츠", "github": "GitHub", "huggingface": "HuggingFace"}[c["source"]]
    parts = []
    if c["source"] == "youtube":
        parts.append(f"조회수 {c['views']:,}회")
    elif c["source"] == "github":
        parts.append(f"스타 {c.get('stars', 0):,}개")
    else:
        parts.append(f"다운로드 {c.get('downloads', 0):,}회")
    if b["days_old"] is not None:
        parts.append(f"공개 {b['days_old']}일 전")
    if c["fit_keywords"]:
        parts.append(f"내 채널 키워드와 일치: {', '.join(c['fit_keywords'][:4])}")
    else:
        parts.append("내 채널 키워드와 직접 일치는 없음")
    if b["novelty"] == 1.0:
        parts.append("이미 다룬 주제와 겹치지 않음")
    else:
        parts.append("이미 다룬 주제와 일부 겹침")
    desc_len = len(c.get("description") or "")
    if c["source"] == "youtube":
        parts.append(f"대본 재료 확보(설명 {desc_len}자)" if desc_len else "설명 없음 → 제목만으로 대본 작성")
    return f"{src_label} 후보 중 점수 {c['score']}점 — " + ", ".join(parts)


def pick_keyword(c: dict) -> str:
    title = re.sub(r"#\S+", "", c.get("title", "")).strip()
    title = re.sub(r"\s+", " ", title)
    if len(title) > 80:
        title = title[:77].rstrip() + "..."
    return title or c.get("id", "AI 트렌드")


# ----------------------------------------------------------------------
# 리포트
# ----------------------------------------------------------------------
def write_report(payload: dict, path: Path) -> None:
    sel = payload["selected"]
    lines = [
        "# 🔎 트렌드 수집 리포트",
        "",
        f"- 수집 시각: {payload['collected_at']}  (소요 {payload['duration_sec']}초)",
        f"- 검색 쿼리: {', '.join(payload['queries'])}",
        f"- 후보 {len(payload['candidates'])}개 / 필터 제외 {len(payload['excluded'])}개",
        f"- 선택 방식: **{payload['selection_mode']}**",
        f"- 선택 풀: {payload.get('selection_pool', {}).get('label', '-')} "
        f"({payload.get('selection_pool', {}).get('size', 0)}개 / 전체 후보 {payload.get('selection_pool', {}).get('total_candidates', 0)}개)",
        "",
        "## 사용한 도구",
        "",
        "| 도구 | 상태 | 수집 수 | 비고 |",
        "|---|---|---|---|",
    ]
    for t in payload["tools"]:
        lines.append(f"| {t['name']} | {t['status']} | {t['count']} | {t.get('error', '')[:120]} |")

    lines += ["", "## 선택 결과", "", f"**{sel['title']}**  ", f"- 키워드: `{sel['keyword']}`  ",
              f"- 출처: {sel['source']} / {sel['url']}  ", f"- 점수: {sel['score']}  ",
              f"- 이유: {sel['reason']}  "]
    if sel.get("content_excerpt"):
        lines += ["", "### 핫한 영상 내용 발췌", "", "```", sel["content_excerpt"][:800], "```"]

    lines += ["", "## 후보 전체 (점수 순)", "",
              "| # | 출처 | 제목 | 조회/스타 | 점수 | 채널 적합 |", "|---|---|---|---|---|---|"]
    for i, c in enumerate(payload["candidates"], 1):
        metric = c.get("views") or c.get("stars") or c.get("downloads") or 0
        fit = ", ".join(c.get("fit_keywords", [])) or "-"
        lines.append(f"| {i} | {c['source']} | [{c['title'][:60]}]({c['url']}) | {metric:,} | {c['score']} | {fit} |")

    if payload["excluded"]:
        lines += ["", "## 필터에서 제외된 후보", "", "| 제목 | 사유 |", "|---|---|"]
        for c in payload["excluded"][:30]:
            lines.append(f"| {c['title'][:50]} | {c['exclude_reason']} |")

    lines += ["", "## 점수 계산 방식", "",
              "`score = (인기도×w1 + 최신성×w2 + 채널적합도×w3 + 새로움×w4) × 100`  ",
              "인기도=log10(조회수 또는 스타/다운로드)/log10(기준값), 최신성=1-경과일/기준일, "
              "채널적합도=제목·설명에서 내 채널 키워드가 몇 개 겹치는지, 새로움=이미 다룬 주제면 0.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="트렌드 수집 + 후보 선택")
    ap.add_argument("--keyword", action="append", default=[], help="추가 검색 키워드 (반복 가능)")
    ap.add_argument("--select", type=int, default=0, help="N순위 후보를 수동 선택 (1부터)")
    ap.add_argument("--no-github", action="store_true")
    ap.add_argument("--no-hf", action="store_true")
    ap.add_argument("--no-youtube", action="store_true")
    ap.add_argument("--any-source", action="store_true", help="YouTube 후보가 있어도 모든 출처에서 선택")
    ap.add_argument("--min-views", type=int, default=None, help="YouTube 최소 조회수 (기본 config.trending.min_views)")
    ap.add_argument("--max-duration", type=int, default=None, help="최대 영상 길이(초)")
    args = ap.parse_args()

    started = now()
    config = load_config()
    if args.min_views is not None:
        config.setdefault("trending", {})["min_views"] = max(0, args.min_views)
    if args.max_duration is not None:
        config.setdefault("trending", {})["max_duration_sec"] = max(5, args.max_duration)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tr = config.get("trending", {}) or {}

    # 채널 분석 결과 (있으면 키워드/중복 필터에 사용)
    channel_keywords: list[str] = []
    covered: list[str] = []
    channel_title = ""
    channel_file = OUTPUT_DIR / "channel_analysis.json"
    if channel_file.exists():
        try:
            ca = json.loads(channel_file.read_text(encoding="utf-8"))
            channel_keywords = [k["keyword"] for k in ca.get("top_keywords", [])]
            covered = ca.get("covered_topics", []) or []
            channel_title = (ca.get("channel") or {}).get("title", "")
        except Exception:
            pass

    # 검색 쿼리는 config의 니치 키워드만 사용한다.
    # (채널 키워드를 그대로 검색어로 쓰면 '무료', 'vibe' 같은 넓은 단어가 노이즈를 만든다.
    #  채널 키워드는 '적합도 점수'와 '중복 필터'에만 사용)
    queries = [q for q in (tr.get("search_keywords") or ["쇼츠"]) if q]
    queries += args.keyword
    # 중복 제거
    queries = list(dict.fromkeys(queries))

    print("=" * 64)
    print(f"[트렌드] 채널 컨텍스트: {channel_title or '(채널 분석 없음)'} / 키워드 {len(channel_keywords)}개")
    print(f"[트렌드] 검색 쿼리: {', '.join(queries)}")
    print("=" * 64)

    errors: list[str] = []
    tools: list[dict] = []
    candidates: list[dict] = []
    excluded: list[dict] = []

    if not args.no_youtube:
        before = len(errors)
        kept, excluded_yt, tool_name = collect_youtube(config, queries, errors)
        excluded += excluded_yt
        candidates += kept
        tools.append({"name": tool_name, "status": "ok" if kept else "error",
                      "count": len(kept), "error": "; ".join(errors[before:])})

    if tr.get("include_github", True) and not args.no_github:
        before = len(errors)
        gh = collect_github(config, errors)
        candidates += gh
        tools.append({"name": "GitHub Search API", "status": "ok" if gh else "error",
                      "count": len(gh), "error": "; ".join(errors[before:])})

    if tr.get("include_huggingface", True) and not args.no_hf:
        before = len(errors)
        hf = collect_huggingface(config, errors)
        candidates += hf
        tools.append({"name": "HuggingFace API", "status": "ok" if hf else "error",
                      "count": len(hf), "error": "; ".join(errors[before:])})

    # 유해 주제 제거 (선택 단계 전에)
    safe, blocked = [], []
    for c in candidates:
        if is_blocked(c):
            blocked.append({**c, "exclude_reason": "금지 주제 키워드 포함"})
        else:
            safe.append(c)
    if blocked:
        print(f"[필터] 금지 주제 {len(blocked)}개 제외")
    candidates = safe
    excluded += blocked

    if not candidates:
        print("[ERROR] 모든 소스에서 후보를 수집하지 못했습니다.")
        for e in errors:
            print(f"  - {e}")
        return 1

    score_candidates(candidates, channel_keywords, covered)
    candidates.sort(key=lambda c: c["score"], reverse=True)

    # 선택 풀 우선순위
    #  1) 내 채널 키워드와 하나라도 겹치는 YouTube 영상 ("관련 트렌드")
    #  2) 그래도 없으면 모든 YouTube 영상
    #  3) YouTube 후보가 아예 없으면 전체 출처
    video_pool = [c for c in candidates if c["source"] == "youtube"]
    relevant = [c for c in video_pool if c.get("fit_keywords")]
    if args.any_source:
        pool, pool_label = candidates, "전체 출처 후보(강제)"
    elif relevant:
        pool, pool_label = relevant, "내 채널 키워드와 겹치는 YouTube 영상"
    elif video_pool:
        pool, pool_label = video_pool, "YouTube 영상 후보(채널 일치 없음 → 인기/최신 위주)"
    else:
        pool, pool_label = candidates, "전체 출처 후보"

    if args.select:
        idx = max(1, min(args.select, len(pool))) - 1
        selected = pool[idx]
        selection_mode = f"manual (사용자가 {pool_label}에서 {idx + 1}순위 지정)"
    else:
        selected = pool[0]
        selection_mode = f"auto ({pool_label} 중 최고 점수)"

    selected = dict(selected)
    selected["keyword"] = pick_keyword(selected)
    selected["reason"] = describe_reason(selected)
    selected["content_excerpt"] = (selected.get("description") or "").strip()

    payload = {
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "duration_sec": round((now() - started).total_seconds(), 1),
        "queries": queries,
        "channel_context": {"channel": channel_title, "keywords": channel_keywords, "covered_topics": covered},
        "tools": tools,
        "errors": errors,
        "candidates": candidates,
        "excluded": excluded,
        "selected": selected,
        "selected_keyword": selected["keyword"],
        "selection_mode": selection_mode,
        "selection_pool": {"label": pool_label, "size": len(pool), "total_candidates": len(candidates)},
        "runner_ups": [{"rank": i + 2, "title": c["title"], "score": c["score"], "source": c["source"], "url": c["url"]}
                       for i, c in enumerate(pool[1:6])],
    }
    # 편의: 선택된 후보를 후보 목록에서 식별할 수 있게 id 기록
    payload["selected_id"] = selected.get("id", "")

    (OUTPUT_DIR / "trends.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(payload, OUTPUT_DIR / "trends_report.md")

    print("")
    print(f"[선택] {selected['title']}")
    print(f"       점수 {selected['score']} / 출처 {selected['source']} / {selected['url']}")
    print(f"       이유: {selected['reason']}")
    print(f"[키워드] {selected['keyword']}")
    print(f"[OK] 저장: output/trends.json, output/trends_report.md (후보 {len(candidates)}개, 제외 {len(excluded)}개)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
