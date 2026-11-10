# 경고봇

새로운 롤 내전 서버에서 관리자와 보조관리자가 경고를 관리하는 Discord 봇입니다.

## 명령어

- `/경고부여 대상 사유`: 관리자·보조관리자 역할 전용. 경고를 1회 부여합니다.
- `/경고차감 대상 사유`: 관리자·보조관리자 역할 전용. 사유를 기록하고 경고를 1회 차감합니다.
- `/경고조회 [대상]`: 자신의 경고를 조회합니다. 관리자·보조관리자는 다른 서버원도 조회할 수 있습니다.

## 경고 처리 기준

| 누적 경고 | 역할 및 처리 |
| --- | --- |
| 0회 | 경고 역할 없음 |
| 1회 | `경고 1회` 역할 지급, 부여 시점부터 30일 후 자동 해제 |
| 2회 | `경고 1회` 역할 제거, `영구경고` 역할 지급, 자동 만료 없음 |

- 이미 영구경고 상태인 서버원에게는 경고를 더 추가하지 않습니다.
- 영구경고를 1회 차감하면 `경고 1회` 상태가 되며, 차감 시점부터 30일의 새 만료 기간이 시작됩니다.
- 경고 1회를 차감하면 경고 역할을 모두 제거합니다.
- 영구경고는 역할만 지급합니다. 자동 차단·추방·타임아웃은 적용하지 않습니다.
- 서버에서 나갔다가 다시 들어와도 만료되지 않은 경고 역할을 복구합니다.
- 부여, 차감, 자동 만료 사유와 담당자는 SQLite에 보관됩니다.

## 환경 변수

`.env.example`의 값을 Railway Variables에 등록합니다.

- `WARNING_LOG_CHANNEL_ID`: 경고 부여·차감·자동 만료 임베드를 기록할 채널
- `ADMIN_ROLE_ID`: 관리자 역할
- `ASSISTANT_ADMIN_ROLE_ID`: 보조관리자 역할
- `WARNING_ROLE_ID`: `경고 1회` 역할
- `PERMANENT_WARNING_ROLE_ID`: `영구경고` 역할
- `TEST_GUILD_ID`: 명령어를 즉시 동기화할 테스트/운영 서버 ID. 선택 사항
- `DATABASE_PATH`: SQLite 경로. Railway Volume 사용 시 `/data/warning_bot.db` 권장

## Discord 설정

1. 개발자 포털의 Bot 설정에서 **Server Members Intent**를 켭니다.
2. OAuth2 URL Generator에서 `bot`, `applications.commands`를 선택합니다.
3. 봇에 `Manage Roles`, `View Channels`, `Send Messages`, `Embed Links`, `Read Message History` 권한을 줍니다.
4. 역할 순서를 위에서부터 `경고봇 역할` → `영구경고`·`경고 1회` 역할 순서로 둡니다.
5. `.env.example`의 역할 및 채널 ID를 실제 ID로 바꿉니다.

관리자와 보조관리자 역할 보유 여부는 역할 **이름이 아니라 ID**로 확인합니다.

## Railway 배포

1. ZIP 파일을 풀어 GitHub 저장소에 업로드합니다.
2. Railway에서 해당 저장소를 연결합니다.
3. Variables에 `.env.example`의 값을 등록합니다.
4. 데이터 보존을 위해 Railway Volume을 `/data`에 마운트하고 `DATABASE_PATH=/data/warning_bot.db`로 설정합니다.
5. 실행 명령은 `python bot.py`입니다. 포함된 `Procfile`도 같은 명령을 사용합니다.

## 주의

- Volume 없이 배포하면 재배포 시 경고 기록과 만료일이 사라질 수 있습니다.
- 봇 역할이 경고 역할이나 대상 서버원의 최고 역할보다 낮으면 역할 변경이 실패합니다.
- 자동 만료 작업은 5분마다 확인하므로 실제 역할 해제는 만료 시각에서 최대 약 5분 늦을 수 있습니다.
