# 🦝 Reels Maker — 너구리 AI 쇼츠 파이프라인

트렌드 수집 → 대본 → 음성(TTS) → 장면 이미지 → B-Roll → CapCut 초안 → **9:16 쇼츠 MP4** → 유튜브 업로드까지
**웹 브라우저 UI에서 단계별로(또는 전체 자동으로)** 돌리는 Windows용 프로그램입니다.

- 내려받아서 **`install.bat` → `run.bat`** 두 번만 더블클릭하면 바로 쓸 수 있습니다.
- API 키는 **`.env`** 파일에만 넣습니다. 이 저장소에는 어떤 키도 들어 있지 않습니다.

---

## 1. 준비물

| 항목 | 필요 여부 | 비고 |
|---|---|---|
| Windows 10 / 11 (x64) | 필수 | |
| Python 3.10 ~ 3.12 | 필수 | 설치 시 **“Add python.exe to PATH”** 체크 |
| 인터넷 | 필수 | 패키지·모델·트렌드 수집 |
| FFmpeg | 필수 | `install.bat` 이 winget으로 자동 설치 시도 |
| GPU (SDXL / WanGP) | 선택 | 5단계(장면 이미지)·7단계(움직임)에만 사용 |
| CapCut (PC) | 선택 | 9단계 초안 열기/편집 |
| ElevenLabs 키 | 선택 | 없으면 무료 edge-tts 음성으로 자동 대체 |
| LLM 키 (Gemini/Groq/OpenRouter/OpenAI 중 1개) | 선택 | 대본·업로드 문구 품질에 영향 |

> GPU 없이도 1·2·3·4·6·8·9·10·11단계는 동작합니다. 5단계(장면 이미지)는 SDXL 환경이 필요하고, 없으면 선택 단계라 건너뜁니다.

---

## 2. 설치 (3단계)

### ① 내려받기
이 폴더를 통째로 `C:\Reels_maker` 처럼 **한글·공백이 없는 경로**에 풀어 둡니다.

### ② `install.bat` 더블클릭
자동으로 아래를 처리합니다.

1. Python 3.10+ 확인 (없으면 안내 후 종료)
2. 프로젝트 안에 `venv\` 가상환경 생성
3. `requirements.txt` 패키지 설치 (Flask, faster-whisper, pycapcut, yt-dlp, elevenlabs, edge-tts, Google API …)
4. FFmpeg 확인 → 없으면 `winget` 으로 설치 시도 (실패 시 수동 안내)
5. `.env.example` → `.env` 복사 (이미 있으면 그대로 둠)
6. `bootstrap.py` 로 `config.json` 경로를 지금 위치 기준으로 자동 수정 + 작업 폴더 생성
7. Whisper tiny 모델(약 80MB) 다운로드 여부를 물어봄 (`n` 입력 시 생략)

### ③ `.env` 에 API 키 입력 (선택)
```bat
notepad .env
```
키가 없어도 실행은 되지만, 대본·업로드 문구 품질과 음성 품질이 떨어집니다.

| 키 | 쓰는 곳 | 없을 때 |
|---|---|---|
| `ELEVENLABS_API_KEY` | 4단계 TTS | 무료 `edge-tts` 로 자동 대체 |
| `GEMINI_API_KEY` / `GROQ_API_KEY` / `OPENROUTER_API_KEY` / `OPENAI_API_KEY` | 3단계 대본, 업로드 문구 | 템플릿 문구로 대체(품질↓) |
| `GITHUB_TOKEN` | 2단계 트렌드 | 호출 제한이 빨리 걸림 |
| `HUGGINGFACE_TOKEN` | 모델 다운로드 | 속도 제한 가능 |
| `scripts\.secrets\youtube\client_secret.json` | 11단계 업로드 | 업로드 단계만 건너뜀 |

---

## 3. 실행

**`run.bat` 더블클릭** → 서버가 켜지고 브라우저가 `http://127.0.0.1:5000` 을 자동으로 엽니다.
창을 닫으면 서버가 꺼집니다. 포트를 바꾸려면 `set RACCOON_WEB_PORT=5001` 후 실행하세요.

수동 실행(같은 결과):
```bat
venv\Scripts\python.exe bootstrap.py
venv\Scripts\python.exe web_pipeline.py
```

CLI로도 돌릴 수 있습니다.
```bat
venv\Scripts\python.exe pipeline_runner.py --status      :: 진행 상태
venv\Scripts\python.exe pipeline_runner.py --auto        :: 전체 자동
venv\Scripts\python.exe pipeline_runner.py --only tts    :: 한 단계만
```

