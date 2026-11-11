"""
[공용] 장면 이미지에 말풍선(스피치 버블) 그리기
================================================
B-Roll 없이 장면 이미지/영상만으로 쇼츠를 만들 때, 화면에 안내 문구(사이트 주소 등)를
넣기 위한 모듈. 문구가 비어 있으면 말풍선을 그리지 않는다.

- scripts_generate_scenes.py (장면 생성, venv) 와
  web_pipeline.py (문구만 다시 적용, pycapcut venv) 양쪽에서 import 한다.
  → torch/diffusers 같은 무거운 의존성을 절대 import 하지 않는다 (PIL 만 사용).
- 말풍선은 '업스케일된 최종 장면 이미지' 위에 그린다. 원본(clean)은 따로 보관해서
  문구를 지우면 깨끗한 이미지로 되돌릴 수 있게 한다.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

DEFAULT_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\malgunbd.ttf",   # 맑은 고딕 Bold
    r"C:\Windows\Fonts\malgun.ttf",
    r"C:\Windows\Fonts\NanumGothicBold.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]

DEFAULT_STYLE = {
    "font": "",                 # 비우면 위 후보에서 자동 선택
    "font_size": 0,             # 0 = 이미지 폭 기준 자동
    "margin_ratio": 0.085,      # 좌우 여백 (클수록 말풍선이 좁아짐)
    "max_lines": 4,
    "position": "top",          # top | middle | bottom
    "fill": "#ffffff",
    "text_color": "#15202b",
    "outline": "#15202b",
    "opacity": 0.94,            # 0.0~1.0
}


def _hex_to_rgba(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    value = (value or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    try:
        r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        r, g, b = 255, 255, 255
    return (r, g, b, alpha)


def pick_font(font_path: str = "") -> str:
    for candidate in ([font_path] if font_path else []) + DEFAULT_FONT_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def _load_font(size: int, font_path: str) -> ImageFont.FreeTypeFont:
    path = pick_font(font_path)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """한글/URL 모두 잘리지 않게 글자 단위로 줄바꿈한다."""
    lines: list[str] = []
    for para in str(text).replace("\\n", "\n").splitlines():
        para = para.strip()
        if not para:
            continue
        current = ""
        for ch in para:
            candidate = current + ch
            if not current or draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = ch
        if current:
            lines.append(current)
    return lines or [""]


def _rounded_mask(size: tuple[int, int], box: tuple[int, int, int, int],
                  radius: int, tail: list[tuple[int, int]]) -> Image.Image:
    mask = Image.new("L", size, 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle(box, radius=radius, fill=255)
    if tail:
        md.polygon(tail, fill=255)
    return mask


def draw_speech_bubble(image: Image.Image, text: str, style: dict | None = None) -> Image.Image:
    """이미지 위에 말풍선 + 문구를 그려 새 이미지를 반환한다(원본은 수정하지 않음)."""
    text = (text or "").strip()
    if not text:
        return image

    cfg = dict(DEFAULT_STYLE)
    cfg.update({k: v for k, v in (style or {}).items() if v not in (None, "")})

    base = image.convert("RGBA")
    width, height = base.size
    margin = max(16, int(width * float(cfg.get("margin_ratio") or 0.085)))
    max_box_w = width - margin * 2
    pad = max(14, int(width * 0.032))

    font_size = int(cfg.get("font_size") or 0) or max(30, int(width * 0.05))
    max_lines = max(1, int(cfg.get("max_lines") or 4))

    measuring = ImageDraw.Draw(base)
    font = _load_font(font_size, cfg.get("font", ""))
    lines = _wrap_text(measuring, text, font, max_box_w - pad * 2)
    # 너무 길면 글자를 줄여 최대 줄 수에 맞춘다
    while len(lines) > max_lines and font_size > 26:
        font_size -= 2
        font = _load_font(font_size, cfg.get("font", ""))
        lines = _wrap_text(measuring, text, font, max_box_w - pad * 2)

    line_h = int(font_size * 1.34)
    text_w = max(int(measuring.textlength(line, font=font)) for line in lines)
    box_w = min(max_box_w, text_w + pad * 2)
    box_h = len(lines) * line_h + pad * 2

    x0 = (width - box_w) // 2
    pos = str(cfg.get("position") or "top").lower()
    if pos == "middle":
        y0 = int(height * 0.26)
    elif pos == "bottom":
        y0 = int(height * 0.58)
    else:
        y0 = int(height * 0.075)
    x1, y1 = x0 + box_w, y0 + box_h
    radius = max(18, int(font_size * 0.62))

    tail_h = max(26, int(font_size * 0.75))
    tail_x = x0 + int(box_w * 0.66)
    tail = [
        (tail_x, y1 - 6),
        (tail_x + int(font_size * 0.95), y1 - 6),
        (tail_x + int(font_size * 0.35), y1 + tail_h),
    ]

    stroke = max(4, int(width * 0.006))
    mask = _rounded_mask(base.size, (x0, y0, x1, y1), radius, tail)

    # 그림자
    shadow = mask.filter(ImageFilter.GaussianBlur(max(6, stroke * 2)))
    shadow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shadow_layer.paste((0, 0, 0, 90), (0, int(stroke * 1.6)), shadow)

    # 테두리 = 마스크를 살짝 키운 영역 − 원래 마스크
    grown = mask.filter(ImageFilter.MaxFilter(stroke * 2 + 1))
    border = ImageChops.subtract(grown, mask)
    body_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    body_layer.paste(_hex_to_rgba(cfg.get("outline", "#15202b")), (0, 0), border)
    fill_alpha = int(max(0.2, min(1.0, float(cfg.get("opacity") or 0.94))) * 255)
    body_layer.paste(_hex_to_rgba(cfg.get("fill", "#ffffff"), fill_alpha), (0, 0), mask)

    out = Image.alpha_composite(base, shadow_layer)
    out = Image.alpha_composite(out, body_layer)

    draw = ImageDraw.Draw(out)
    color = _hex_to_rgba(cfg.get("text_color", "#15202b"))
    for i, line in enumerate(lines):
        lw = draw.textlength(line, font=font)
        draw.text(
            (x0 + (box_w - lw) / 2, y0 + pad + i * line_h + (line_h - font_size) / 2 - font_size * 0.12),
            line, font=font, fill=color,
        )

    return out.convert(image.mode) if image.mode != "RGBA" else out


# ----------------------------------------------------------------------
# 투명 레이어 (영상 위에 얹기)
# ----------------------------------------------------------------------
def draw_bubble_layer(size: tuple[int, int], text: str, style: dict | None = None) -> Image.Image:
    """말풍선만 그린 투명 레이어(RGBA)를 만든다.

    WanGP 로 만든 모션 클립은 입력 이미지를 다시 그리기 때문에 말풍선을 이미지에 넣어두면
    글자가 뭉개진다. 그래서 영상 생성은 clean 원본으로 하고, 말풍선은 조립 단계에서
    이 레이어를 영상 위에 얹는다(글자가 원본 해상도로 선명하게 유지된다).
    """
    layer = Image.new("RGBA", (max(1, int(size[0])), max(1, int(size[1]))), (0, 0, 0, 0))
    return draw_speech_bubble(layer, text, style)


def bubble_layer_path(text: str, size: tuple[int, int], style: dict | None = None,
                      out_dir: str | Path = "output/scenes/bubbles") -> Path | None:
    """투명 말풍선 PNG 경로(문구/크기/스타일별 캐시). 문구가 비면 None."""
    text = (text or "").strip()
    if not text:
        return None
    width, height = int(size[0]), int(size[1])
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    key_src = json.dumps({"t": text, "s": [width, height], "st": style or {}},
                         ensure_ascii=False, sort_keys=True)
    key = hashlib.md5(key_src.encode("utf-8")).hexdigest()[:10]
    path = out_dir / f"bubble_{width}x{height}_{key}.png"
    if path.exists() and path.stat().st_size > 200:
        return path
    try:
        draw_bubble_layer((width, height), text, style).save(path, format="PNG")
    except Exception:
        return None
    # 같은 크기의 옛 레이어(문구가 바뀌기 전)는 정리한다
    for old in out_dir.glob(f"bubble_{width}x{height}_*.png"):
        if old != path:
            try:
                old.unlink()
            except Exception:
                pass
    return path


# ----------------------------------------------------------------------
# 장면 파일에 적용 (clean 원본 보관)
# ----------------------------------------------------------------------
FRESH_TOLERANCE_SEC = 300.0   # 같은 회차에서 만든 파일로 볼 시간 차이(5분)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except Exception:
        return 0.0


def clean_matches(image_path: str | Path, clean_path: str | Path,
                  tolerance: float = FRESH_TOLERANCE_SEC) -> bool:
    """clean 원본이 '지금 이 장면 이미지'와 같은 회차에서 만들어진 것인지.

    이미지를 새로 만들면 clean/ 도 같이 새로 써야 한다. 이 검사가 없으면
    예전 회차의 clean 원본이 남아 있어서, 말풍선을 지웠을 때 **이전 장면 이미지로
    되돌아가거나** WanGP 모션이 옛 이미지로 만들어지는 사고가 난다.
    """
    clean_path = Path(clean_path)
    if not clean_path.exists():
        return False
    t_img = _mtime(Path(image_path))
    if not t_img:
        return True
    # '원본이 더 새것'은 정상(방금 복사한 경우도 있다). 문제는 원본이 **옛 회차**인 경우.
    return _mtime(clean_path) + tolerance >= t_img


def apply_to_scene(image_path: str | Path, text: str, clean_dir: str | Path,
                   style: dict | None = None, fresh_source: bool = False) -> dict:
    """장면 PNG 에 말풍선을 적용/제거한다.

    - 원본(clean)은 clean_dir 에 보관한다.
    - fresh_source=True : 방금 만든 이미지(말풍선 없음) → 원본을 항상 새로 덮어쓴다.
    - text 가 있으면 원본 위에 말풍선을 그리고, 없으면 원본을 그대로 되돌린다.
    - 원본이 현재 이미지보다 오래됐으면(예전 회차) 아무것도 하지 않는다.
      (옛 이미지로 되돌아가거나 옛 원본에 말풍선을 다시 그리는 것을 막는다)
    """
    image_path = Path(image_path)
    clean_dir = Path(clean_dir)
    clean_path = clean_dir / image_path.name
    result = {"image": str(image_path), "clean": str(clean_path), "bubble": bool((text or "").strip()),
              "changed": False}
    if not image_path.exists():
        result["error"] = f"장면 이미지가 없습니다: {image_path}"
        return result

    clean_dir.mkdir(parents=True, exist_ok=True)
    if fresh_source:
        clean_path.write_bytes(image_path.read_bytes())
    elif not clean_path.exists():
        clean_path.write_bytes(image_path.read_bytes())
    elif not clean_matches(image_path, clean_path):
        result["stale_clean"] = True
        result["warning"] = (
            f"clean 원본이 현재 장면 이미지보다 오래됐습니다({clean_path.name}) — "
            "이전 이미지로 되돌리지 않도록 건너뜁니다. 5단계를 다시 실행하면 원본이 새로 만들어집니다."
        )
        return result

    if (text or "").strip():
        with Image.open(clean_path) as src:
            drawn = draw_speech_bubble(src, text, style)
            drawn.save(image_path, format="PNG")
        result["changed"] = True
    else:
        # 문구를 지우면 깨끗한 원본으로 복원
        if image_path.read_bytes() != clean_path.read_bytes():
            image_path.write_bytes(clean_path.read_bytes())
            result["changed"] = True
    return result
