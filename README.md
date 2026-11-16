# 🎲 디스코드 팀 나누기 봇

대기방 음성 채널에 모인 사람들을 **무작위로 두 팀으로 나눠**, 미리 만들어 둔 두 개의 음성 채널로 한 번에 옮겨 주는 봇이에요.

- `/팀나누기` — 지금 들어 있는 음성 채널(대기방) 인원을 5:5(홀수면 1명 차이)로 나눠 이동
- 결과 메시지 버튼 — **🔀 다시 섞기**, **📢 대기방으로 모으기**
- `/모이기` — 두 팀 채널에 흩어진 인원을 한 채널로 다시 모으기

---

## 1. 준비물

| 준비 | 설명 |
| --- | --- |
| 디스코드 봇 | 디스코드 개발자 포털에서 무료로 만들 수 있어요 |
| 음성 채널 3개 | `대기방`(사람들이 모이는 곳), `팀 1`, `팀 2` |
| 파이썬 3.10 이상 | 로컬에서 돌릴 때만 필요 (호스팅에 올리면 자동) |

## 2. 봇 만들기 (한 번만)

1. <https://discord.com/developers/applications> → **New Application** → 이름 입력(예: 팀나누기)
2. 왼쪽 **Bot** → **Reset Token** → 토큰 복사 (이 값은 절대 공개하지 마세요)
3. 같은 화면 아래 **Privileged Gateway Intents** → **SERVER MEMBERS INTENT** 를 **ON**
   (이게 꺼져 있으면 봇이 켜지지 않아요. `MESSAGE CONTENT INTENT` 는 필요 없어요)
4. 왼쪽 **OAuth2 → URL Generator**
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `View Channels`, `Move Members`
   - 만들어진 링크로 봇을 서버에 초대

> 빠른 초대 링크 (위 권한 그대로):
> `https://discord.com/api/oauth2/authorize?client_id=봇_애플리케이션_ID&permissions=16778240&scope=bot%20applications.commands`

## 3. 설치하고 실행하기 (내 컴퓨터에서)

```bash
cd discord_team_bot
pip install -r requirements.txt
cp .env.example .env          # Windows: copy .env.example .env
```

`.env` 를 열어 토큰을 넣어 주세요.

```env
DISCORD_BOT_TOKEN=여기에_복사한_토큰
```

실행:

```bash
python bot.py
```

`로그인 완료: ...` 가 뜨면 성공이에요. 슬래시 명령어는 전역 등록이라 처음 한 번은
디스코드에 나타나기까지 최대 1시간쯤 걸릴 수 있어요. 바로 쓰고 싶다면 `.env` 에
`DISCORD_GUILD_ID=서버_ID` 를 넣으세요 (서버 ID: 디스코드 설정 → 고급 → 개발자 모드 ON →
서버 우클릭 → 서버 ID 복사).

## 4. 사용법

1. **대기방** 음성 채널에 사람들이 모입니다.
2. 아무 음성 채널에서 `/팀나누기` 를 입력하고 `팀1_채널`, `팀2_채널` 을 고릅니다.
3. 팀이 공개되고 모두 각 팀 채널로 이동합니다.
4. 결과 메시지의 버튼
   - **🔀 다시 섞기** — 같은 인원을 다시 섞어 이동 (버튼은 30분 동안 사용 가능)
   - **📢 대기방으로 모으기** — 두 팀 채널 인원을 처음 대기방으로 복귀
5. 팀 채널에 흩어진 인원을 한 번에 모으려면 `/모이기` → `대상_채널1`(, `대상_채널2`),
   `모일_채널` 은 비워 두면 지금 계신 채널로 모여요.

> 버튼은 명령을 실행한 사람과 **'멤버 이동'** 권한이 있는 사람만 누를 수 있어요.

## 5. 무료 클라우드에서 24시간 돌리기

Render · Koyeb · Cloudtype 처럼 **Web Service(웹 서비스)** 타입으로 올리면 되고,
설정은 세 가지예요.

