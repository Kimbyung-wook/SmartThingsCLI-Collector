#!/usr/bin/env bash
# Raspberry Pi OS (Bookworm 이상)에 stcollector 설치/업데이트.
#   sudo ./deploy/install.sh [--cli-binary /경로/smartthings]
# 다시 실행하면 코드와 CLI 바이너리만 교체하고, 기존 설정·DB·토큰·CLI 자격증명은 그대로 둔다.
# --cli-binary 를 생략하면 sudo 를 실행한 사용자의 ~/st-cli/smartthings 를 쓴다.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "sudo 로 실행하세요: sudo $0" >&2
    exit 1
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"

CLI_BINARY=""
CLI_BINARY_EXPLICIT=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --cli-binary)
            [[ $# -ge 2 ]] || { echo "--cli-binary 뒤에 파일 경로가 필요합니다." >&2; exit 1; }
            CLI_BINARY="$2"
            CLI_BINARY_EXPLICIT=1
            shift 2
            ;;
        *)
            echo "알 수 없는 옵션: $1" >&2
            exit 1
            ;;
    esac
done
if [[ -z "$CLI_BINARY" && -n "${SUDO_USER:-}" ]]; then
    sudo_home="$(getent passwd "$SUDO_USER" | cut -d: -f6 || true)"
    [[ -z "$sudo_home" ]] || CLI_BINARY="$sudo_home/st-cli/smartthings"
fi
if [[ $CLI_BINARY_EXPLICIT -eq 1 && ! -f "$CLI_BINARY" ]]; then
    echo "CLI 바이너리가 없습니다: $CLI_BINARY" >&2
    exit 1
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || { echo "Python 3.11 이상이 필요합니다 (Raspberry Pi OS Bookworm 권장)." >&2; exit 1; }

# 1) 전용 시스템 계정 (로그인 불가)
if ! id stcollector &>/dev/null; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin stcollector
    echo "계정 생성: stcollector"
fi

# 2) 코드: root 소유, 서비스 계정은 읽기만
install -d -m 755 -o root -g root /opt/stcollector
rm -rf /opt/stcollector/stcollector
cp -r "$SRC/stcollector" /opt/stcollector/
find /opt/stcollector -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root /opt/stcollector
chmod -R u=rwX,go=rX /opt/stcollector

# 3) 설정: root 소유, stcollector 그룹만 읽기 (client_secret 보호)
install -d -m 750 -o root -g stcollector /etc/stcollector
if [[ ! -f /etc/stcollector/config.toml ]]; then
    install -m 640 -o root -g stcollector "$SRC/config.example.toml" /etc/stcollector/config.toml
    echo "설정 파일 생성: /etc/stcollector/config.toml"
else
    chown root:stcollector /etc/stcollector/config.toml
    chmod 640 /etc/stcollector/config.toml
fi

# 4) 데이터: stcollector 만 접근 (DB, 토큰)
install -d -m 700 -o stcollector -g stcollector /var/lib/stcollector

# 5) SmartThings CLI: root 소유, 서비스 계정은 실행만 (쓰기 불가)
#    파일 750 root:stcollector. Node 단일 실행 파일은 자기 자신을 읽어야 하므로 그룹에 r 도 준다.
CLI_DIR=/opt/stcollector-cli
CLI_DST="$CLI_DIR/smartthings"
CLI_HOME=/var/lib/stcollector/cli

is_arm64_elf() {
    [[ "$(od -An -tx1 -N4 "$1" | tr -d ' \n')" == 7f454c46 ]] \
        && [[ "$(od -An -tx1 -j18 -N2 "$1" | tr -d ' \n')" == b700 ]]
}

install -d -m 750 -o root -g stcollector "$CLI_DIR" "$CLI_DIR/login-bin"
install -m 750 -o root -g stcollector "$SRC/deploy/cli/xdg-open" "$CLI_DIR/login-bin/xdg-open"

if [[ -n "$CLI_BINARY" && -f "$CLI_BINARY" ]]; then
    if ! is_arm64_elf "$CLI_BINARY"; then
        echo "linux-arm64 실행 파일이 아닙니다: $CLI_BINARY" >&2
        exit 1
    fi
    if [[ -f "$CLI_DST" ]] && cmp -s "$CLI_BINARY" "$CLI_DST"; then
        chown root:stcollector "$CLI_DST"
        chmod 750 "$CLI_DST"
        echo "CLI 바이너리 변경 없음: $CLI_DST"
    else
        # 실행 중인 프로세스에 영향이 없도록 임시 파일에 쓴 뒤 교체한다.
        CLI_TMP="$CLI_DIR/.smartthings.new.$$"
        trap 'rm -f -- "$CLI_TMP"' EXIT
        install -m 750 -o root -g stcollector "$CLI_BINARY" "$CLI_TMP"
        mv -fT -- "$CLI_TMP" "$CLI_DST"
        trap - EXIT
        echo "CLI 바이너리 설치: $CLI_DST"
        sha256sum "$CLI_DST"
    fi
elif [[ -f "$CLI_DST" ]]; then
    chown root:stcollector "$CLI_DST"
    chmod 750 "$CLI_DST"
    echo "받아 둔 CLI 바이너리가 없어 기존 파일을 유지합니다: $CLI_DST"
else
    echo "경고: CLI 바이너리가 없습니다. backend = \"cli\" 로 쓰려면 ~/st-cli/smartthings 에 받아 두고 다시 실행하세요." >&2
fi

# CLI 프로세스의 HOME. 자격증명이 이 아래에 있으며 내용은 건드리지 않는다.
if [[ -L "$CLI_HOME" ]]; then
    echo "$CLI_HOME 이 심볼릭 링크입니다. 확인 후 다시 실행하세요." >&2
    exit 1
fi
install -d -m 700 -o stcollector -g stcollector "$CLI_HOME"

# 6) stcollector 래퍼와 systemd 서비스
install -m 755 -o root -g root "$SRC/deploy/stcollector" /usr/local/bin/stcollector
for unit in stcollector.service stcollector-web.service stcollector-rollup.service stcollector-rollup.timer stcollector-sysmon.service; do
    install -m 644 -o root -g root "$SRC/deploy/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload

# 예전 설정 파일에는 집계·대시보드 항목이 없으므로 없을 때만 덧붙인다 (기존 값은 건드리지 않는다)
if ! grep -q '^\[web\]' /etc/stcollector/config.toml; then
    sed -n '/^\[rollup\]/,$p' "$SRC/config.example.toml" >> /etc/stcollector/config.toml
    echo "설정 파일에 [rollup], [web] 항목을 추가했습니다."
fi
if ! grep -q '^\[sysmon\]' /etc/stcollector/config.toml; then
    sed -n '/^\[sysmon\]/,$p' "$SRC/config.example.toml" >> /etc/stcollector/config.toml
    echo "설정 파일에 [sysmon] 항목을 추가했습니다."
fi

for svc in stcollector stcollector-web stcollector-sysmon; do
    if systemctl is-active --quiet "$svc"; then
        systemctl restart "$svc"
        echo "서비스 재시작 완료: $svc"
    fi
done

# 기존 설정은 바꾸지 않는다. CLI 방식으로 바꿔야 하면 알리기만 한다.
if ! grep -Eq '^[[:space:]]*backend[[:space:]]*=[[:space:]]*"cli"' /etc/stcollector/config.toml; then
    echo "참고: 설정 파일에 backend = \"cli\" 가 없습니다. CLI 방식으로 쓰려면 README 의 '설정 입력' 단계를 진행하세요."
fi

cat <<'EOF'

설치 완료. 처음 설치라면 다음 순서로 진행하세요 (자세한 내용은 README).
  1) sudo bash deploy/migrate-cli-credentials.sh   # 로그인해 둔 CLI 자격증명 이전
     (로그인한 적이 없으면 README 의 'CLI 로그인과 재로그인')
  2) stcollector check                          # 기기 목록, 이벤트 이력 점검
  3) sudo systemctl enable --now stcollector    # 수집 시작 (부팅 시 자동 시작)
  4) journalctl -u stcollector -f               # 로그 확인
  5) 1주 후: stcollector intervals              # 센서별 실제 보고 간격

대시보드 (M2~M4)
  1) sudo systemctl enable --now stcollector-rollup.timer   # 매시 2분 집계
  2) stcollector hash-password                               # 비밀번호 해시 만들기
  3) sudo nano /etc/stcollector/config.toml                  # [web] password_hash 입력
  4) sudo ufw allow from 192.168.0.0/24 to any port 8080 proto tcp
  5) sudo systemctl enable --now stcollector-web             # http://<RPi IP>:8080
  6) sudo systemctl enable --now stcollector-sysmon          # RPi 상태와 이벤트 기록 (상태 탭)
EOF
