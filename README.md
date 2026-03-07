# Hoseo Macro

학습 목적으로 개발된 호서대학교 LMS 자동 수강 프로그램입니다.
해당 매크로 사용에 대한 어떠한 책임도 지지 않습니다.

## 기능
- LMS 자동 로그인
- 미수강 강의 자동 탐색 및 수강
- 수강 가능 기간 자동 판별 (주차별 진도 체크 기간 기준)
- 백그라운드 실행 (Headless Chrome)
- GUI 제공 (PySide6)
- 아이디/비밀번호 암호화 저장

## 실행 방법 
1. 필요한 라이브러리 설치:
   ```bash
   pip install -r requirements.txt
   ```
2. 프로그램 실행:
   ```bash
   cd src
   python hoseo_gui_pyside.py
   ```

## 프로젝트 구조
```
src/
├── hoseo_gui_pyside.py   # GUI (PySide6)
├── hoseo_crawler.py      # 모듈 통합 진입점
├── driver_utils.py       # 드라이버 유틸리티 (로깅, 딜레이 등)
├── auth.py               # 로그인/드라이버 초기화
├── course_scanner.py     # 강의 목록 조회, 수강 가능 주차 판별
├── video_watcher.py      # 동영상 재생 및 수강 처리
└── config_manager.py     # 설정 저장/로드 (암호화)
```