---

## 4. 11단계 파이프라인

| # | 단계 | 하는 일 | 필수 |
|---|---|---|---|
| 1 | 채널 분석 | 내 유튜브 채널의 최근 영상·조회수·키워드 수집 | 필수 |
| 2 | 트렌드 수집 | YouTube·GitHub·HuggingFace 후보 수집 → **사람이 후보 확정** | 선택 |
| 3 | 대본 초안 | 너구리 페르소나 260~350자 대본 (LLM) | 필수 |
| 4 | 음성 생성 | ElevenLabs → 실패 시 edge-tts | 필수 |
| 5 | 장면 생성 | SDXL 480p 생성 → 1080p 업스케일, 말풍선 | 선택(GPU) |
| 6 | B-Roll 준비 | 전체 화면/창 녹화, MP4 업로드, 사용 안 함 | 선택 |
| 7 | 장면 모션 | 고른 장면만 WanGP img2video | 선택(GPU) |
| 8 | 발화 타이밍 | Whisper tiny로 말소리 구간만 추출 | 필수 |
| 9 | CapCut 초안 | pycapcut 드래프트 생성 | 필수 |
| 10 | 미리보기 MP4 | FFmpeg 1080×1920 쇼츠 합성 | 선택 |
| 11 | 유튜브 업로드 | 제목·설명·태그 자동 생성 후 업로드 | 선택 |

기본 설정은 **B-Roll 사용 안 함 + 장면 전체 화면 + 말풍선** 입니다(`config.json` 의 `broll.enabled`, `assembly.layout`).

---

## 5. 처음 영상 한 편 만드는 순서

1. `run.bat` 실행 → 웹 UI 접속
2. **채널 분석** 실행 (채널 URL은 웹 UI 또는 `config.channel.channel_url` 에 입력)
3. **트렌드 수집** → 마음에 드는 후보 선택 → **확정**
4. **대본 초안** → 웹에서 문장 다듬기 → **승인**
5. **음성 생성** (ElevenLabs 키가 없으면 자동으로 edge-tts)
6. (GPU 있으면) **장면 생성** → 웹 `🖼 장면 · 말풍선` 에서 장면별 문구 입력
7. **발화 타이밍** → **CapCut 초안** → **미리보기 MP4**
8. 결과물: `output\final\shorts_final_*.mp4`

> 6단계 B-Roll 은 “사용 안 함”으로 두면 건너뛰고 장면 이미지 전체 화면으로 만듭니다.

---

## 6. 선택 기능 설정

### GPU 장면 이미지 (5단계)
`config.json` 에서 본인 환경 경로를 넣습니다.
```json
"scenes": {
  "python": "C:\\sd\\venv\\Scripts\\python.exe",
  "model_path": "C:\\sd\\models\\sdxl-turbo"
}
```
`torch`, `diffusers`, `transformers` 가 설치된 별도 가상환경을 `scenes.python` 에 지정하세요.

### WanGP 움직임 (7단계)
```json
"wangp": {
  "enabled": true,
  "root": "C:\\WanGP",
  "python": "C:\\WanGP\\venv\\Scripts\\python.exe",
  "base_url": "http://127.0.0.1:7860"
}
```
WanGP 웹UI(7860)를 같이 켜두면 VRAM이 부족할 수 있습니다. `scenes.auto_stop_wangp` 가 생성 전에 정리합니다.

