import json
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup


CATEGORY_PATTERNS = {
    "politics": [
        "politics",
        "politic",
        "polrss",
        "정치",
        "segye_politic",
        "sectionid=01",
        "section/65",
        "s1n47",
        "mediatoday",
        "미디어",
        "언론",
    ],
    "economy": [
        "economy",
        "economic",
        "eco",
        "경제",
        "business",
        "30100041",
        "30200030",
        "50200011",
        "economy-industry",
        "sectionid=02",
        "section/66",
        "s1n54",
        "eto/economy",
    ],
    "society": [
        "society",
        "social",
        "national",
        "soc",
        "사회",
        "50400012",
        "30300018",
        "50100032",
        "politics-society",
        "sectionid=03",
        "section/67",
        "s1n58",
        "industry",
        "산업",
        "eto/industry",
        "ablenews",
        "장애",
        "복지",
        "womennews",
        "여성"
    ],
    "international": [
        "international",
        "world",
        "global",
        "kh_world",
        "intrss",
        "국제",
        "세계",
        "sectionid=07",
        "section/68",
        "s1n59",
        "eto/global",
        "northkorea",
        "north-korea",
        "북한"
    ]
}


PUBLISHER_PATTERNS = [
    ("조선일보", "right", ["chosun.com"]),
    ("연합뉴스", "mid", ["yna.co.kr"]),
    ("연합뉴스TV", "mid", ["yonhapnewstv.co.kr"]),
    ("매일경제", "right", ["mk.co.kr"]),
    ("경향신문", "left", ["khan.co.kr"]),
    ("국민일보", "right", ["kmib.co.kr"]),
    ("뉴시스", "mid", ["newsis.com"]),
    ("동아일보", "right", ["donga.com"]),
    ("미디어오늘", "left", ["mediatoday.co.kr"]),
    ("서울뉴스", "mid", ["seoulnews.org"]),
    ("세계일보", "right", ["segye.com"]),
    ("시사저널", "mid", ["sisajournal.com"]),
    ("에이블뉴스", "mid", ["ablenews.co.kr"]),
    ("여성신문", "left", ["womennews.co.kr"]),
    ("프레시안", "left", ["pressian.com"]),
    ("한겨레", "left", ["hani.co.kr"]),
    ("JTBC", "left", ["jtbc.co.kr"]),
    ("SBS", "mid", ["sbs.co.kr"]),
    ("이투데이", "mid", ["etoday.co.kr"]),
    ("아시아경제", "mid", ["asiae.co.kr"]),
    ("MBN", "right", ["mbn.co.kr"]),
]


def infer_feed_category(feed_url: str, feed_title: str | None = None) -> str:
    """피드 URL과 제목을 기반으로 기사 카테고리를 추론한다."""
    target = f"{feed_url} {feed_title or ''}".lower()
    category_order = [
        "politics",
        "economy",
        "international",
        "society"
    ]
    for category in category_order:
        patterns = CATEGORY_PATTERNS[category]
        if any(pattern.lower() in target for pattern in patterns):
            return category
    return "other"


def infer_publisher_metadata(feed_url: str, feed_title: str | None = None) -> tuple[str, str]:
    """피드 URL과 제목으로 언론사 이름과 언론사 기준 성향을 추론한다.

    `bias_type`은 개별 기사 논조 분석 결과가 아니라 언론사 단위 하드코딩 매핑이다.
    사용 가능한 값은 프로젝트 정책상 `right`, `left`, `mid` 세 가지로 제한한다.
    """
    target = f"{feed_url} {feed_title or ''}".lower()
    for publisher, bias_type, patterns in PUBLISHER_PATTERNS:
        if any(pattern.lower() in target for pattern in patterns):
            return publisher, bias_type
    return "기타", "mid"


def _looks_like_url(value: str) -> bool:
    """값이 본문 HTML이 아니라 URL인지 확인한다."""
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"}


def html_to_text(html: str | None) -> str:
    """RSS summary/content HTML을 후속 처리용 plain text로 정규화한다."""
    if not html:
        return ""
    if _looks_like_url(html):
        # 일부 RSS 필드는 본문 대신 대표 이미지/기사 URL만 넣어 오므로 본문으로 취급하지 않는다.
        return ""
    if "<" not in html and ">" not in html:
        return " ".join(html.split())
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return " ".join(text.split())


