import os

DEFAULT_DB = os.path.join("data", "news.db")
DEFAULT_FEEDS_FILE = os.path.join("config", "feeds.json")
DEFAULT_MIN_RSS_LEN = 300
DEFAULT_MIN_CRAWL_LEN = 400
DEFAULT_CRAWL_BATCH_SIZE = 20
DEFAULT_DOMAIN_DELAY = 2.0
DEFAULT_LLM_CLEANUP_MODEL = "gpt-4.1-mini"
USER_AGENT = "NoiseFreeRSSBot/0.1 (+https://example.invalid)"
