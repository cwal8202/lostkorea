# LostKorea 배포 (단일 VPS + Docker Compose + HTTPS + 자동배포)

Hetzner CX33(8GB) 등 VPS 한 대에 **OpenSearch(nori) + PostgreSQL + 백엔드(FastAPI+웹) + Caddy**를
`docker-compose.prod.yml`로 함께 띄운다. 프론트는 백엔드가 같이 서빙하므로 별도 배포 불필요.

- **HTTPS**: DuckDNS(무료 서브도메인) + **Caddy**(리버스프록시)가 Let's Encrypt 인증서 자동 발급·갱신.
  `https://<도메인>` 접속. http→https 자동 리다이렉트. (도메인 미설정 시 `http://<서버IP>`로도 동작.)
- **자동배포(CI/CD)**: `main`에 push → GitHub Actions가 서버 SSH → `git pull` + 재빌드. (아래 9절)
- **최신 데이터**: cron이 매일 00:00 최근 7일 습득물을 다시 조회해 새 항목만 수집·색인·재매칭. (아래 8절)

> 시크릿은 서버의 `.env`(git 미포함). **OpenSearch·PostgreSQL은 외부 포트 노출 안 함** — Caddy(80/443)만 외부에 열림.

라이브: **https://lostkorea.com** (Cloudflare 프록시 경유 — 아래 'Cloudflare 앞단'). www·옛 주소(findit-lost.duckdns.org)는 새 주소로 영구 이동.

---

## 0. 서버 접속
```bash
ssh root@<서버IP>       # 콘솔이 준 IP / (SSH키 없으면 이메일로 온 root 비번)
```

## 1. Docker 설치 (Ubuntu)
```bash
curl -fsSL https://get.docker.com | sh
docker --version && docker compose version
```

## 2. 코드 받기
```bash
apt-get update && apt-get install -y git
git clone https://github.com/cwal8202/lostkorea.git /root/lostkorea
cd /root/lostkorea
```
> 자동배포 워크플로가 `/root/lostkorea` 경로를 가정하므로 이 위치에 클론한다.

## 3. 시크릿(.env) 작성
```bash
cp .env.example .env
nano .env      # 아래 키 채우기
```
- `GEMINI_API_KEY` (임베딩·grading·Vision)
- `DATA_GO_KR_SERVICE_KEY_DECODED` (수집기)
- `KAKAO_REST_API_KEY` (지오코딩, 선택)
- **`POSTGRES_PASSWORD`** (필수 — 없으면 뜨지 않음. 생성: `openssl rand -hex 24`)
- **`DOMAIN`** (HTTPS 주소, 쉼표로 여러 개 가능 — 모두 인증서 자동 발급) — 비우면 http만.
- **`PUBLIC_BASE_URL`**(대표 주소: 메일 링크·canonical·sitemap) · **`REDIRECT_HOSTS`**(옛 주소 → 대표 주소 영구 이동) — 아래 '주소 바꾸기'.
- 이메일 알림 쓰면: `NOTIFIER_CHANNEL=email`, `SMTP_USER`, `SMTP_PASSWORD`, `NOTIFY_EMAIL_TO`
- (`OPENSEARCH_URL`·`DATABASE_URL`은 compose가 자동 주입하므로 건드리지 말 것)

> **DuckDNS 준비**: duckdns.org에서 서브도메인 생성 → 서버 IP 지정 → 그 도메인을 `.env` `DOMAIN`에. 운영 중 주소를 바꿀 땐 아래 '주소 바꾸기'.

## 4. 기동 (빌드 + 실행)
```bash
docker compose -f docker-compose.prod.yml up -d --build
# OpenSearch가 healthy 될 때까지 대기(1~2분). 상태:
docker compose -f docker-compose.prod.yml ps
```
컨테이너 4종이 뜬다: `lostkorea-opensearch` · `lostkorea-postgres` · `lostkorea-backend` · `lostkorea-caddy`.

## 5. 색인 시드 (캐시된 2,000건 즉시 색인 — 임베딩 비용 0)
```bash
docker compose -f docker-compose.prod.yml exec backend uv run python eval/index_opensearch.py
```
→ 인덱스 자동 생성 + 문서수 출력되면 성공.

