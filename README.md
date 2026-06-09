# 한눈 RSS Collector

RSS 피드에서 기사 항목을 수집해 SQLite에 저장하고, 모든 신규 기사 원문 페이지를 크롤링해 본문과 기사 이미지를 확보하는 수집기입니다. 이후 욕설/악성 콘텐츠 탐지, 요약, 분류 같은 AI 처리 단계로 넘길 데이터를 준비하는 용도로 사용할 수 있습니다.

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
- `process`: 본문이 준비된 기사에 대해 어뷰징 분류 후 정상 기사만 요약합니다.
- `run`: RSS 수집, 원문 크롤링, 어뷰징 분류, 정상 기사 요약까지 실행합니다. 명령을 생략하면 기본값으로 `run`이 실행됩니다.

## 주요 옵션

- `--db`: SQLite 데이터베이스 경로입니다. 기본값은 `data/news.db`입니다.
- `--database-url`: Supabase/Postgres 연결 URL입니다. 지정하지 않으면 `DATABASE_URL` 환경변수를 사용하고, 둘 다 없으면 SQLite를 사용합니다.
- `--feeds-file`: RSS 피드 목록 JSON 파일 경로입니다. 기본값은 `config/feeds.json`입니다.
- `--feed`: 추가 RSS 피드 URL 또는 로컬 피드 파일 경로입니다. 여러 번 지정할 수 있습니다.
- `--min-crawl-len`: 크롤링한 본문을 유효한 기사 본문으로 인정할 최소 길이입니다.
- `--crawl-batch-size`: 한 번에 DB에서 가져와 처리할 크롤링 배치 크기입니다. `needs_crawl` 기사가 남아 있으면 다음 배치를 계속 처리합니다. 기본값은 `20`입니다.
- `--llm-cleanup`: 크롤링한 본문이 광고/구독 유도/관련기사 문구 등으로 의심될 때만 LLM으로 정제합니다.
- `--llm-cleanup-model`: `--llm-cleanup`에 사용할 OpenAI 모델입니다. 기본값은 `gpt-4.1-mini`입니다.
- `--domain-delay`: 같은 도메인에 연속 요청할 때 기다릴 시간(초)입니다.
- `--offline`: HTTP/HTTPS 요청을 건너뜁니다. 로컬 피드 파일 테스트에 사용할 수 있습니다.

## 데이터베이스 사용

로컬 개발과 테스트는 기본 SQLite를 사용합니다.

```powershell
python main.py run
```

운영 환경에서는 Supabase Postgres 연결 URL을 넘깁니다. Supabase/Postgres 스키마는 이 수집기 코드에서 생성하지 않고, 운영 DB의 migration 관리 흐름에서 별도로 준비합니다.

```powershell
$env:DATABASE_URL="postgresql://..."
python main.py run
```

또는 명령줄 옵션으로 직접 지정할 수 있습니다.

```powershell
python main.py --database-url "postgresql://..." run
```

Supabase/Postgres에서 필요한 테이블이나 컬럼이 없으면 앱은 DDL을 실행하지 않고 에러를 냅니다.

`data/*.db`, `.env`, Python 캐시 파일은 Git에 올리지 않습니다.

## SQLite 출력

`articles.status` 값:

- `needs_crawl`: RSS에서 수집된 신규 기사이며 원문 크롤링이 필요합니다.
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

## AI 후속 처리 작업

크롤링으로 본문이 준비된 기사(`ready`)를 `article_jobs` 큐에서 가져와 기사 단위로 어뷰징 분류 후 정상 기사만 바로 요약합니다.

- `models/clickbait-classifier`: 낚시성 기사 분류 모델
- `models/topic-mismatch-detector`: 본문 주제분리 탐지 모델
- 두 모델 중 하나라도 `abuse`면 최종 `abuse`, 둘 다 `normal`이면 최종 `normal`
- `--ai-batch-size`는 전체 개수 제한이 아니라 한 번에 가져올 AI 처리 배치 크기입니다. 기본값은 `20`입니다.
- `article_ai_results`에서는 `abuse_score`, `abuse_label`만 저장/수정합니다.
- `abuse` 기사는 요약하지 않고 `sent` 처리하며, `normal` 기사는 즉시 요약 후 `sent` 처리합니다.
- 저장 성공 후 `abuse_result=saved`, `summary=saved`, `job_status=sent` 로그가 출력됩니다.

실행:

```bash
python main.py process
```

## 정상 기사 요약 작업

요약은 `process` 또는 `run` 안에서 정상 기사에 대해 이어서 실행됩니다.

- `models/summary/bertsum_ext_model.pt`: KLUE BERT 기반 추출형 요약 모델
- 긴 기사는 본문 전체를 한 번에 넣지 않고 앞/중간/끝 문장을 나눠 점수화한 뒤 상위 문장을 뽑습니다.
- `article_ai_results`에서는 `summary`만 저장/수정합니다.

실행:

```bash
python main.py run
```

모델 weight는 Git에 올리지 않고 `models/` 아래에 별도로 배치합니다.
