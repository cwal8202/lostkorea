#!/usr/bin/env bash
# 매일 "최근 N일" 습득물 수집 + 재매칭 (cron에서 호출). 실패·데이터 정체는 관리자에게 메일.
#
# 왜 '어제분'이 아니라 최근 N일인가: 습득물은 습득일 이후 며칠에 걸쳐 등록된다.
#   실측(2026-09-29): 자정에 '어제분'을 조회하면 5건 → 하루 뒤 같은 날짜는 ~2,800건.
#   그래서 매일 최근 N일 창을 다시 조회하고, 수집기가 이미 색인된 항목은 건너뛴다(상세·임베딩은 새 항목만).
#   수집기는 500건마다 색인하므로 중간에 끊겨도 앞 배치는 남고, 다음 실행이 이어서 처리한다.
#
# 설치(서버, KST 자정):
#   chmod +x scripts/daily_collect.sh
#   (crontab -l 2>/dev/null; echo "0 0 * * * /root/lostkorea/scripts/daily_collect.sh >> /root/lostkorea/daily_collect.log 2>&1") | crontab -
cd "$(dirname "$0")/.."                 # repo 루트(/root/lostkorea)

# 로그가 끝없이 커지지 않게: 5MB를 넘으면 최근 2만 줄만 남김(같은 파일을 cron이 이어 쓰므로 내용만 교체)
LOG="$(pwd)/daily_collect.log"
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 5000000 ]; then
  tail -n 20000 "$LOG" > "$LOG.tmp" && cat "$LOG.tmp" > "$LOG"; rm -f "$LOG.tmp"
fi
COMPOSE="docker compose -f docker-compose.prod.yml"

DAYS=${COLLECT_DAYS:-7}                 # 조회 창(일). 늦게 등록되는 습득물까지 잡도록 넉넉히.
START=$(date -d "$DAYS days ago" +%Y%m%d)
END=$(date -d 'yesterday' +%Y%m%d)
echo "==== [$(date '+%F %T')] 최근 ${DAYS}일($START~$END) 수집 시작 ===="

# --max: 소스별 목록 상한(하루 ~1,700건/소스 × 7일 여유). 상세·임베딩은 새 항목에만 쓰임.
# --reconcile: 경찰 목록과 대조해 종결(주인 반환 등)된 습득물 삭제 + 바뀐 사진 반영 — 수집 창 + 오래된 구간 30일씩 돌아가며.
# --catch-up: 최근 90일 습득분 중 늦게 등록돼 빠진 것을 출처당 하룻밤 최대 N건 수집(30일씩 순환, 색인 60만 건 이상이면 멈춤,
#   공공 API가 거절(429)하면 그날 중단).
# run --rm: 백엔드 서비스 컨테이너(exec)가 아니라 별도 일회성 컨테이너에서 실행 → 수집 도중 자동배포가
# 백엔드를 재생성해도 끊기지 않음(2026-09-30: exec 방식이 01:24 배포로 중단돼 1시간 반 작업 유실).
if ! $COMPOSE run --rm -T backend \
  uv run python -m apps.collector.run \
    --source both --start "$START" --end "$END" \
    --rows 1000 --max 20000 --enrich --reconcile --catch-up 15000 --rematch; then
  echo "==== [$(date '+%F %T')] 수집 실패 ===="
  $COMPOSE run --rm -T backend uv run python -m apps.ops alert "일일 수집 실패" \
    "daily_collect.sh 실패 ($(date '+%F %T')). 로그: /root/lostkorea/daily_collect.log" || true
  exit 1
fi

# 수집은 성공했어도 새 데이터가 며칠째 없으면(공공 API 장애·쿼터 등) 알림
$COMPOSE run --rm -T backend uv run python -m apps.ops freshness --max-age-days 3 || true

# 디스크 사용률 경보 — 색인·로그·백업이 쌓여 디스크가 차면 DB·검색·수집이 함께 멈춘다
USE=$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')
if [ "${USE:-0}" -ge "${DISK_ALERT_PERCENT:-85}" ]; then
  $COMPOSE run --rm -T backend uv run python -m apps.ops alert "디스크 ${USE}% 사용 — 정리 필요" \
    "서버 디스크 사용률이 ${USE}%예요(기준 ${DISK_ALERT_PERCENT:-85}%). 확인: df -h /, docker system df, du -sh /root/lostkorea-backups" || true
fi

# 월요일엔 지난 7일 운영 요약(방문·신고·알림·오류·수집) 메일
if [ "$(date +%u)" = "1" ]; then
  $COMPOSE run --rm -T backend uv run python -m apps.ops weekly || true
fi

echo "==== [$(date '+%F %T')] 수집 완료 ===="