## 6. 접속 확인
- 브라우저에서 **https://<도메인>** → LostKorea 화면 (🔒 유효 인증서 확인).
- `curl https://<도메인>/health` → `{"status":"ok",...}`
- (도메인 미설정 시 `http://<서버IP>`)

## 7. 방화벽 (포트 열기)
- **Hetzner Cloud Firewall**(콘솔) 또는 서버 ufw로 **22(SSH), 80(HTTP), 443(HTTPS)** 인바운드 허용.
  (Caddy가 80으로 인증서 검증 + https 리다이렉트, 443으로 실서비스. OpenSearch/Postgres 포트는 열지 않는다.)
```bash
ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw --force enable   # ufw 쓸 경우
```

## 8. 최신 데이터 유지 — 매일 자동 수집 (cron)
`scripts/daily_collect.sh`가 **최근 7일** 습득물을 다시 조회해 새 항목만 수집·색인하고, 이어서 재매칭한다. cron에 등록:
```bash
timedatectl set-timezone Asia/Seoul     # 서버 시각을 KST로(현재 운영 서버는 KST)
chmod +x scripts/daily_collect.sh
(crontab -l 2>/dev/null; echo "0 0 * * * /root/lostkorea/scripts/daily_collect.sh >> /root/lostkorea/daily_collect.log 2>&1") | crontab -
```
- 매일 00:00(KST) 실행 → 최근 7일 창 `--enrich --rematch`.
- **왜 7일 창인가**: 습득물은 습득일 이후 며칠에 걸쳐 등록된다. '어제분'만 자정에 조회하면 5건 안팎만 잡히고
  (하루 뒤엔 같은 날짜가 ~2,800건) 새 데이터 99%를 놓친다. 창은 `COLLECT_DAYS` 환경변수로 변경 가능.
- **비용**: 목록 조회는 가벼움(7일치 ~1만8천 건 ≈ 1분). 이미 색인된 항목은 `_mget`으로 걸러 **상세 조회(항목당 1콜, 일 10만 한도)와
  임베딩은 새 항목에만** — 평소 하루 3~6천 건. 상세는 4건 동시 조회(`COLLECT_WORKERS`), 실패분은 다음 날 재시도.
  빈 날도 재매칭은 진행.
- **색인 상한**: 습득일 185일(`FOUND_RETENTION_DAYS`)이 지난 습득물은 수집 때마다 삭제 — 소유자가 찾아갈 수 있는 기간(6개월)이 지났고,
  안 지우면 벡터 메모리·디스크가 계속 커진다.
- 로그 확인: `tail -50 /root/lostkorea/daily_collect.log` (재매칭 대상·최고 grade·신규 매칭 건수가 찍힘)
- 즉시 한 번 돌려보려면: `bash scripts/daily_collect.sh`
- 수집은 **별도 일회성 컨테이너**(`docker compose run --rm backend …`)에서 실행 → 수집 중 자동배포가 백엔드를 재생성해도 끊기지 않음.
  (예전 `exec` 방식은 새벽 배포에 수집이 중단돼 작업이 유실됨. 첫 7일 창 실행은 서버에서 ~4.5시간.)
- 진행 확인: `docker ps | grep backend-run` (수집 컨테이너) · `tail -f /root/lostkorea/daily_collect.log`
- 수집기는 **500건마다 색인** — 중간에 끊겨도 앞 배치는 남고 다음 실행이 '이미 색인' 스킵으로 이어서 처리.
- **실패·정체 알림**: 수집이 실패하면, 또는 가장 최근 습득일이 3일 넘게 지나면(새 데이터가 안 들어옴) 관리자
  (`NOTIFY_EMAIL_TO`, 없으면 `SMTP_USER`)에게 메일(`apps/ops.py`). 수동 점검:
  `docker compose -f docker-compose.prod.yml run --rm -T backend uv run python -m apps.ops freshness --max-age-days 3`

