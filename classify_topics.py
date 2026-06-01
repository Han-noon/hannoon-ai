"""토픽 분류 파이프라인 실행 진입점.

main.py와 동일한 패턴으로 src/를 import path에 추가한 뒤
topic_classifier.pipeline.main()으로 처리를 위임한다.

실행: python classify_topics.py
"""

import os
import sys


def main() -> int:
    root = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(root, "src"))
    from topic_classifier.pipeline import main as pipeline_main

    return pipeline_main()


if __name__ == "__main__":
    raise SystemExit(main())
