# 문서 지도

## 상황별 문서

| 지금 하려는 일 | 볼 문서 |
|---|---|
| RPi에 처음 설치, 코드 갱신 | [README](../README.md) |
| CLI 로그인, 토큰 만료 후 재로그인 | [README: CLI 로그인과 재로그인](../README.md#cli-로그인과-재로그인) |
| 평소 상태 점검, 집계 수동 실행 | [OPERATIONS: 일상 점검](OPERATIONS.md#일상-점검) |
| 오류 발생 | [OPERATIONS: 장애 대응](OPERATIONS.md#장애-대응) |
| 보드나 microSD 고장 | [MIGRATION](MIGRATION.md) |
| 코드 동작 방식, DB 테이블, 서비스 관계, 보안 경계 | [ARCHITECTURE](ARCHITECTURE.md) |
| 설계 이유, 앞으로의 단계 | [PLAN](../PLAN.md) |
| 버전별 변경 내역 | [CHANGELOG](../CHANGELOG.md) |
| 전압 부족 시험 결과, 전원 운영 설정 되돌리기 | [POWER_TEST](POWER_TEST.md) |
| SmartThings 앱에서 RPi 상태 보기, 허브 드라이버 설치 | [edge-driver/README](../edge-driver/README.md) |

## 내용이 겹칠 때 기준

| 주제 | 기준 문서 |
|---|---|
| 지금 코드의 동작, 테이블, 서비스 | [ARCHITECTURE](ARCHITECTURE.md). 더 정확한 원본은 코드와 `stcollector/schema.sql`, `deploy/systemd/` |
| 설계 이유, 진행 단계, 미결 사항 | [PLAN](../PLAN.md) |
| 설치 명령과 CLI 로그인 절차 | [README](../README.md). 설치 스크립트의 안내문이 이 문서의 절 제목을 가리키므로 제목 변경 금지 |
| 전원 시험 수치 | [POWER_TEST](POWER_TEST.md) |

## 현재 상태

- 배포: 2026-11-10 01:15 KST 완료
- 버전: 0.2.0 (2026-11-13). 변경 내역은 [CHANGELOG](../CHANGELOG.md)
- 활성화, 부팅 시 자동 시작: `stcollector`, `stcollector-web`, `stcollector-sysmon`, `stcollector-rollup.timer`
- 수집 방식: `backend = "cli"`. OAuth는 유료 Developer Plan이 필요해 선택지로만 유지
- CLI 실행 파일: `/opt/stcollector-cli/smartthings`
- CLI 자격증명: `/var/lib/stcollector/cli/.local/share/@smartthings/cli/credentials.json` (서비스 계정 소유)
- 전원 운영: QC5V + 15cm 케이블, 900MHz, 1코어
- RPi에서 확인됨: 서비스 샌드박스 안 CLI 동작(기기 25개, 수집 방식 `history`), 설치·이전 스크립트 실행, PC에서 `http://<RPi IP>:8080` 응답
- 아직 미확인: CLI 토큰 자동 갱신(액세스 토큰 만료 2026-11-11 00:06 KST), 실제 데이터로 본 대시보드와 첫 집계, 만료 시 재인증 출력, 스마트폰 로그인. 전체 목록은 [OPERATIONS: RPi 미확인 항목](OPERATIONS.md#rpi-미확인-항목)
- 진행 단계 기준: [PLAN §11](../PLAN.md#11-단계별-진행)
- 자동 백업(M5): 미구현

## 어댑터 명칭

- QC5V: 원래 쓰던 어댑터. 현재 15cm 케이블로 운영
- DZ5V3A: 교체한 어댑터(케이블 일체형, 약 50cm)