## 8-1. DB 일일 백업 (cron)
`scripts/backup_db.sh` — PostgreSQL(분실물 신고)을 `pg_dump | gzip`으로 `/root/lostkorea-backups/`에 저장, 14일 보관, 실패 시 메일.
```bash
chmod +x scripts/backup_db.sh
(crontab -l 2>/dev/null; echo "0 4 * * * /root/lostkorea/scripts/backup_db.sh >> /root/lostkorea/backup.log 2>&1") | crontab -
```
- 복구: `gunzip -c /root/lostkorea-backups/lostkorea_YYYYMMDD_HHMM.sql.gz | docker compose -f docker-compose.prod.yml exec -T postgres psql -U lostkorea lostkorea`
- 백업은 같은 서버에 남음(서버 장애엔 무력) → 중요해지면 외부 저장소로 복사. OpenSearch는 재수집으로 복구 가능해 백업 제외.

## 8-2. DB 비밀번호 (필수)
`.env`의 `POSTGRES_PASSWORD`는 **필수**다 — compose에 기본값이 없어서 비어 있으면 아예 뜨지 않는다(공개 레포에 적힌 기본값으로
조용히 뜨는 일 방지). 운영 서버는 2026-11-09에 무작위 값으로 교체했다(옛 기본값은 네트워크 접속 거부 확인).
다시 바꿀 때는 **수집 시간(00:00~01:00)을 피해서** — Postgres는 처음 만들 때만 이 값을 읽으므로 DB 안에서 바꾸고 `.env`를 맞춘다:
```bash
P=$(openssl rand -hex 24)
docker compose -f docker-compose.prod.yml exec -T postgres psql -U lostkorea -c "ALTER USER lostkorea WITH PASSWORD '$P'"
sed -i '/^POSTGRES_PASSWORD=/d' .env && echo "POSTGRES_PASSWORD=$P" >> .env && unset P
docker compose -f docker-compose.prod.yml up -d postgres backend    # DATABASE_URL 갱신(수 초 중단)
curl -s localhost/health/deep
```
※ 컨테이너 안(127.0.0.1)에서는 비밀번호 없이 접속된다(공식 이미지의 로컬 신뢰 설정) — 확인은 다른 컨테이너에서 네트워크로.

## 9. 자동배포 (GitHub Actions)
`.github/workflows/deploy.yml` — **`main`에 push되면** GitHub Actions가 서버에 SSH 접속해 자동 재배포한다.
```
main push → [test] pytest 통과 → [deploy] git pull --ff-only → docker compose up -d --build backend caddy
            → caddy reload(Caddyfile 변경 반영) → image prune
```
- **테스트가 실패하면 배포하지 않는다**(깨진 커밋이 운영에 나가지 않게).
- 접속정보는 **레포 Settings > Secrets**에 저장(코드에 노출 안 함):
  - `SSH_HOST` : 서버 IP
  - `SSH_KEY`  : 서버 접속용 SSH 개인키 전체(`-----BEGIN ...-----` 포함)
- 공개 레포라 Actions **무료·무제한**. Docker 레이어 캐시로 재배포 ~십수 초.
- 수동 트리거도 가능(Actions 탭 > Deploy > Run workflow / `workflow_dispatch`).

## 9-1. 감시·알림 (운영자가 아무것도 안 봐도 문제를 알게)
| 무엇 | 어떻게 | 어디서 |
|---|---|---|
| 서비스 다운(서버·검색엔진·DB) | `.github/workflows/uptime.yml`이 30분마다 `/`·`/health/deep` 확인, 3번 연속 실패 시 워크플로 실패 → **GitHub가 메일** | 바깥(GitHub) |
| 앱 오류(500·신고 처리 실패) | `ops.report_error` → 관리자 메일(같은 오류 1시간 1통) | 서버 |
| 수집 실패·데이터 정체 | `daily_collect.sh` → `apps.ops alert`/`freshness` | 서버 cron |
| 주간 요약(방문·신고·알림·오류·수집) | 월요일 수집 후 `apps.ops weekly` | 서버 cron |
- 수동 확인: `docker compose -f docker-compose.prod.yml run --rm -T backend uv run python -m apps.ops weekly --days 30`
- 통계는 `metrics` 테이블(일·종류별 횟수만, 개인정보 없음). 봇 접속은 방문 수에서 제외.
- **공개 레포는 60일간 커밋이 없으면 GitHub가 예약 워크플로(uptime)를 멈춘다** → Actions 탭에서 다시 켜기.

