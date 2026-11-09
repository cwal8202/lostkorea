#!/usr/bin/env bash
# 서버 이전: findit → lostkorea (DB·사용자·컨테이너·볼륨·경로). 실패 시 즉시 중단. 옛 볼륨은 지우지 않음(되돌리기용).
# 1회용(2026-11-09). 서버에서 git pull 하기 **전에** 실행 — 스크립트가 옛 스택을 내린 뒤 직접 pull 한다.
#   curl -fsSL https://raw.githubusercontent.com/cwal8202/findit/main/scripts/migrate_rename.sh -o /root/migrate_rename.sh
#   bash /root/migrate_rename.sh </dev/null
# 순서: 점검 → DB 백업 → DB·사용자 이름 변경 → 옛 스택 down → 폴더 이동(+옛 경로 링크) → git pull →
#       새 볼륨에 복사(파일 수·바이트 비교) → 이미지 태그 → 새 스택 기동 → 건수 비교.
set -euo pipefail
OLD=/root/findit; NEW=/root/lostkorea
TS=$(date +%Y%m%d_%H%M)
fingerprint() {  # 볼륨 내용 지문: 파일 수 + 전체 바이트
  docker run --rm -v "$1":/d:ro --entrypoint sh postgres:16-alpine -c \
    'echo "$(find /d -type f | wc -l) files, $(find /d -type f -exec stat -c %s {} + | awk "{s+=\$1} END{print s+0}") bytes"'
}

echo "[0] 사전 점검 $(date '+%F %T')"
[ -d "$OLD" ] && [ ! -L "$OLD" ] || { echo "옛 폴더 없음(또는 이미 링크) — 중단"; exit 1; }
[ ! -e "$NEW" ] || { echo "새 폴더가 이미 있음 — 중단"; exit 1; }
if docker ps --format '{{.Names}}' | grep -q backend-run; then echo "수집 중 — 중단"; exit 1; fi
cd "$OLD"
OC="docker compose -f docker-compose.prod.yml"
grep -q '^POSTGRES_PASSWORD=' .env || { echo ".env 비밀번호 없음 — 중단"; exit 1; }
ROWS_BEFORE=$($OC exec -T postgres psql -U findit -d findit -tAc "select count(*) from lost_items")
OS_BEFORE=$($OC exec -T backend curl -s http://opensearch:9200/found_items/_count | grep -o '"count":[0-9]*' | cut -d: -f2)
echo "    신고 ${ROWS_BEFORE}건, 색인 ${OS_BEFORE}건"

echo "[1] DB 백업"
$OC exec -T postgres pg_dump -U findit findit | gzip > "/root/findit-backups/pre-rename_${TS}.sql.gz"
[ -s "/root/findit-backups/pre-rename_${TS}.sql.gz" ] && ls -l "/root/findit-backups/pre-rename_${TS}.sql.gz"

echo "[2] DB·사용자 이름 변경(findit → lostkorea)"
$OC stop backend caddy
$OC exec -T postgres psql -U findit -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE ROLE lk_tmp SUPERUSER LOGIN"
$OC exec -T postgres psql -U lk_tmp -d postgres -v ON_ERROR_STOP=1 -q -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='findit' OR datname='findit'" >/dev/null
$OC exec -T postgres psql -U lk_tmp -d postgres -v ON_ERROR_STOP=1 -q -c "ALTER DATABASE findit RENAME TO lostkorea"
$OC exec -T postgres psql -U lk_tmp -d postgres -v ON_ERROR_STOP=1 -q -c "ALTER ROLE findit RENAME TO lostkorea"
$OC exec -T postgres psql -U lostkorea -d lostkorea -v ON_ERROR_STOP=1 -q -c "DROP ROLE lk_tmp"
echo "    $($OC exec -T postgres psql -U lostkorea -d lostkorea -tAc "select current_user||' / '||current_database()||' / 신고 '||(select count(*) from lost_items)")"

echo "[3] 옛 스택 내리기(볼륨 유지)"
$OC down 2>&1 | tail -3

echo "[4] 폴더 이동 + 옛 경로 링크(cron 호환)"
mv "$OLD" "$NEW"
mv /root/findit-backups /root/lostkorea-backups
ln -s "$NEW" "$OLD"
cd "$NEW"
git pull --ff-only -q
git log --oneline -1
NC="docker compose -f docker-compose.prod.yml"
$NC config -q && echo "    새 compose 설정 검증 통과"

echo "[5] 새 볼륨 생성·데이터 복사"
CV=$(docker compose version --short)
for v in os-data pg-data caddy-data caddy-config; do
  docker volume create --label com.docker.compose.project=lostkorea --label com.docker.compose.volume=$v \
    --label com.docker.compose.version="$CV" "lostkorea_$v" >/dev/null
  docker run --rm -v "findit_findit-$v":/from:ro -v "lostkorea_$v":/to --entrypoint sh postgres:16-alpine -c 'cp -a /from/. /to/'
  a=$(fingerprint "findit_findit-$v"); b=$(fingerprint "lostkorea_$v")
  echo "    $v: $a → $b"
  [ "$a" = "$b" ] || { echo "내용 불일치 — 중단"; exit 1; }
done

echo "[6] 이미지 이름"
docker tag findit/opensearch-nori:2.17.1 lostkorea/opensearch-nori:2.17.1
docker tag findit/backend:latest lostkorea/backend:latest

echo "[7] 새 스택 기동"
$NC up -d --build 2>&1 | tail -6
for i in $(seq 1 60); do curl -sf localhost/health/deep >/dev/null 2>&1 && break; sleep 3; done
echo "    health: $(curl -s localhost/health/deep)"
ROWS_AFTER=$($NC exec -T postgres psql -U lostkorea -d lostkorea -tAc "select count(*) from lost_items")
OS_AFTER=$($NC exec -T backend curl -s http://opensearch:9200/found_items/_count | grep -o '"count":[0-9]*' | cut -d: -f2)
echo "    신고 ${ROWS_BEFORE} → ${ROWS_AFTER}, 색인 ${OS_BEFORE} → ${OS_AFTER}"
docker ps --format '    {{.Names}} {{.Status}}'
echo "[완료] $(date '+%F %T')"
