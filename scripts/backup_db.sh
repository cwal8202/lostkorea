#!/usr/bin/env bash
# PostgreSQL(분실물 신고) 일일 백업 — 서버에 14일 + 서버 밖(Cloudflare R2)에 암호화해 14일, 매월 1일엔 개인정보 없는 표를 12개월 보관.
# 실패하면 관리자에게 메일.
#
# 설치(서버, 수집이 끝난 뒤 새벽 4시 KST):
#   chmod +x scripts/backup_db.sh
#   (crontab -l 2>/dev/null; echo "0 4 * * * /root/lostkorea/scripts/backup_db.sh >> /root/lostkorea/backup.log 2>&1") | crontab -
#
# 왜 매일 '전체' 백업인가: 압축해 수십 KB~수십 MB라 몇 초면 끝나고, 파일 하나로 되살릴 수 있으며(증분은 기준+변경분이 다 있어야 함),
#   14일 지난 파일만 지우면 처리방침의 '삭제한 정보는 백업에 최대 14일' 약속이 그대로 지켜짐. 백업 하나가 1GB를 넘으면 다시 검토.
#
# 서버 밖 백업(R2) 설정 — 없으면 건너뜀(서버 백업은 그대로):
#   1) 암호 파일: openssl rand -base64 48 > /root/.lostkorea-backup-pass && chmod 600 /root/.lostkorea-backup-pass
#      → 내용을 비밀번호 관리자에 꼭 보관(서버를 잃으면 이 암호가 있어야 백업을 열 수 있음)
#   2) .env에 BACKUP_R2_ACCESS_KEY_ID · BACKUP_R2_SECRET_ACCESS_KEY · BACKUP_R2_ENDPOINT(https://<계정ID>.r2.cloudflarestorage.com)
#      · BACKUP_R2_BUCKET(lostkorea-backups) — 키 권한은 이 버킷 읽기·쓰기만
#   올리는 파일은 서버에서 AES-256으로 암호화(gpg) — 저장소 회사는 내용을 볼 수 없음. 도구는 rclone 공식 이미지(설치 없이 docker로).
#
# 복구: docs/DEPLOY.md '백업과 복구' — 서버 백업은 gunzip -c 파일 | psql, R2 백업은 scripts/restore_test.sh가 받아 푸는 순서 그대로.
#
# 참고: OpenSearch 색인은 공공데이터에서 다시 수집할 수 있어 백업하지 않음(.env는 비밀번호 관리자에 별도 보관).
set -uo pipefail
cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker-compose.prod.yml"
DIR=${BACKUP_DIR:-/root/lostkorea-backups}
KEEP=${BACKUP_KEEP_DAYS:-14}
ARCHIVE_KEEP=${BACKUP_ARCHIVE_KEEP_DAYS:-365}
PASS=${BACKUP_PASS_FILE:-/root/.lostkorea-backup-pass}
RCLONE_IMAGE=${RCLONE_IMAGE:-rclone/rclone:1.68.2}
STAMP=$(date +%Y%m%d_%H%M)

log() { echo "[$(date '+%F %T')] $*"; }
alert() {  # 관리자 메일(실패해도 백업 스크립트는 계속)
  $COMPOSE run --rm -T backend uv run python -m apps.ops alert "$1" "$2 ($(date '+%F %T')). 로그: /root/lostkorea/backup.log" || true
}
metric() {  # 익명 운영 통계 +n(주간 요약) — psql로 바로
  $COMPOSE exec -T postgres psql -U lostkorea -d lostkorea -q -c \
    "INSERT INTO metrics (day, kind, n) VALUES ('$(date +%F)', '$1', $2) ON CONFLICT (day, kind) DO UPDATE SET n = metrics.n + excluded.n" \
    >/dev/null 2>&1 || true
}
envval() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }

# ── 1. 서버 백업(전체) ──
mkdir -p "$DIR" && chmod 700 "$DIR"
F="$DIR/lostkorea_${STAMP}.sql.gz"
if $COMPOSE exec -T postgres pg_dump -U lostkorea lostkorea | gzip > "$F.tmp" && [ -s "$F.tmp" ]; then
  mv "$F.tmp" "$F" && chmod 600 "$F"
  log "백업 완료: $F ($(du -h "$F" | cut -f1))"
  metric backup_kb "$(( $(stat -c%s "$F") / 1024 ))"
