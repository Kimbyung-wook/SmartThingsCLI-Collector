# 문서 지도

상황에 맞는 문서 하나만 열면 되도록 나눴습니다.

| 지금 하려는 일 | 볼 문서 |
|---|---|
| RPi에 처음 설치, 코드 갱신 | [README](../README.md) |
| CLI 로그인, 토큰 만료 후 재로그인 | [README: CLI 로그인과 재로그인](../README.md#cli-로그인과-재로그인) |
| 평소 상태 점검, 집계 수동 실행 | [OPERATIONS: 일상 점검](OPERATIONS.md#일상-점검) |
| 오류가 났을 때 | [OPERATIONS: 장애 대응](OPERATIONS.md#장애-대응) |
| 보드나 microSD가 고장 났을 때 | [MIGRATION](MIGRATION.md) |
| 코드가 어떻게 돌아가는지, DB 테이블, 서비스 관계, 보안 경계 | [ARCHITECTURE](ARCHITECTURE.md) |
| 왜 이렇게 설계했는지, 앞으로의 단계 | [PLAN](../PLAN.md) |
| 전압 부족 시험 결과, 전원 운영 설정 되돌리기 | [POWER_TEST](POWER_TEST.md) |

## 내용이 겹칠 때 기준

| 주제 | 기준 문서 |
|---|---|
| 지금 코드의 동작, 테이블, 서비스 | [ARCHITECTURE](ARCHITECTURE.md). 더 정확한 원본은 코드와 `stcollector/schema.sql`, `deploy/systemd/` |
| 설계 이유, 진행 단계, 미결 사항 | [PLAN](../PLAN.md) |
| 설치 명령과 CLI 로그인 절차 | [README](../README.md). 설치 스크립트의 안내문이 이 문서의 절 제목을 가리키므로 제목을 바꾸지 않습니다. |
| 전원 시험 수치 | [POWER_TEST](POWER_TEST.md) |

## 현재 상태

- 수집 방식의 기준은 `backend = "cli"`입니다. OAuth 방식은 유료 Developer Plan이 필요해 선택지로만 남아 있습니다.
- 수집 코드의 CLI 전환과 배포 스크립트는 PC 테스트까지 끝났고, RPi에는 아직 올리지 않았습니다. RPi의 `~/stcollector-src`와 `/opt/stcollector`에는 예전 M1 코드가 남아 있고, CLI 실행 파일과 자격증명은 로그인한 계정의 홈에 있어 서비스 계정으로 옮겨야 합니다.
- 대시보드와 집계는 구현됐지만 실제 데이터로 확인하지 못했습니다. 진행 단계는 [PLAN §11](../PLAN.md#11-단계별-진행)이 기준입니다.
- 아직 RPi에서 시험하지 못한 항목은 [OPERATIONS: RPi 미확인 항목](OPERATIONS.md#rpi-미확인-항목)에 모아 두었습니다.
- 자동 백업(M5)은 구현되지 않았습니다.

어댑터 명칭: **QC5V**는 원래 쓰던 어댑터, **DZ5V3A**는 교체한 어댑터(케이블 일체형, 약 50cm)입니다.
