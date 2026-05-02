import os
import sys


def main() -> int:
    """CLI 실행 진입점.

    이 프로젝트는 아직 패키지로 설치하지 않고 저장소 루트에서 바로 실행하는 형태다.
    그래서 `src` 디렉터리를 import path에 추가한 뒤 실제 CLI 구현인
    `collector.cli.main()`으로 처리를 넘긴다.
    """
    root = os.path.dirname(os.path.abspath(__file__))
    # 설치 패키지로 배포하지 않아도 로컬 실행에서 src/collector를 import할 수 있게 한다.
    sys.path.insert(0, os.path.join(root, "src"))
    from collector.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())