else
  rm -f "$F.tmp"
  log "백업 실패"
  alert "DB 백업 실패" "backup_db.sh 실패"
  exit 1
fi
# -mtime +N은 'N+1일 이상'이라 +14면 15일 가까이 남았음 → KEEP-1로 14일을 넘기지 않게(처리방침 '14일')
find "$DIR" -maxdepth 1 \( -name '*.sql.gz' -o -name '*.gpg' \) -mtime +"$((KEEP - 1))" -delete   # 옛 findit_* 파일도 함께 정리

# ── 2. 서버 밖 백업(R2, 암호화) ──
KEY_ID=$(envval BACKUP_R2_ACCESS_KEY_ID); SECRET=$(envval BACKUP_R2_SECRET_ACCESS_KEY)
ENDPOINT=$(envval BACKUP_R2_ENDPOINT); BUCKET=$(envval BACKUP_R2_BUCKET)
if [ -z "$KEY_ID" ] || [ -z "$SECRET" ] || [ -z "$ENDPOINT" ] || [ -z "$BUCKET" ] || [ ! -s "$PASS" ]; then
  log "서버 밖 백업 건너뜀: R2 설정(.env BACKUP_R2_*) 또는 암호 파일($PASS) 없음"
  exit 0
fi
rclone() {  # rclone 공식 이미지 — 설정은 환경변수로만(파일에 키를 남기지 않음)
  docker run --rm -v "$DIR:/data" \
    -e RCLONE_CONFIG_R2_TYPE=s3 -e RCLONE_CONFIG_R2_PROVIDER=Cloudflare \
    -e RCLONE_CONFIG_R2_ACCESS_KEY_ID="$KEY_ID" -e RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$SECRET" \
    -e RCLONE_CONFIG_R2_ENDPOINT="$ENDPOINT" -e RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true \
    "$RCLONE_IMAGE" "$@"
}
encrypt() {  # $1 → $1.gpg (AES-256, 암호 파일)
  gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$PASS" \
    --symmetric --cipher-algo AES256 -o "$1.gpg" "$1" && chmod 600 "$1.gpg"
}

OFFSITE_OK=1
if encrypt "$F" && rclone copyto "/data/$(basename "$F").gpg" "r2:$BUCKET/daily/$(basename "$F").gpg" --s3-no-check-bucket -q; then
  log "서버 밖 백업 완료: r2:$BUCKET/daily/$(basename "$F").gpg"
  metric backup_offsite_ok 1
  rclone delete "r2:$BUCKET/daily/" --min-age "${KEEP}d" -q \
    || log "서버 밖 옛 백업 정리 실패(R2 자동 삭제 규칙이 있으면 그쪽에서 지워짐)"
else
  OFFSITE_OK=0
  log "서버 밖 백업 실패"
  alert "서버 밖 DB 백업 실패" "R2 올리기 실패 — 서버 백업은 됨"
fi
rm -f "$F.gpg"

# ── 3. 매월 1일: 개인정보 없는 표(통계·습득물 이탈 기록·수령 진행 익명 기록)만 12개월 보관 ──
if [ "$(date +%d)" = "01" ] && [ "$OFFSITE_OK" = 1 ]; then
  A="$DIR/archive_${STAMP}.sql.gz"
  if $COMPOSE exec -T postgres pg_dump -U lostkorea lostkorea -t metrics -t found_closures -t outcomes | gzip > "$A" \
     && [ -s "$A" ] && encrypt "$A" \
     && rclone copyto "/data/$(basename "$A").gpg" "r2:$BUCKET/archive/$(basename "$A").gpg" --s3-no-check-bucket -q; then
    log "월간 보관 완료: r2:$BUCKET/archive/$(basename "$A").gpg"
    rclone delete "r2:$BUCKET/archive/" --min-age "${ARCHIVE_KEEP}d" -q || true
  else
    log "월간 보관 실패"
    alert "월간 보관(통계·이탈 기록) 실패" "R2 archive 올리기 실패"
  fi
  rm -f "$A" "$A.gpg"
  # 매월 복구 시험 — R2에서 최신 백업을 받아 임시 DB에 넣어 봄
  scripts/restore_test.sh || alert "백업 복구 시험 실패" "restore_test.sh 실패"
fi