> 자동배포는 `backend`·`caddy`만 재빌드(코드/웹 변경분). OpenSearch·Postgres는 데이터 보존 위해 건드리지 않음.
> DB 스키마 변경(컬럼 추가)은 백엔드 기동 시 `store.init()`이 `ADD COLUMN IF NOT EXISTS`로 자동 적용 — 수동 마이그레이션 불필요.

> ⚠ **서버에서 레포 파일을 직접 고치지 말 것** — 권한(`chmod`)만 바꿔도 git은 로컬 변경으로 보고,
> 그 파일이 다음 커밋에 포함되면 `git pull --ff-only`가 실패해 **자동배포가 멈춘다**. 변경은 레포에 커밋(실행권한은
> `git update-index --chmod=+x`). 이미 생겼다면 `git status`로 확인 후 `git checkout -- <파일>`.

---

## 운영 메모
- 로그: `docker compose -f docker-compose.prod.yml logs -f backend`
- 재시작: `docker compose -f docker-compose.prod.yml restart backend`
- 수동 코드 갱신(자동배포 대신): `git pull && docker compose -f docker-compose.prod.yml up -d --build backend caddy`
- 중지(과금 종료 전, 데이터 볼륨 유지): `docker compose -f docker-compose.prod.yml down`
- 완전 삭제: 위 + `docker volume rm lostkorea_os-data lostkorea_pg-data lostkorea_caddy-data lostkorea_caddy-config`
- 인증서 갱신은 Caddy가 자동. `lostkorea_caddy-data` 볼륨에 인증서 영속(재기동해도 재발급 안 함).
- **실시간 진행(SSE)**: 웹 검색은 `POST /lost-items/stream`·`/lost-items/image/stream`(`text/event-stream`)을 씀.
  백엔드가 `X-Accel-Buffering: no`·`Cache-Control: no-cache`를 보냄. 라이브에서 단계가 한꺼번에 뜨면(프록시 버퍼링)
  `Caddyfile`의 `reverse_proxy backend:8000` 블록에 `flush_interval -1` 추가 후 caddy 재기동.
- 스트림 점검(저장·메일 없는 비신고 입력): Windows 셸은 한글 인코딩이 깨지므로 UTF-8 JSON 파일로 보낼 것.
  ```bash
  printf '{"text":"오늘 날씨 어때?"}' > q.json
  curl -N -X POST https://<도메인>/lost-items/stream -H "Content-Type: application/json" --data-binary @q.json
  ```
  → `event: extract` → `event: result`(status `invalid`)가 오면 정상.

## 보안·남용 방지 (운영 값)
- **레이트리밋·일일 AI 상한**: `.env`로 조정(`LIMIT_REPORT_10MIN`·`LIMIT_IMAGE_DAY`·`DAILY_AI_BUDGET` 등, `apps/api/config.py`).
  프로세스 메모리 카운터라 **uvicorn 워커를 늘리면** 공유 저장소(Redis 등)로 바꿔야 한다. 백엔드 재시작 시 초기화.
- **요청 본문 상한**: Caddy `request_body max_size 10MB`.
- **이메일**: 확인(double opt-in)된 주소에만 알림. 확인 링크는 `PUBLIC_BASE_URL` 기준(기본 라이브 도메인).

## 개인정보·로그
- **보유 기간**: 알림 기간 종료 + `RETENTION_DAYS`(기본 30)일이 지난 신고는 매일 재매칭 전에 자동 삭제.
  `/privacy`(개인정보처리방침)에 같은 기간을 적어 두었으니 값을 바꾸면 **방침 문구도 같이** 고칠 것.
- **컨테이너 로그**: backend·caddy·postgres는 10MB × 3개로 회전(`x-logging`). 백엔드 로그엔 내부 프록시 IP만 남는다.
- **수집 로그**(`daily_collect.log`): 5MB를 넘으면 수집 시작 때 최근 2만 줄만 남김. **디스크 85%↑**(`DISK_ALERT_PERCENT`)면 관리자 메일.

