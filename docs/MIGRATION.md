# 보드 교체와 복구

> **자동 백업(M5)은 아직 구현되지 않았습니다.** `deploy/`에 백업 서비스와 타이머가 없습니다. 아래 [수동 사본](#수동-사본-m5-전까지)을 만들어 두지 않으면, microSD가 읽히지 않을 때 쌓인 데이터를 되살릴 수 없습니다.
>
> 이 문서의 복구 절차는 **RPi에서 시험하지 못했습니다 (RPi 미확인).** 파일 위치와 권한은 `deploy/install.sh`를 읽어 맞췄습니다.

## 먼저 상황을 고릅니다

| 상황 | 할 일 |
|---|---|
| 보드만 고장, microSD 정상 | 같은 모델의 새 보드에 microSD를 그대로 꽂습니다. 옮길 것이 없습니다. MAC 주소가 바뀌므로 공유기의 고정 IP를 다시 할당하고, PC의 SSH `known_hosts`는 그대로 씁니다. |
| microSD가 읽히지만 부팅 안 됨, 또는 새 microSD로 옮김 | [복구 순서](#복구-순서)를 따릅니다. |
| microSD가 읽히지 않고 사본도 없음 | 새로 설치합니다. 수집을 시작하면 SmartThings에 남은 최근 이력(약 7일, `backfill_days`)만 다시 받습니다. |

다른 모델의 보드나 다른 어댑터로 바꾸면 운영 설정과 전원 시험 결과를 그대로 적용할 수 없습니다. [POWER_TEST](POWER_TEST.md)를 보고 `vcgencmd get_throttled`로 다시 확인합니다.

## 옮길 것

| 데이터 | 경로 | 비고 |
|---|---|---|
| DB | `/var/lib/stcollector/sensors.db`, 있으면 `sensors.db-wal`도 | **`-wal`이 있으면 반드시 함께 옮깁니다.** 최근 기록이 그 안에 있을 수 있습니다. `-shm`과 `*.lock`은 필요 없습니다. |
| 설정 | `/etc/stcollector/config.toml` | 대시보드 비밀번호 해시, `location_id`가 들어 있습니다. OAuth 방식이면 client secret도 있습니다. |
| CLI 자격증명 | `/var/lib/stcollector/cli/.local/share/@smartthings/cli/credentials.json` | **복사보다 재로그인을 권장합니다.** 아래 주의사항을 먼저 읽으세요. |
| OAuth 토큰 | `/var/lib/stcollector/token.json` | `backend = "oauth"`일 때만 있습니다. |

옮기지 않는 것: 코드(`/opt/stcollector`), systemd 유닛, 명령행 래퍼는 `install.sh`가 다시 만듭니다. CLI 실행 파일은 다시 받습니다. CLI 자체 로그(`smartthings.log`)는 내용을 확인하지 못했으므로 옮기지 않습니다.

집계 테이블은 DB 안에 있으므로 따로 옮기지 않습니다. 보관 기간(기본 1년)이 지난 원본은 이미 지워져 다시 계산할 수 없으므로, DB는 일부가 아니라 파일 전체를 옮깁니다.

복구한 DB에는 수집 위치(`history_cursor`)가 함께 있어, 수집을 다시 시작하면 멈춘 시점 10분 전부터 이어서 받습니다. SmartThings에 이력이 남아 있는 기간(약 7일) 안이면 빈틈이 메워집니다.

## CLI 자격증명 복사 시 주의사항

이 파일의 권한은 `controller:stCli`로 **기기 제어까지 가능합니다.** 수집기의 읽기 명령 제한은 파일이 유출되면 소용없습니다.

- 가능하면 복사하지 말고 새 보드에서 [재로그인](../README.md#cli-로그인과-재로그인)합니다. 브라우저 로그인 한 번이면 됩니다.
- 복사한다면 옛 microSD를 **새 RPi에 직접** 연결해 옮깁니다. PC, USB 메모리, 클라우드 동기화 폴더를 거치지 않습니다.
- 내용을 출력하지 않습니다. `cat`, 편집기, 채팅, 이슈 붙여넣기 모두 금지입니다.
- 복사 뒤 소유자가 `stcollector`, 파일 600, 상위 폴더 700인지 확인합니다.
- **옛 보드와 새 보드를 같은 자격증명으로 동시에 돌리지 않습니다.** 토큰 갱신 방식이 확인되지 않아, 한쪽의 갱신이 다른 쪽을 무효로 만들 수 있습니다.
- 옛 microSD에는 자격증명이 남아 있습니다. 재사용하거나 버리기 전에 물리적으로 파기합니다. microSD는 덮어쓰기로 지워진다는 보장이 없습니다.
- `deploy/migrate-cli-credentials.sh`는 같은 보드의 로그인 사용자에게서 서비스 계정으로 옮기는 도구입니다. 옛 microSD에서 복구할 때는 쓰지 않습니다.
- 유출이 의심되면 [OPERATIONS: 자격증명 유출](OPERATIONS.md#자격증명-유출이-의심될-때)을 따릅니다.

## 복구 순서

옛 보드는 꺼 둡니다. 명령은 새 RPi에서 실행합니다.

1. 새 microSD에 [README M0](../README.md#m0-준비)의 기본 설정(OS, SSH, 방화벽, journald, 운영 설정)과 CLI 다운로드를 진행합니다.
2. [README M1의 코드 복사와 설치](../README.md#1-코드-복사와-설치)까지 진행합니다. **수집과 웹은 아직 시작하지 않습니다.** 새 DB가 없어야 합니다.

   ```bash
   sudo ls -la /var/lib/stcollector/
   ```

3. 옛 microSD를 USB 리더로 연결하고 읽기 전용으로 마운트합니다. `lsblk`로 장치 이름을 확인해 `sdX2`를 바꿉니다. 두 번째 파티션이 루트입니다.

   ```bash
   lsblk
   ```

   ```bash
   sudo mkdir -p /mnt/old && sudo mount -o ro /dev/sdX2 /mnt/old
   ```

4. 설정을 복구합니다.

   ```bash
   sudo install -m 640 -o root -g stcollector /mnt/old/etc/stcollector/config.toml /etc/stcollector/config.toml
   ```

5. DB를 복구합니다.

   ```bash
   sudo install -m 600 -o stcollector -g stcollector /mnt/old/var/lib/stcollector/sensors.db /var/lib/stcollector/sensors.db
   ```

   ```bash
   sudo sh -c 'f=/mnt/old/var/lib/stcollector/sensors.db-wal; [ ! -f "$f" ] || install -m 600 -o stcollector -g stcollector "$f" /var/lib/stcollector/sensors.db-wal'
   ```

   ```bash
   sudo -u stcollector sqlite3 /var/lib/stcollector/sensors.db "PRAGMA integrity_check"
   ```

   `ok`가 나와야 합니다. 아니면 이 DB로 수집을 시작하지 말고, 사본이 있으면 그것을 씁니다. 복구할 수 없으면 DB 파일을 치우고 새로 시작합니다.

6. CLI 자격증명을 준비합니다. 권장 방법은 [재로그인](../README.md#cli-로그인과-재로그인)입니다. 부득이 복사할 때만 다음을 실행합니다.

   ```bash
   sudo sh -c 'd=/var/lib/stcollector/cli; for p in .local .local/share .local/share/@smartthings .local/share/@smartthings/cli; do install -d -m 700 -o stcollector -g stcollector "$d/$p"; done'
   ```

   ```bash
   sudo install -m 600 -o stcollector -g stcollector /mnt/old/var/lib/stcollector/cli/.local/share/@smartthings/cli/credentials.json /var/lib/stcollector/cli/.local/share/@smartthings/cli/credentials.json
   ```

7. 옛 microSD를 분리합니다.

   ```bash
   sudo umount /mnt/old
   ```

8. 점검하고 서비스를 시작합니다.

   ```bash
   stcollector check
   ```

   ```bash
   sudo systemctl enable --now stcollector stcollector-rollup.timer stcollector-web
   ```

9. [README의 방화벽 단계](../README.md#4-방화벽과-서비스)로 8080을 열고, [OPERATIONS의 일상 점검](OPERATIONS.md#일상-점검)으로 상태를 확인합니다. 멈춘 동안의 공백은 대시보드 **상태** 탭에 표시됩니다.

공유기 대역이 달라졌으면 `ufw` 규칙, `allowed_networks`, `IPAddressAllow`를 함께 바꿉니다.

## 수동 사본 (M5 전까지)

자동 백업이 생기기 전에 가끔 DB와 설정의 사본을 별도 USB 디스크에 만들어 둡니다. 수집을 멈추지 않아도 됩니다. USB 디스크는 `/mnt/usb`에 마운트했다고 가정합니다. **CLI 자격증명은 사본에 넣지 않습니다.** 복구할 때 재로그인합니다.

```bash
sudo -u stcollector sqlite3 /var/lib/stcollector/sensors.db ".backup '/var/lib/stcollector/snapshot.db'"
```

```bash
sudo install -m 600 /var/lib/stcollector/snapshot.db "/mnt/usb/sensors-$(date +%F).db"
```

```bash
sudo -u stcollector rm /var/lib/stcollector/snapshot.db
```

```bash
sudo install -m 600 /etc/stcollector/config.toml "/mnt/usb/config-$(date +%F).toml"
```

사본에는 기기 이름, 방 이름, 위치 ID, 비밀번호 해시가 들어 있으므로 클라우드에 올리지 않습니다. 사본에서 복구할 때는 위 5단계의 원본 경로를 사본 파일로 바꿉니다. 사본에는 `-wal`이 없습니다.