def _strip_unwanted(soup: BeautifulSoup) -> None:
    """본문 후보 탐색 전에 명백한 비본문 영역을 제거한다."""
    # 본문 후보 점수를 왜곡하는 스크립트/내비게이션/광고성 영역을 먼저 제거한다.
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    for tag in soup.find_all(["nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    for tag in soup.find_all(True):
        # decompose된 태그는 attrs가 None이 될 수 있어 재접근 전에 방어한다.
        if tag is None or not hasattr(tag, "get") or tag.attrs is None:
            continue
        classes = " ".join(tag.get("class") or []).lower()
        ident = (tag.get("id") or "").lower()
        hint = f"{classes} {ident}"
        tokens = {part for chunk in hint.replace("_", "-").split() for part in chunk.split("-")}
        unwanted_tokens = {
            "nav",
            "footer",
            "header",
            "sidebar",
            "comment",
            "related",
            "share",
            "ad",
            "ads",
            "banner",
        }
        if tokens.intersection(unwanted_tokens):
            tag.decompose()


def _score_text(text: str) -> int:
    """본문 후보의 점수를 계산한다.

    긴 텍스트와 단어 수가 많은 영역이 실제 기사 본문일 가능성이 높다는 단순한
    휴리스틱을 사용한다.
    """
    words = text.split()
    return len(text) + (len(words) * 5)


def _collect_content_element_text(elements: list[dict]) -> list[str]:
    """Arc/Fusion CMS의 중첩 content_elements에서 텍스트 노드만 모은다."""
    # Arc/Fusion 계열 CMS는 본문을 중첩 content_elements JSON으로 제공한다.
    texts: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        content = element.get("content")
        if element.get("type") == "text" and content:
            text = html_to_text(content)
            if text:
                texts.append(text)
        nested = element.get("content_elements")
        if isinstance(nested, list):
            texts.extend(_collect_content_element_text(nested))
    return texts


def _extract_fusion_global_content(html: str) -> str:
    """조선일보 등 Arc/Fusion 기반 페이지에서 본문 JSON을 추출한다."""
    # 조선일보 페이지는 렌더링된 DOM보다 Fusion.globalContent에 본문이 더 안정적으로 들어 있다.
    marker = "Fusion.globalContent="
    start = html.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = html.find(";Fusion.globalContentConfig=", start)
    if end < 0:
        return ""

    try:
        data = json.loads(html[start:end])
    except json.JSONDecodeError:
        return ""

    elements = data.get("content_elements")
    if not isinstance(elements, list):
        return ""
    return " ".join(_collect_content_element_text(elements))


def _extract_jtbc_query_content(html: str) -> str:
    """JTBC Next.js 페이지의 hydration 스크립트에서 articleContent를 추출한다."""
    # JTBC는 Next.js hydration 스크립트의 React Query 캐시에 articleContent를 넣는다.
    marker = '"articleContent":"'
    start = html.find(marker)
    if start < 0:
        return ""
    start += len(marker)

    chars: list[str] = []
    escaped = False
    for char in html[start:]:
        if escaped:
            chars.append("\\" + char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            break
        chars.append(char)

    try:
        content_html = json.loads(f'"{"".join(chars)}"')
    except json.JSONDecodeError:
        return ""
    return html_to_text(content_html)


def extract_article_text(html: str) -> str:
    """HTML에서 기사 본문 텍스트를 추출한다.

    우선 사이트별로 안정적인 JSON 데이터 구조를 시도한다. 사이트별 구조가 없으면
    article/main/section/div 후보 중 가장 본문에 가까운 영역을 점수화해 선택한다.
    """
    # 사이트 전용 구조를 먼저 시도하고, 실패하면 범용 DOM 휴리스틱으로 떨어진다.
    fusion_text = _extract_fusion_global_content(html)
    if fusion_text:
        return fusion_text
    jtbc_text = _extract_jtbc_query_content(html)
    if jtbc_text:
        return jtbc_text

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""
    _strip_unwanted(soup)

    candidates = []
    for selector in ["article", "main", "section", "div"]:
        candidates.extend(soup.find_all(selector))

    best_text = ""
    best_score = 0
    for node in candidates:
        if node is None:
            continue
        text = node.get_text(" ", strip=True)
        text = " ".join(text.split())
        if len(text) < 200:
            continue
        score = _score_text(text)
        if score > best_score:
            best_text = text
            best_score = score

    if not best_text:
        # 적절한 본문 컨테이너를 못 찾은 경우에도 빈 값 대신 페이지 전체 텍스트를 반환한다.
        container = soup.body or soup
        best_text = " ".join(container.get_text(" ", strip=True).split())

    return best_text


def load_feed_urls(feeds_file: str | None, feeds: list[str] | None) -> list[str]:
    """설정 파일과 CLI 옵션에서 RSS 피드 URL을 읽어 중복 제거 후 반환한다."""
    urls: list[str] = []
    if feeds:
        urls.extend(feeds)
    if feeds_file and os.path.exists(feeds_file):
        with open(feeds_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            urls.extend(data)
        elif isinstance(data, dict) and "feeds" in data:
            urls.extend(data["feeds"])
    # 순서를 유지한 채 중복 피드를 제거한다.
    return list(dict.fromkeys(urls))


def resolve_entry_link(link: str | None, feed_source: str) -> str | None:
    """RSS entry link를 크롤러가 처리할 수 있는 절대 URL로 정규화한다."""
    if not link:
        return None
    parsed = urlparse(link)
    if parsed.scheme:
        return link
    if os.path.exists(feed_source):
        # 로컬 RSS 샘플의 상대 링크는 같은 폴더 기준 file:// URL로 바꿔 크롤러가 처리하게 한다.
        base_dir = os.path.dirname(os.path.abspath(feed_source))
        local_path = os.path.join(base_dir, link)
        return Path(local_path).resolve().as_uri()
    parsed_feed = urlparse(feed_source)
    if parsed_feed.scheme == "file":
        # 이미 file:// 형태로 들어온 피드도 상대 링크 기준점을 유지한다.
        base_dir = os.path.dirname(unquote(parsed_feed.path.lstrip("/")))
        local_path = os.path.join(base_dir, link)
        return Path(local_path).resolve().as_uri()
    return link
