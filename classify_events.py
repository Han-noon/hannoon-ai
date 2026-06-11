"""
이벤트 분류 파이프라인 실행 진입점

실행: python classify_events.py
"""

import os
import sys


def main() -> int:
    root = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(root, "src"))
    from event_classifier.pipeline import main as pipeline_main

    return pipeline_main()


if __name__ == "__main__":
    raise SystemExit(main())