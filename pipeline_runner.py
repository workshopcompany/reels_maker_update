"""
너구리 쇼츠 - CLI 러너
========================
  python pipeline_runner.py --auto                # 전체 자동 (실패 시 중단)
  python pipeline_runner.py --auto --scheduled    # 스케줄러용: 오늘 이미 완료면 건너뜀
  python pipeline_runner.py --only trends         # 한 단계만
  python pipeline_runner.py --from script         # 특정 단계부터 끝까지
  python pipeline_runner.py --status              # 진행 상태 표
  python pipeline_runner.py --reset [--step tts]  # 상태 초기화

웹 UI(web_pipeline.py)와 같은 상태 파일(output/pipeline_state.json)을 공유한다.
종료 코드: 필수 단계가 모두 완료되면 0, 아니면 1.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT / "output"
FLAG_FILE = OUTPUT_DIR / "built_today.flag"

# stdout 를 새 래퍼로 교체하면, 웹 서버가 이 모듈을 import 할 때 기존 스트림이 닫혀
# Werkzeug 로깅/소켓이 깨진다. 인코딩만 바꾸는 reconfigure 를 사용한다.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pipeline_core as core


def today() -> str:
    return datetime.now().strftime("%Y%m%d")


def already_built_today() -> bool:
    if not FLAG_FILE.exists():
        return False
    return FLAG_FILE.read_text(encoding="utf-8", errors="ignore").strip().startswith(today())


def set_flag() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    FLAG_FILE.write_text(today(), encoding="utf-8")


def notify(title: str, text: str) -> None:
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(PROJECT / "scripts_notify.ps1"), "-title", title, "-text", text],
            timeout=30, capture_output=True,
        )
    except Exception as e:
        print(f"[WARN] 알림 실패: {e}")


def print_status() -> None:
    snap = core.snapshot()
    print("=" * 74)
    print(f" 너구리 쇼츠 파이프라인 상태  ({snap['progress']['done']}/{snap['progress']['total']} 완료, {snap['progress']['percent']}%)")
    print("=" * 74)
    icon = {"done": "✔", "failed": "✖", "running": "▶", "skipped": "–", "pending": "·"}
    for s in snap["steps"]:
        print(f" {icon.get(s['status'], '?')} {s['num']}. {s['name']:<28} {s['status']:<8}"
              f"{' (선택)' if s['optional'] else ''}")
        if s["error"]:
            print(f"      └ {s['error']}")
        for a in s["artifacts"][:2]:
            print(f"      └ {a['path']}")
    print("=" * 74)


def main() -> int:
    ap = argparse.ArgumentParser(description="너구리 쇼츠 파이프라인 러너")
    ap.add_argument("--auto", action="store_true", help="전체 자동 실행")
    ap.add_argument("--only", default=None, help="단일 단계 실행")
    ap.add_argument("--from", dest="start_from", default=None, help="이 단계부터 자동 실행")
    ap.add_argument("--scheduled", action="store_true", help="오늘 이미 완료면 건너뜀")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--step", default=None, help="--reset / --mark-done 과 함께 쓸 단계 id")
    ap.add_argument("--mark-done", action="store_true",
                    help="산출물이 이미 있는 단계를 수동으로 '완료' 처리 (예: 직접 만든 voice.mp3)")
    ap.add_argument("--note", default="", help="--mark-done 사유")
    ap.add_argument("--force", action="store_true", help="--scheduled 여도 강제 실행")
    args = ap.parse_args()

    core.recover_interrupted()

    if args.status:
        print_status()
        return 0

    if args.reset:
        if args.step:
            core.reset_step(args.step)
            print(f"[OK] '{args.step}' 단계 초기화")
        else:
            core.reset_all()
            print("[OK] 전체 상태 초기화")
        return 0

    if args.mark_done:
        if not args.step or args.step not in core.STEP_BY_ID:
            print("[ERROR] --step <단계 id> 가 필요합니다: " + ", ".join(core.STEP_BY_ID))
            return 1
        step = core.STEP_BY_ID[args.step]
        arts = core.resolve_artifacts(step)
        if step.get("artifacts") and not arts:
            print(f"[ERROR] '{args.step}' 산출물이 없습니다. 먼저 실제로 만들어야 합니다.")
            return 1
        state = core.load_state()
        st = state["steps"][args.step]
        st["status"] = "done"
        st["returncode"] = 0
        st["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st["error"] = ""
        st.setdefault("log", []).append(
            f"[{datetime.now().strftime('%H:%M:%S')}] 수동 완료 처리: {args.note or '산출물 확인됨'}"
        )
        core.save_state(state)
        print(f"[OK] '{step['name']}' → 완료 처리 ({len(arts)}개 산출물)")
        return 0

    if args.scheduled and not args.force and already_built_today():
        print(f"[SKIP] 오늘({today()}) 이미 완료된 실행이 있습니다. (--force 로 강제 실행)")
        return 0

    if args.only:
        if args.only not in core.STEP_BY_ID:
            print(f"[ERROR] 알 수 없는 단계: {args.only}")
            print("        사용 가능: " + ", ".join(core.STEP_BY_ID))
            return 1
        ok, reason = core.prereq_status(core.STEP_BY_ID[args.only])
        if not ok:
            print(f"[BLOCKED] {reason}")
            return 1
        ok = core.run_step(args.only)
        print_status()
        return 0 if ok else 1

    if args.auto or args.start_from:
        results = core.run_auto(start_from=args.start_from)
        print_status()
        print("[결과] " + ", ".join(f"{k}={v}" for k, v in results.items()))

        snap = core.snapshot()
        failed_required = [s for s in snap["steps"]
                           if s["status"] in ("failed", "blocked", "skipped") and not s["optional"]
                           and s["id"] in results]
        done_required = all(
            s["status"] in ("done", "skipped") for s in snap["steps"]
            if not s["optional"] and s["id"] != "upload"
        )
        if failed_required:
            for s in failed_required:
                print(f"[FAIL] {s['name']}: {s['error']}")
            notify("너구리 쇼츠 실패", f"{failed_required[0]['name']} 단계 실패 — 로그 확인")
            return 1

        if done_required:
            set_flag()
            final = next((a["path"] for s in snap["steps"] if s["id"] == "assemble"
                          for a in s["artifacts"][:1]), "")
            notify("너구리 쇼츠 완료", "초안이 준비됐어! CapCut에서 내보내기를 눌러줘.")
            if final:
                print(f"[DONE] 미리보기: {final}")
            print("[DONE] 오늘 작업 완료 플래그 기록")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
