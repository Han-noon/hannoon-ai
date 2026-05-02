# NoiseFree RSS Collector

RSS 피드에서 기사 항목을 수집해 SQLite에 저장하고, RSS 본문이 짧은 경우 원문 페이지를 크롤링해 본문을 보강하는 수집기입니다. 이후 욕설/악성 콘텐츠 탐지, 요약, 분류 같은 AI 처리 단계로 넘길 데이터를 준비하는 용도로 사용할 수 있습니다.

## 프로젝트 구조

- `main.py`: CLI 진입점
- `src/collector/`: RSS 수집, 원문 크롤링, 저장소 처리 코드
- `config/feeds.json`: 수집할 RSS 피드 목록
- `data/`: 실행 중 생성되는 SQLite 데이터베이스 저장 위치
- `requirements.txt`: 실행에 필요한 Python 패키지 목록

## 빠른 시작

```powershell
python -m pip install -r requirements.txt
python main.py run
```

## 피드 설정

`config/feeds.json` 파일에 수집할 RSS 주소를 등록합니다.

```json
{
  "feeds": [
    "https://www.chosun.com/arc/outboundfeeds/rss/category/politics/?outputType=xml"
  ]
}
```

명령줄에서 피드를 직접 추가할 수도 있습니다. 공통 옵션은 하위 명령어보다 앞에 둡니다.

```powershell
python main.py --feed https://feeds.bbci.co.uk/news/rss.xml run
```

## 실행 명령

- `fetch`: RSS 항목만 수집해 저장합니다.
- `crawl`: 원문 크롤링이 필요한 기사만 처리합니다.
- `run`: RSS 수집 후 필요한 기사 원문을 크롤링합니다. 명령을 생략하면 기본값으로 `run`이 실행됩니다.

## 주요 옵션

- `--db`: SQLite 데이터베이스 경로입니다. 기본값은 `data/news.db`입니다.
- `--database-url`: Supabase/Postgres 연결 URL입니다. 지정하지 않으면 `DATABASE_URL` 환경변수를 사용하고, 둘 다 없으면 SQLite를 사용합니다.
- `--feeds-file`: RSS 피드 목록 JSON 파일 경로입니다. 기본값은 `config/feeds.json`입니다.
- `--feed`: 추가 RSS 피드 URL 또는 로컬 피드 파일 경로입니다. 여러 번 지정할 수 있습니다.
- `--min-rss-len`: RSS 본문이 이 길이보다 짧으면 원문 크롤링 대상으로 표시합니다.
- `--min-crawl-len`: 크롤링한 본문을 유효한 기사 본문으로 인정할 최소 길이입니다.
- `--domain-delay`: 같은 도메인에 연속 요청할 때 기다릴 시간(초)입니다.
- `--offline`: HTTP/HTTPS 요청을 건너뜁니다. 로컬 피드 파일 테스트에 사용할 수 있습니다.

## 데이터베이스 사용

로컬 개발과 테스트는 기본 SQLite를 사용합니다.

```powershell
python main.py run
```

운영 환경에서는 Supabase Postgres 연결 URL을 넘깁니다.

```powershell
$env:DATABASE_URL="postgresql://..."
python main.py run
```

또는 명령줄 옵션으로 직접 지정할 수 있습니다.

```powershell
python main.py --database-url "postgresql://..." run
```

`data/*.db`, `.env`, Python 캐시 파일은 Git에 올리지 않습니다.

## SQLite 출력

`articles.status` 값:

- `needs_crawl`: RSS 본문이 짧아 원문 크롤링이 필요합니다.
- `ready`: 사용할 수 있는 본문이 준비되었습니다.
- `crawl_failed`: 크롤링이 실패했거나 크롤링한 본문이 너무 짧습니다.

`articles.category` 값:

- RSS 피드 URL과 피드 제목을 기준으로 자동 분류됩니다.
- 기본 분류값은 `politics`, `economy`, `society`, `international`, `other`입니다.

`articles.publisher` 값:

- RSS 피드 URL과 피드 제목을 기준으로 추론한 언론사 이름입니다.
- 예: `조선일보`, `JTBC`, `한겨레`, `연합뉴스`

`articles.bias_type` 값:

- 기사 내용의 AI 판정값이 아니라 언론사 이름 기준으로 매핑한 성향값입니다.
- 현재 값은 `right`, `left`, `mid` 세 가지만 사용합니다.

`article_jobs` 테이블:

- `status`: 후속 처리 상태입니다. `pending`, `sent`, `failed` 값을 사용할 수 있습니다.
- `attempts`: 후속 처리 시도 횟수입니다.
- `last_error`: 마지막 실패 메시지입니다.
- `last_attempt_at`: 마지막 후속 처리 시각입니다.
