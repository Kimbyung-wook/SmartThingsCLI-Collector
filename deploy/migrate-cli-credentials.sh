#!/usr/bin/env bash
# SmartThings CLI 자격증명을 로그인한 사용자에게서 서비스 계정 stcollector 로 옮긴다.
#   sudo bash deploy/migrate-cli-credentials.sh [--from-user 사용자] [--force]
#
# 사용자가 터미널에서 직접 실행한다. 자격증명 내용은 출력하거나 로그에 남기지 않는다.
# (set -x 금지, 내용을 표준 출력으로 보내는 명령 금지)
#
# 다시 실행해도 안전하다.
#   - 이미 옮겼으면 대상의 소유자와 권한만 맞추고 끝낸다.
#   - 원본과 대상이 다르면 멈춘다. 대상이 더 최신일 수 있어서다. 원본으로 덮어쓰려면 --force.
set -euo pipefail
umask 077

SERVICE_USER=stcollector
CLI_HOME=/var/lib/stcollector/cli
CRED_DIR_PARTS=(.local share @smartthings cli)
CRED_NAME=credentials.json

die() {
    echo "오류: $*" >&2
    exit 1
}

FROM_USER=homesensor
FORCE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --from-user)
            [[ $# -ge 2 ]] || die "--from-user 뒤에 사용자 이름이 필요합니다."
            FROM_USER="$2"
            shift 2
            ;;
        --force)
            FORCE=1
            shift
            ;;
        *)
            die "알 수 없는 옵션: $1"
            ;;
    esac
done

[[ $EUID -eq 0 ]] || die "sudo 로 실행하세요."
id "$SERVICE_USER" &>/dev/null || die "$SERVICE_USER 계정이 없습니다. 먼저 deploy/install.sh 를 실행하세요."
[[ -d "$CLI_HOME" && ! -L "$CLI_HOME" ]] || die "$CLI_HOME 이 없습니다. 먼저 deploy/install.sh 를 실행하세요."

FROM_HOME="$(getent passwd "$FROM_USER" | cut -d: -f6 || true)"
[[ -n "$FROM_HOME" ]] || die "사용자를 찾을 수 없습니다: $FROM_USER"

REL_DIR="$(IFS=/; echo "${CRED_DIR_PARTS[*]}")"
SRC="$FROM_HOME/$REL_DIR/$CRED_NAME"
DST_DIR="$CLI_HOME/$REL_DIR"
DST="$DST_DIR/$CRED_NAME"

# root 로 쓰는 경로에 심볼릭 링크가 끼어 있으면 다른 파일을 덮어쓸 수 있으므로 멈춘다.
[[ ! -L "$SRC" ]] || die "원본이 심볼릭 링크입니다: $SRC"
path="$CLI_HOME"
for part in "${CRED_DIR_PARTS[@]}" "$CRED_NAME"; do
    path="$path/$part"
    [[ ! -L "$path" ]] || die "대상 경로에 심볼릭 링크가 있습니다: $path"
done

ensure_dst_permissions() {
    local dir="$CLI_HOME" part
    install -d -m 700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$dir"
    for part in "${CRED_DIR_PARTS[@]}"; do
        dir="$dir/$part"
        install -d -m 700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$dir"
    done
    if [[ -e "$DST" ]]; then
        chown "$SERVICE_USER:$SERVICE_USER" "$DST"
        chmod 600 "$DST"
    fi
}

if [[ ! -e "$SRC" ]]; then
    if [[ -f "$DST" ]]; then
        ensure_dst_permissions
        echo "이미 옮겨져 있습니다: $DST (소유자와 권한을 확인했습니다)"
        exit 0
    fi
    die "원본도 대상도 없습니다. README 의 'CLI 로그인과 재로그인' 절차로 로그인하세요."
fi

[[ -f "$SRC" && -s "$SRC" ]] || die "원본이 일반 파일이 아니거나 비어 있습니다: $SRC"
[[ "$(stat -c %U "$SRC")" == "$FROM_USER" ]] || die "원본 소유자가 $FROM_USER 가 아닙니다: $SRC"
# 형식만 검사한다. 오류 메시지에 내용이 섞이지 않도록 출력은 모두 버린다.
# -I: 현재 폴더의 파일이 import 되지 않게 한다.
python3 -I -c 'import json, sys; json.load(sys.stdin)' <"$SRC" >/dev/null 2>&1 \
    || die "원본이 올바른 JSON 이 아닙니다: $SRC"

if [[ -e "$DST" ]] && ! cmp -s "$SRC" "$DST"; then
    [[ $FORCE -eq 1 ]] || die "대상이 이미 있고 원본과 다릅니다. 대상이 더 최신일 수 있습니다. 원본으로 덮어쓰려면 --force 를 붙이세요."
    if systemctl is-active --quiet stcollector; then
        die "stcollector 가 실행 중입니다. 덮어쓰기 전에 sudo systemctl stop stcollector 를 실행하세요."
    fi
fi

ensure_dst_permissions

TMP="$DST_DIR/.$CRED_NAME.tmp.$$"
trap 'rm -f -- "$TMP"' EXIT
install -m 600 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SRC" "$TMP"
cmp -s "$SRC" "$TMP" || die "복사본이 원본과 다릅니다. 원본은 지우지 않았습니다."
mv -fT -- "$TMP" "$DST"
trap - EXIT

# 정전에 대비해 복사본을 디스크에 기록한 뒤에 원본을 지운다.
sync

cmp -s "$SRC" "$DST" || die "대상이 원본과 다릅니다. 원본은 지우지 않았습니다."
[[ "$(stat -c '%U:%G %a' "$DST")" == "$SERVICE_USER:$SERVICE_USER 600" ]] \
    || die "대상의 소유자나 권한이 예상과 다릅니다. 원본은 지우지 않았습니다."

# microSD 는 덮어쓰기가 같은 셀에 된다는 보장이 없어 shred 는 최선의 시도일 뿐이다.
shred -u -- "$SRC" 2>/dev/null || rm -f -- "$SRC"
[[ ! -e "$SRC" ]] || die "원본을 지우지 못했습니다: $SRC"
sync

echo "옮겼습니다: $DST (소유자 $SERVICE_USER, 파일 600, 폴더 700)"
echo "원본을 지웠습니다: $SRC"
echo "다음 단계: stcollector check"
