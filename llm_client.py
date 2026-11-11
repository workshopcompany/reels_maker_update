"""
LLM 백엔드 클라이언트 (대본 생성용)
=====================================
프로바이더를 골라서 쓴다. 기본은 API(Gemini/Groq/OpenRouter/Freebuff)이고
Ollama 는 `config.llm.ollama_enabled=true` 일 때만 사용한다.

키 조회 순서: .env(환경변수) → config.llm.external_keys_file(기본 .env)
키 값은 절대 로그/응답에 출력하지 않는다.

가짜 대본을 조용히 만들지 않는다. 모든 백엔드가 실패하면 호출자가 실패를 처리한다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

TEMPLATE_SCRIPT = (
    "[excited] {keyword} 이거 모르면 진짜 손해야!\n"
    "너구리가 30초로 정리해줬다.\n"
    "핵심만 말하면, 지금 바로 써볼 수 있고 돈도 안 들어.\n"
    "따라 하면 오늘 안에 결과 나온다니까?\n"
    "[giggles] 더 궁금하면 팔로우하고 다음 영상 보자!"
)

# 프로바이더 기본값 (config.llm.providers 에서 덮어쓸 수 있다)
PROVIDER_DEFAULTS: dict[str, dict] = {
    "freebuff": {
        "label": "Freebuff API (OpenAI 호환)",
        "style": "openai",
        "base_url": "https://freebuff.com/api/v1/paid",
        "model": "claude-haiku-5.5",
        "key_envs": ["OPENAI_API_KEY", "FREEBUFF_API_KEY"],
    },
    "gemini": {
        "label": "Google Gemini",
        "style": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-3.5-flash",
        "key_envs": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    },
    "groq": {
        "label": "Groq",
        "style": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
        "key_envs": ["GROQ_API_KEY"],
    },
    "openrouter": {
        "label": "OpenRouter",
        "style": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openrouter/free",
        "key_envs": ["OPENROUTER_API_KEY", "OUTROUTER_API_KEY"],
    },
    "ollama": {
        "label": "Ollama (로컬 · 기본 꺼짐)",
        "style": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen3.5-9b-q4-k-m:latest",
        "key_envs": [],
    },
}

# .env 같은 외부 설정에서 읽어도 되는 키 이름만 허용
EXTERNAL_KEY_ALLOWLIST = {
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "FREEBUFF_API_KEY",
    "OPENROUTER_API_KEY",
    "OUTROUTER_API_KEY",
}

AUTO_ORDER = ("freebuff", "gemini", "groq", "openrouter")

_EXTERNAL_KEY_CACHE: dict[str, dict] = {}


# ----------------------------------------------------------------------
# 설정/키 해석
# ----------------------------------------------------------------------
def _llm_cfg(config: dict) -> dict:
    return config.get("llm", {}) or {}


def provider_cfg(provider: str, config: dict) -> dict:
    """config.llm.providers[id] + 기본값 병합."""
    llm = _llm_cfg(config)
    raw = (llm.get("providers") or {}).get(provider) or {}
    base = dict(PROVIDER_DEFAULTS.get(provider, {}))
    # 예전 config 키 호환
    if provider == "ollama":
        base["base_url"] = llm.get("ollama_url") or base["base_url"]
        base["model"] = llm.get("ollama_model") or base["model"]
    if provider == "freebuff":
        base["base_url"] = os.getenv("OPENAI_BASE_URL") or llm.get("openai_base_url") or base["base_url"]
    merged = {**base, **{k: v for k, v in raw.items() if v not in (None, "")}}
    merged.setdefault("label", base.get("label", provider))
    return merged


def _read_external_keys(path: str) -> dict:
    """`NAME = "value"` 형태의 파이썬 설정 파일에서 허용된 키만 읽는다."""
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    key = str(p.resolve())
    mtime = p.stat().st_mtime
    cached = _EXTERNAL_KEY_CACHE.get(key)
    if cached and cached.get("mtime") == mtime:
        return cached.get("keys", {})
    keys: dict[str, str] = {}
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return {}
    for name, value in re.findall(r'^\s*([A-Z][A-Z0-9_]*)\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE):
        if name in EXTERNAL_KEY_ALLOWLIST and value.strip():
            keys[name] = value.strip()
    _EXTERNAL_KEY_CACHE[key] = {"mtime": mtime, "keys": keys}
    return keys


def resolve_key(provider: str, config: dict) -> tuple[str, str]:
    """(key, source). source: 'env' | external 파일 경로 | ''(없음)."""
    cfg = provider_cfg(provider, config)
    env_names = cfg.get("key_envs") or []
    for name in env_names:
        val = (os.getenv(name) or "").strip()
        if val:
            return val, f"env:{name}"
    ext_path = (_llm_cfg(config).get("external_keys_file") or "").strip()
    ext = _read_external_keys(ext_path)
    for name in env_names:
        val = (ext.get(name) or "").strip()
        if val:
            return val, ext_path
    return "", ""


def ollama_enabled(config: dict) -> bool:
    return bool(_llm_cfg(config).get("ollama_enabled", False))


def resolve_model(provider: str, config: dict, override: str | None = None) -> str:
    if override:
        return override.strip()
    cfg = provider_cfg(provider, config)
    return (cfg.get("model") or "").strip()


# ----------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) raccoon-shorts/1.0"


def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body or e.reason}") from e


def _get_json(url: str, headers: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body or e.reason}") from e


# ----------------------------------------------------------------------
# 프로바이더 호출
# ----------------------------------------------------------------------
def _call_openai_style(prompt: str, provider: str, config: dict,
                       model: str | None = None) -> tuple[str, dict]:
    cfg = provider_cfg(provider, config)
    key, key_src = resolve_key(provider, config)
    if not key:
        raise RuntimeError(f"{cfg.get('label', provider)} 키가 없습니다 ({'/'.join(cfg.get('key_envs') or [])})")
    model_id = resolve_model(provider, config, model)
    if not model_id:
        raise RuntimeError(f"{cfg.get('label', provider)} 모델이 지정되지 않았습니다. 웹에서 모델을 고르세요.")
    llm = _llm_cfg(config)
    base = (cfg.get("base_url") or "").rstrip("/")
    headers = {"Authorization": f"Bearer {key}"}
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/raccoon-shorts"
        headers["X-Title"] = "Raccoon Shorts"
    max_tokens = int(llm.get("max_tokens", 1000))
    if provider in ("groq", "openrouter"):
        # 추론(reasoning) 모델은 사고 토큰이 본문을 잡아먹어 빈 응답이 나온다 → 여유를 준다
        max_tokens = max(2000, max_tokens)
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": "너는 한국어 유튜브 쇼츠 작가다. 지시한 형식만 출력한다."},
            {"role": "user", "content": prompt},
        ],
        "temperature": float(llm.get("temperature", 0.7)),
        "max_tokens": max_tokens,
    }
    if provider == "groq" and ("gpt-oss" in model_id or "qwen" in model_id):
        payload["reasoning_effort"] = "low"
    def _content(d: dict) -> str:
        try:
            msg = d["choices"][0]["message"]
        except Exception:
            return ""
        val = msg.get("content")
        if isinstance(val, str):
            return val.strip()
        if isinstance(val, list):  # 일부 제공자는 content 를 파트 배열로 준다
            return "".join(p.get("text", "") for p in val if isinstance(p, dict)).strip()
        return ""

    data = _post_json(f"{base}/chat/completions", payload, headers=headers, timeout=180)
    text = _content(data)
    if not text:
        # 사고 토큰이 본문을 다 써버린 경우: 토큰을 늘리고 추론을 낮춰 1회 재시도
        retry_payload = dict(payload)
        retry_payload["max_tokens"] = max(3000, max_tokens * 2)
        if provider == "groq":
            retry_payload["reasoning_effort"] = "low"
        print(f"[LLM] {provider} 빈 응답 → 토큰 확대 후 재시도")
        data = _post_json(f"{base}/chat/completions", retry_payload, headers=headers, timeout=180)
        text = _content(data)
    if not text:
        raise RuntimeError(f"{provider} 응답이 비어 있음: {json.dumps(data)[:200]}")
    print(f"[LLM] {provider} · {model_id} · {len(text)}자 (key={key_src})")
    return text, {"backend": provider, "provider": provider, "model": model_id,
                  "key_source": key_src}


def _call_gemini(prompt: str, config: dict, model: str | None = None) -> tuple[str, dict]:
    cfg = provider_cfg("gemini", config)
    key, key_src = resolve_key("gemini", config)
    if not key:
        raise RuntimeError("GEMINI_API_KEY 가 없습니다")
    model_id = resolve_model("gemini", config, model)
    llm = _llm_cfg(config)
    url = (f"{cfg['base_url'].rstrip('/')}/models/{model_id}:generateContent"
           f"?key={key}")
    gen_cfg = {
        "temperature": float(llm.get("temperature", 0.7)),
        "maxOutputTokens": max(2048, int(llm.get("max_tokens", 1000))),
        # 3.x flash 는 사고 토큰이 출력 한도를 잡아먹어 대본이 중간에 끊긴다 → 사고 끔
        "thinkingConfig": {"thinkingBudget": 0},
    }
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": gen_cfg}
    try:
        data = _post_json(url, payload, timeout=180)
    except RuntimeError as e:
        if "thinkingConfig" in str(e) or "thinking" in str(e).lower():
            print("[LLM] gemini thinkingConfig 미지원 → 빼고 재시도")
            gen_cfg.pop("thinkingConfig", None)
            data = _post_json(url, payload, timeout=180)
        else:
            raise
    parts: list[str] = []
    for cand in data.get("candidates", []) or []:
        for p in (cand.get("content", {}) or {}).get("parts", []) or []:
            # thinking 모델의 사고 과정(thought=true)은 대본이 아니므로 제외
            if p.get("thought"):
                continue
            if p.get("text"):
                parts.append(p["text"])
    text = "\n".join(parts).strip()
    finish = ((data.get("candidates") or [{}])[0] or {}).get("finishReason", "")
    if finish == "MAX_TOKENS" and text:
        print(f"[WARN] gemini 응답이 토큰 한도에서 끊겼습니다 ({len(text)}자)")
    if not text:
        raise RuntimeError(f"gemini 응답이 비어 있음: {json.dumps(data)[:200]}")
    print(f"[LLM] gemini · {model_id} · {len(text)}자 (key={key_src})")
    return text, {"backend": "gemini", "provider": "gemini", "model": model_id,
                  "key_source": key_src}


def _call_ollama(prompt: str, config: dict, model: str | None = None) -> tuple[str, dict]:
    if not ollama_enabled(config):
        raise RuntimeError("Ollama 는 꺼져 있습니다 (웹에서 'Ollama 사용'을 켜야 합니다)")
    cfg = provider_cfg("ollama", config)
    base = (cfg.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
    installed = list_models("ollama", config)
    if not installed:
        raise RuntimeError(f"Ollama 응답 없음 ({base})")
    preferred = resolve_model("ollama", config, model)
    model_id = preferred if preferred in installed else _pick_ollama_model(installed, preferred)
    llm = _llm_cfg(config)
    data = _post_json(
        f"{base}/api/generate",
        {
            "model": model_id,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                "temperature": float(llm.get("temperature", 0.7)),
                "num_predict": int(llm.get("max_tokens", 1000)),
            },
        },
        timeout=300,
    )
    text = (data.get("response") or "").strip()
    if "Thinking Process" in text:
        parts = [p.strip() for p in text.split("\n\n") if p.strip()]
        text = parts[-1] if parts else text
    if not text:
        raise RuntimeError("Ollama 응답이 비어 있음")
    print(f"[LLM] ollama · {model_id} · {len(text)}자")
    return text, {"backend": "ollama", "provider": "ollama", "model": model_id}


OLLAMA_PREFERENCES = ("qwen3.5", "qwen3.", "gemma-3", "qwen2.5", "gemma", "llama", "mistral")


def _pick_ollama_model(installed: list[str], preferred: str) -> str:
    if preferred in installed:
        return preferred
    for key in OLLAMA_PREFERENCES:
        for name in installed:
            if key in name.lower() and ":" in name:
                return name
    return installed[0] if installed else preferred


def _template(prompt: str, config: dict) -> tuple[str, dict]:
    keyword = "AI 트렌드"
    for line in prompt.splitlines():
        if line.startswith("키워드:"):
            keyword = line.split(":", 1)[1].strip()
            break
    return TEMPLATE_SCRIPT.format(keyword=keyword), {"backend": "template", "model": "builtin"}


def call_provider(provider: str, prompt: str, config: dict,
                  model: str | None = None) -> tuple[str, dict]:
    """지정한 프로바이더만 호출한다(폴백 없음)."""
    provider = (provider or "").strip().lower()
    if provider == "gemini":
        return _call_gemini(prompt, config, model)
    if provider in ("groq", "openrouter", "freebuff", "openai"):
        if provider == "openai":
            provider = "freebuff"
        return _call_openai_style(prompt, provider, config, model)
    if provider == "ollama":
        return _call_ollama(prompt, config, model)
    if provider == "template":
        return _template(prompt, config)
    raise RuntimeError(f"알 수 없는 프로바이더: {provider}")


def _auto_order(config: dict) -> list[str]:
    order = []
    for name in AUTO_ORDER:
        if name == "freebuff":
            # Freebuff/OpenAI 호환은 키가 있을 때만
            if resolve_key("freebuff", config)[0]:
                order.append(name)
            continue
        if resolve_key(name, config)[0]:
            order.append(name)
    if ollama_enabled(config):
        order.append("ollama")
    return order


def generate(prompt: str, config: dict, backend: str | None = None,
             model: str | None = None) -> tuple[str, dict]:
    """대본 생성. 반환: (text, meta). 실패 시 RuntimeError."""
    llm = _llm_cfg(config)
    requested = (backend or llm.get("provider") or llm.get("backend") or "auto").strip().lower()
    if requested in ("auto", ""):
        order = _auto_order(config)
        if not order:
            raise RuntimeError(
                "사용 가능한 LLM 이 없습니다. 웹 '🧠 대본 LLM'에서 프로바이더를 고르거나 "
                ".env / .env 에 API 키를 넣으세요. (Ollama 는 기본 꺼짐)"
            )
    else:
        order = [requested]

    attempts: list[dict] = []
    started = time.time()
    for name in order:
        t0 = time.time()
        try:
            text, meta = call_provider(name, prompt, config, model)
            meta.update({
                "attempts": attempts,
                "elapsed_sec": round(time.time() - started, 2),
                "requested": requested,
                "order": order,
            })
            return text, meta
        except Exception as e:  # noqa: BLE001
            attempts.append({"backend": name, "error": f"{type(e).__name__}: {e}",
                             "elapsed_sec": round(time.time() - t0, 2)})

    detail = " / ".join(f"{a['backend']}: {a['error']}" for a in attempts)
    raise RuntimeError(
        f"대본 생성 실패 (요청={requested}, 시도={order}). {detail}"
    )


# ----------------------------------------------------------------------
# 모델 목록 / 상태 (웹 UI용)
# ----------------------------------------------------------------------
GROQ_EXCLUDE = ("whisper", "tts", "guard", "orpheus", "playai", "embed", "distil")


def list_models(provider: str, config: dict) -> list[str]:
    provider = (provider or "").strip().lower()
    cfg = provider_cfg(provider, config)
    key, _ = resolve_key(provider, config)
    if provider == "ollama":
        base = (cfg.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
        try:
            data = _get_json(f"{base}/api/tags", timeout=5)
        except Exception:
            return []
        return [m.get("name", "") for m in data.get("models", []) or [] if m.get("name")]
    if not key:
        raise RuntimeError(f"{cfg.get('label', provider)} 키가 없습니다")
    base = (cfg.get("base_url") or "").rstrip("/")
    if provider == "gemini":
        data = _get_json(f"{base}/models?key={key}")
        out = []
        for m in data.get("models", []) or []:
            methods = m.get("supportedGenerationMethods") or []
            if "generateContent" in methods:
                out.append((m.get("name") or "").replace("models/", ""))
        return [m for m in out if m]
    headers = {"Authorization": f"Bearer {key}"}
    if provider == "openrouter":
        data = _get_json(f"{base}/models", headers=headers)
        ids = [m.get("id") for m in data.get("data", []) or [] if m.get("id")]
        return ids[:400]
    data = _get_json(f"{base}/models", headers=headers)
    ids = [m.get("id") for m in data.get("data", []) or [] if m.get("id")]
    if provider == "groq":
        ids = [i for i in ids if not any(bad in i.lower() for bad in GROQ_EXCLUDE)]
    return ids[:400]


def provider_status(config: dict) -> dict:
    """키 보유 여부만 노출한다(값은 절대 반환하지 않음)."""
    llm = _llm_cfg(config)
    providers = {}
    for name in PROVIDER_DEFAULTS:
        cfg = provider_cfg(name, config)
        key, src = resolve_key(name, config)
        providers[name] = {
            "label": cfg.get("label", name),
            "model": resolve_model(name, config),
            "base_url": cfg.get("base_url", ""),
            "has_key": bool(key) or name == "ollama",
            "key_source": ("직접 입력 불필요" if name == "ollama" else
                           ("환경변수" if src.startswith("env:") else (src or "없음"))),
            "key_envs": cfg.get("key_envs") or [],
            "enabled": (ollama_enabled(config) if name == "ollama" else True),
        }
    return {
        "provider": (llm.get("provider") or llm.get("backend") or "auto"),
        "model": llm.get("model") or "",
        "temperature": llm.get("temperature", 0.7),
        "max_tokens": llm.get("max_tokens", 1000),
        "ollama_enabled": ollama_enabled(config),
        "external_keys_file": llm.get("external_keys_file") or "",
        "providers": providers,
    }


def test_provider(provider: str, config: dict, model: str | None = None) -> dict:
    """짧은 문장으로 연결을 확인한다."""
    started = time.time()
    text, meta = call_provider(
        provider,
        "너구리 쇼츠 대본 테스트입니다. '연결 성공'이라는 말이 들어간 한국어 한 문장만 출력해라.",
        config,
        model,
    )
    return {**meta, "text": text.strip()[:200], "elapsed_sec": round(time.time() - started, 2)}
