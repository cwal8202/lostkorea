#!/usr/bin/env bash
# 백업 복구 시험 — 서버 밖(R2)의 최신 백업을 받아 암호를 풀고, 임시 PostgreSQL 컨테이너에 넣어 표마다 건수를 운영 DB와 비교.
# 백업은 '되살려 봐야' 믿을 수 있음. backup_db.sh가 매월 1일 자동 실행, 설정 직후엔 손으로 한 번:
#   cd /root/lostkorea && scripts/restore_test.sh
# 운영 DB는 읽기만(건수 비교), 임시 컨테이너·파일은 끝나면 지움. 실패하면 0이 아닌 값으로 끝남.
set -uo pipefail
cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker-compose.prod.yml"
DIR=${BACKUP_DIR:-/root/lostkorea-backups}
PASS=${BACKUP_PASS_FILE:-/root/.lostkorea-backup-pass}
RCLONE_IMAGE=${RCLONE_IMAGE:-rclone/rclone:1.68.2}
PG_IMAGE=${PG_IMAGE:-postgres:16-alpine}
NAME=lostkorea-restore-test
TMP="$DIR/restore_tmp"
TABLES="lost_items metrics found_closures outcomes"

envval() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
KEY_ID=$(envval BACKUP_R2_ACCESS_KEY_ID); SECRET=$(envval BACKUP_R2_SECRET_ACCESS_KEY)
ENDPOINT=$(envval BACKUP_R2_ENDPOINT); BUCKET=$(envval BACKUP_R2_BUCKET)
if [ -z "$KEY_ID" ] || [ -z "$SECRET" ] || [ -z "$ENDPOINT" ] || [ -z "$BUCKET" ] || [ ! -s "$PASS" ]; then
  echo "복구 시험 불가: R2 설정(.env BACKUP_R2_*) 또는 암호 파일($PASS) 없음"; exit 2
fi
rclone() {
  docker run --rm -v "$DIR:/data" \
    -e RCLONE_CONFIG_R2_TYPE=s3 -e RCLONE_CONFIG_R2_PROVIDER=Cloudflare \
    -e RCLONE_CONFIG_R2_ACCESS_KEY_ID="$KEY_ID" -e RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$SECRET" \
    -e RCLONE_CONFIG_R2_ENDPOINT="$ENDPOINT" -e RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true \
    "$RCLONE_IMAGE" "$@"
}
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1; rm -rf "$TMP"; }
trap cleanup EXIT
cleanup; mkdir -p "$TMP" && chmod 700 "$TMP"

LATEST=$(rclone lsf "r2:$BUCKET/daily/" | sort | tail -1)
[ -n "$LATEST" ] || { echo "복구 시험 실패: R2에 백업이 없음"; exit 1; }
echo "[$(date '+%F %T')] 복구 시험: r2:$BUCKET/daily/$LATEST"
rclone copyto "r2:$BUCKET/daily/$LATEST" "/data/restore_tmp/$LATEST" -q || { echo "받기 실패"; exit 1; }
gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$PASS" -d "$TMP/$LATEST" > "$TMP/dump.sql.gz" \
  || { echo "암호 풀기 실패(암호 파일이 백업 때와 다름?)"; exit 1; }

docker run -d --name "$NAME" -e POSTGRES_PASSWORD="$(openssl rand -hex 16)" -e POSTGRES_USER=lostkorea \
  -e POSTGRES_DB=lostkorea "$PG_IMAGE" >/dev/null || { echo "임시 DB 시작 실패"; exit 1; }
for _ in $(seq 1 30); do docker exec "$NAME" pg_isready -U lostkorea -d lostkorea >/dev/null 2>&1 && break; sleep 2; done
gunzip -c "$TMP/dump.sql.gz" | docker exec -i "$NAME" psql -q -U lostkorea -d lostkorea -v ON_ERROR_STOP=1 >/dev/null \
  || { echo "임시 DB에 넣기 실패"; exit 1; }

count() {  # $1=restored|live $2=table
  local q="SELECT count(*) FROM $2"
  if [ "$1" = restored ]; then docker exec "$NAME" psql -At -U lostkorea -d lostkorea -c "$q" 2>/dev/null
  else $COMPOSE exec -T postgres psql -At -U lostkorea -d lostkorea -c "$q" 2>/dev/null; fi
}
FAIL=0
for t in $TABLES; do
  r=$(count restored "$t"); l=$(count live "$t")
  if [ -z "$r" ]; then echo "  $t: 백업에 표가 없음(운영 $l건)"; FAIL=1; else echo "  $t: 백업 $r건 / 운영 $l건"; fi
done
[ "$(count restored metrics)" -gt 0 ] 2>/dev/null || { echo "  metrics가 비어 있음"; FAIL=1; }
if [ "$FAIL" = 0 ]; then echo "복구 시험 통과"; else echo "복구 시험 실패"; exit 1; fi