## 웹사이트·브랜드
- 페이지는 `apps/api/site.py`가 언어별(한/영)로 서버에서 그린다: 템플릿 `apps/web/*.html` + 공통 머리글·바닥글
  `apps/web/partials/` + 문구 사전(`T`·`FAQ`). 정적 자산은 내용 해시를 `?v=`로 붙여 배포 즉시 새 파일을 받는다.
- 서비스 이름은 `BRAND_NAME`(기본 `LostKorea`) 하나로 화면·메일·OG에 반영. 로고 글자(Lost**Korea**)는 템플릿에 직접 있음.
- 내부 이름도 2026-11-09에 findit → lostkorea로 이전(아래 '이름 이전 기록').
- 폰트: Pretendard(SIL OFL, `static/fonts/pretendard/`)를 자체 호스팅 — 외부 CDN에 방문자 IP를 넘기지 않고, 차단 지역에서도 동작.

## AI 모델·비용 설정 (.env)
| 변수 | 기본값 | 뜻 |
|---|---|---|
| `GEMINI_GRADE_MODEL` | `gemini-3.6-flash` | 추출·노선·판정·사진에 쓰는 모델 |
| `GEMINI_FALLBACK_MODEL` | `gemini-3.8-flash` | 기본 모델이 404(종료·차단)면 자동 전환 + 관리자 메일. 재시작하면 기본 모델부터 다시 시도 |
| `GEMINI_THINKING_LEVEL` | `minimal` | 숨은 thinking 토큰(출력 요금). 실측상 꺼도 품질 동일·응답 16.9→6.3초. 모델이 거부하면 low→생략 자동 조정 |
| `AI_PRICE_IN_PER_M` · `AI_PRICE_OUT_PER_M` | `0.75` · `3.75` | 주간 요약의 AI 비용 추정 단가(USD/100만 토큰). 2027-01-01 Gemini 인상 시 갱신 |
| `DAILY_AI_BUDGET` | `1500` | 하루 AI 사용 상한(글 신고 1 · 사진 3 · 검색 API 1~2 단위) — 넘으면 429 |
- 비용 실측·모델 선택 메모는 [LAUNCH.md 4](LAUNCH.md) 참고.

## 주소 바꾸기 (2026-11-09 실제로 한 것: findit-lost.duckdns.org → lostkorea.com)
1. 새 주소의 DNS A 레코드를 서버 IP로(`@`·`www` 두 줄. DuckDNS면 IP를 **서버 IP**로 — 기본값은 접속한 PC의 IP).
2. 서버 `.env`(운영 값):
   ```bash
   DOMAIN=lostkorea.com, www.lostkorea.com, findit-lost.duckdns.org   # 모두 인증서 발급
   PUBLIC_BASE_URL=https://lostkorea.com                              # 대표 주소
   REDIRECT_HOSTS=www.lostkorea.com,findit-lost.duckdns.org           # 대표 주소로 영구 이동(GET 301 / 그 외 308)
   ```
   ※ 채팅·문서에서 복사하면 `www.` 주소가 `[www…](https://…)` 링크 모양으로 바뀌어 보일 수 있다 — 붙여 넣은 뒤
   `grep -E '^(DOMAIN|PUBLIC_BASE_URL|REDIRECT_HOSTS)=' .env`로 실제 값 확인.
3. `docker compose -f docker-compose.prod.yml up -d backend caddy` → 새 주소 https 확인 → 옛 주소가 새 주소로 넘어가는지 확인.
4. `.github/workflows/uptime.yml`의 `BASE`를 새 주소로, Search Console·네이버에 새 주소 등록.
- 서버 안 점검(`curl localhost/...`)·IP 접속은 목록에 없으므로 이동하지 않는다.

