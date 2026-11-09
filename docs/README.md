# 문서 지도

- 목적: 상황에 맞는 문서 하나만 열도록 구분

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
| 전압 부족 시험 결과, 전원 운영 설정 되돌리기 | [POWER_TEST](POWER_TEST.md) |

## 내용이 겹칠 때 기준

| 주제 | 기준 문서 |
|---|---|
| 지금 코드의 동작, 테이블, 서비스 | [ARCHITECTURE](ARCHITECTURE.md). 더 정확한 원본은 코드와 `stcollector/schema.sql`, `deploy/systemd/` |
| 설계 이유, 진행 단계, 미결 사항 | [PLAN](../PLAN.md) |
| 설치 명령과 CLI 로그인 절차 | [README](../README.md). 설치 스크립트의 안내문이 이 문서의 절 제목을 가리키므로 제목 변경 금지 |
| 전원 시험 수치 | [POWER_TEST](POWER_TEST.md) |

## 현재 상태

- 수집 방식 기준: `backend = "cli"`
- OAuth 방식: 유료 Developer Plan이 필요해 선택지로만 유지
- 수집 코드의 CLI 전환과 배포 스크립트: PC 테스트까지 완료, RPi에는 아직 미배포
- RPi의 `~/stcollector-src`와 `/opt/stcollector`: 예전 M1 코드가 남아 있음
- CLI 실행 파일과 자격증명: 로그인한 계정의 홈에 있어 서비스 계정으로 이전 필요
- 대시보드와 집계: 구현 완료, 실제 데이터로는 미확인
- 진행 단계 기준: [PLAN §11](../PLAN.md#11-단계별-진행)
- RPi에서 시험하지 못한 항목: [OPERATIONS: RPi 미확인 항목](OPERATIONS.md#rpi-미확인-항목)
- 자동 백업(M5): 미구현

## 어댑터 명칭

- **QC5V**: 원래 쓰던 어댑터
- **DZ5V3A**: 교체한 어댑터(케이블 일체형, 약 50cm)