### CapCut 초안
`config.paths.capcut_draft_root`(기본 `%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft`)에
`너구리쇼츠_초안_<시각>` 폴더가 생깁니다. `output\capcut_export\` 에 사본도 남습니다.

### 유튜브 업로드 (11단계)
1. Google Cloud Console → YouTube Data API v3 사용 설정 → OAuth 클라이언트(데스크톱) JSON 다운로드
2. `scripts\.secrets\youtube\client_secret.json` 으로 저장
3. `config.youtube.enabled` 를 `true` 로, 첫 실행 때 브라우저 동의 → `token.pickle` 생성
4. **처음엔 `default_privacy` 를 `private` 로 두고 확인하세요.** (기본값이 private)

### 매일 자동 실행
```bat
scripts_register_scheduler.bat     :: 매일 17:30 + 백업 23:00 등록
scripts_run_daily.bat              :: 지금 바로 1회 실행
```
해제: `schtasks /delete /tn "ReelsMaker_Daily" /f`, `schtasks /delete /tn "ReelsMaker_Backup" /f`

---

## 7. 폴더 구조

```text
Reels_maker/
├── install.bat              설치 (1회)
├── run.bat                  실행 (웹 UI)
├── bootstrap.py             config.json 경로 자동 수정 + 폴더 생성
├── web_pipeline.py          웹 서버 (Flask, 127.0.0.1:5000)
├── pipeline_runner.py       CLI 실행기
├── pipeline_core.py         단계 정의·상태·로그
├── config.json              모든 설정
├── .env.example → .env      API 키 (커밋 금지)
├── requirements.txt
├── templates/index.html     웹 UI
├── scripts_*.py             각 단계 실행 파일 (1~11단계)
├── llm_client.py            LLM 호출(프로바이더 자동 선택)
├── motion_plan.py           모션 계획
├── broll_lib.py             B-Roll 라이브러리
├── bubble_overlay.py        말풍선 렌더링
├── youtube_meta.py          업로드 문구 생성
├── scripts_run_daily.bat    배치 실행
├── scripts_register_scheduler.bat  작업 스케줄러 등록
├── scripts_run_hidden.vbs   숨김 실행
├── scripts_notify.ps1       완료 알림
├── venv/                    가상환경 (install.bat 생성)
├── models/whisper/          Whisper 모델
├── output/                  모든 산출물
│   ├── scenes/  generated_videos/  broll_clips/  final/  capcut_export/
└── scripts/
    ├── .secrets/youtube/    client_secret.json, token.pickle (커밋 금지)
    └── logs/
```

---

## 8. 문제 해결

| 증상 | 원인 / 해결 |
|---|---|
| `No module named 'flask'` | 시스템 파이썬으로 실행함 → `run.bat` 사용 (venv 파이썬 사용) |
| `.bat` 창이 바로 닫힘 | 명령 프롬프트에서 `install.bat` 을 실행해 오류 메시지 확인 |
| `ffmpeg` 을 찾을 수 없음 | `install.bat` 재실행 또는 FFmpeg 설치 후 **새 창**에서 실행 |
| 포트 5000 사용 중 | `set RACCOON_WEB_PORT=5001` 후 `run.bat` |
| TTS 401 / 음성 실패 | `.env` 의 `ELEVENLABS_API_KEY` 확인 → 없으면 자동으로 edge-tts 로 진행 |
| Whisper 모델이 없다고 나옴 | 인터넷 연결 후 8단계 재실행 → 자동 다운로드 (`config.whisper.fallback_model`) |
| 5단계 실패 (torch/diffusers) | `config.scenes.python` / `model_path` 를 GPU 환경에 맞게 설정하거나 5단계 건너뛰기 |
| VRAM 부족(OOM) | WanGP 웹UI 종료, 5·7단계 동시 실행 금지, `scenes.cpu_offload` 유지 |
| 경로를 옮긴 뒤 동작이 이상함 | `run.bat` 이 `bootstrap.py` 로 경로를 다시 맞춥니다. 한 번 실행하면 됩니다 |
| 한글이 깨져 보임 | Windows 설정 → 언어 → “Beta: UTF-8 지원” 켜기, 또는 `chcp 65001` |

---

## 9. 개인정보 / 보안

이 저장소는 공유용으로 정리되어 있으며 **다음은 포함되어 있지 않습니다.**

- `.env` (모든 API 키)
- `scripts/.secrets/` (YouTube `client_secret.json`, `token.pickle`)
- `output/` (생성된 영상·이미지·대본)
- `models/` (대용량 모델 파일)
- 개인 PC의 절대 경로 (`E:\...`, `D:\saas\...` 등)

지켜야 할 것:

1. **키는 `.env` 에만** 넣으세요. `config.json`, 소스, 스크린샷에 붙여넣지 마세요.
2. `.env`, `scripts/.secrets/`, `output/` 은 `.gitignore` 로 제외되어 있습니다. 강제로 올리지 마세요.
3. `install.bat`/`run.bat` 이 처음 실행될 때 `config.json` 의 경로를 지금 PC 기준으로 바꿉니다.
   이 변경이 git에 표시되는 것은 정상이며, 올리고 싶지 않다면 커밋하지 않으면 됩니다.
4. 업로드(11단계)는 되돌리기 어렵습니다. 처음엔 `--dry` 또는 `private` 로 확인하세요.
5. 채널 URL·계정 정보는 본인 `config.json` 에만 두고 공유하지 마세요.

---

## 10. 라이선스 / 면책

개인적·교육적 사용을 전제로 공유되는 코드입니다. 각 외부 서비스(ElevenLabs, Google/YouTube, WanGP, SDXL 모델 등)의
이용 약관을 따르세요. 업로드·저작권·초상권 등 사용 결과에 대한 책임은 사용자에게 있습니다.