## Cloudflare 앞단 (2026-11-09~)
도메인 `lostkorea.com`은 Cloudflare Registrar에서 구입(원가 연 $11.17, 2027-11-09 만료, 자동 갱신). DNS도 Cloudflare.
- **DNS**: A `@`·`www` → 서버 IP, **Proxied**(주황 구름). 문제가 생기면 DNS only(회색)로 바꾸면 곧바로 서버 직접 접속.
- **SSL/TLS 모드: Full (strict)** — Cloudflare↔서버 구간도 암호화 + 서버 인증서 검증(서버는 Caddy가 받은 Let's Encrypt 인증서).
  Flexible이면 Cloudflare가 서버에 HTTP로 붙고 Caddy가 HTTPS로 돌려보내 무한 이동이 된다.
- **Always Use HTTPS: 끔** — 인증서 발급·갱신 확인 요청이 HTTP로 오는데 Cloudflare가 이를 HTTPS로 돌리면 갱신이 실패할 수
  있다(실패하면 90일 뒤 서버 인증서 만료 → strict 모드라 사이트 전체 526). HTTP → HTTPS 이동은 Caddy가 한다(308).
- **방문자 IP**: 프록시를 켜면 서버에 붙는 건 Cloudflare 서버라, 요청 한도가 방문자 대신 Cloudflare IP 몇 개로 묶인다.
  `apps/api/limits.py`가 접속 IP가 Cloudflare 대역일 때만 `CF-Connecting-IP`를 쓴다(직접 접속자의 위조는 무시).
  Cloudflare가 대역을 바꾸면(cloudflare.com/ips) `CLOUDFLARE_NETS`를 갱신.
- 첫 인증서: 프록시를 켠 상태에서 발급 성공(Caddy 로그 'certificate obtained successfully' — lostkorea.com·www 각 1건).
- 옛 주소(findit-lost.duckdns.org)는 Cloudflare를 거치지 않고 서버 IP를 그대로 가리킨다 → 공격자가 Cloudflare를 우회할 수 있음.
  DDoS 방어를 제대로 쓰려면 옛 주소를 정리하고 서버 방화벽을 Cloudflare 대역만 허용으로(이후 강화).

## 이름 이전 기록 (2026-11-09, findit → lostkorea)
서비스명 변경(FIND-82)에 이어 내부 이름도 옮겼다(FIND-83). Docker 볼륨은 이름을 바꿀 수 없어 **새 볼륨으로 복사**했다.
**실행: 2026-11-09 20:31~20:32(약 1분)**, `scripts/migrate_rename.sh` — 신고 38 → 38, 색인 170,581 → 170,581, 볼륨 4개 파일 수·바이트
일치(검색 색인 193개·3.28GB), `/health/deep` 정상.
1. DB 백업(`pre-rename_*.sql.gz`) → 백엔드 중지 → 임시 슈퍼유저로 `ALTER DATABASE findit RENAME TO lostkorea`,
   `ALTER ROLE findit RENAME TO lostkorea`(SCRAM 비밀번호는 이름 변경에도 유지) → 임시 사용자 삭제
2. 옛 스택 `down`(볼륨 유지) → `/root/findit` → `/root/lostkorea`, 백업 폴더도 `/root/lostkorea-backups`로 이동
3. 새 볼륨 `lostkorea_{os,pg,caddy}-data`·`lostkorea_caddy-config`를 compose 라벨과 함께 만들고 `cp -a`로 복사, 크기 비교
4. 이미지 태그 `findit/*` → `lostkorea/*`, 새 스택 기동(`name: lostkorea`) → DB·색인 건수·`/health/deep`·인증서 재사용 확인
5. 옛 경로 `/root/findit` → 새 폴더 심볼릭 링크 — cron(`/root/findit/scripts/…`)은 이 링크로 그대로 동작(경로 갱신은 선택)
- **옛 볼륨 `findit_findit-*`는 되돌리기용으로 남겨 둠** — 한동안 문제없으면 삭제:
  `docker volume rm findit_findit-os-data findit_findit-pg-data findit_findit-caddy-data findit_findit-caddy-config`
  (`findit_findit-data`는 현재 구성에서 쓰지 않는 예전 볼륨 — 내용 확인 후 결정)

## 이후 강화(선택)
- **React/Vercel 프론트 분리**: 분리 시 FastAPI에 CORS 허용 오리진 추가.
- **관측성**: 에러 추적(Sentry 등)·외부 헬스 체크, 백업 외부 복제.
- **이미지 임베딩(CLIP) 레인**: RAM 여유 있는 인스턴스에서 image-to-image KNN 추가(현재는 Gemini 사진 대조로 대체).