| 항목 | 값 |
| --- | --- |
| Build / Install Command | `pip install -r requirements.txt` |
| Start Command | `python bot.py` |
| 환경변수 | `DISCORD_BOT_TOKEN` (필수), `DISCORD_GUILD_ID` (권장) |
| Health Check Path | `/health` |
| 배포 방식 | GitHub 저장소 연결 후 Python 또는 Dockerfile 빌드 |

- 호스팅이 포트를 자동으로 넣어 주는 경우(`PORT` 환경변수) 봇이 **같은 프로세스 안에서
  가벼운 헬스체크 서버를 백그라운드 스레드로** 함께 띄웁니다. 그래서 "포트를 열지 않았다"며
  컨테이너를 죽이는 문제가 생기지 않아요. 추가 패키지나 별도 파일은 없습니다.
- `PORT` 가 자동으로 들어오지 않는 호스팅이라면 환경변수에 `ENABLE_HEALTH_SERVER=true`
  를 추가하세요 (8080 포트로 열립니다). 브라우저로 `http://내주소/health` 를 열어
  `{"status": "ok"}` 가 보이면 정상이에요.
- Worker(백그라운드 작업) 타입은 포트가 필요 없어서 `PORT` 없이 그냥 `python bot.py` 만
  넣으면 됩니다.
- 무료 플랜 중에는 일정 시간 요청이 없으면 잠드는 호스팅도 있어요. 24시간 깨어 있게 하려면
  그 호스팅의 슬립 설정을 확인하거나, 외부 핑 서비스로 `/health` 를 주기적으로 호출해 주세요.
- **GitHub 에 올릴 때 `.env` 는 절대 올리지 마세요.** (같은 폴더의 `.gitignore` 가 막아 줍니다.
  호스팅에는 토큰을 환경변수로 넣습니다.)

로컬에서 헬스체크까지 확인하려면:

```bash
# macOS / Linux
PORT=8080 python bot.py &
curl http://localhost:8080/health
```

### Docker 로 올리는 호스팅 (Koyeb·Cloudtype·Fly.io 등)

같은 폴더의 `Dockerfile` 을 그대로 쓰면 됩니다. 저장소를 GitHub 에 올리고
호스팅에서 "Dockerfile" 빌드를 고른 뒤 환경변수만 넣어 주세요.

```bash
# 로컬에서 미리 확인
docker build -t team-bot .
docker run --rm -e DISCORD_BOT_TOKEN=토큰 -e PORT=8080 -p 8080:8080 team-bot
curl http://localhost:8080/health
```

## 6. 안 되면

| 증상 | 해결 |
| --- | --- |
| 봇이 켜지지 않고 `PrivilegedIntentsRequired` | 개발자 포털 → Bot → **SERVER MEMBERS INTENT** ON |
| `LoginFailure` / 토큰 오류 | 토큰을 다시 복사 (Reset Token 후 새 값) |
| 명령어가 안 보여요 | 전역 등록은 최대 1시간. `DISCORD_GUILD_ID` 를 넣으면 즉시 등록 |
| "먼저 대기방 음성 채널에 들어간 뒤..." | 봇을 부른 사람이 음성 채널에 있어야 해요 |
| "인원이 1명뿐이에요" | 봇 제외 2명 이상 필요 |
| "멤버 이동 권한이 없어요" | 봇 역할에 **Move Members** 권한, 해당 채널 권한에서도 허용 |
| 일부만 이동했어요 | 서버 설정 → 역할에서 봇 역할을 팀 채널들보다 위로 올리거나 채널 권한 확인 |
| `헬스체크 서버를 열지 못했어요` | 다른 프로그램이 그 포트를 쓰는 중. `PORT` 값을 바꾸거나 헬스체크를 끄세요 (봇은 계속 동작) |

## 파일 구성

```
discord_team_bot/
├── bot.py            # 봇 전체 코드 (명령어·버튼·헬스체크 서버)
├── requirements.txt  # discord.py, python-dotenv
├── .env.example      # 환경변수 예시 (복사해서 .env 로)
├── Dockerfile        # 무료 클라우드(Docker) 배포용
├── .gitignore        # .env 등 제외
└── README.md         # 이 문서
```
