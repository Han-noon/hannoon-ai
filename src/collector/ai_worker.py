from __future__ import annotations

from abuse_detector import ArticleAbuseDetector, ArticleInput

from .storage import (
    load_pending_article_jobs,
    mark_article_job_failed,
    save_article_ai_result,
)


def classify_pending_articles(
    conn,
    *,
    p1_model_dir: str,
    p2_model_dir: str,
    batch_size: int,
    device: str = "auto",
    max_attempts: int = 3,
) -> int:
    """대기 중인 ready 기사를 배치 단위로 모두 어뷰징 분류한다.

    batch_size는 전체 처리 제한이 아니라 한 번에 DB에서 가져올 묶음 크기다.
    분류 결과는 article_ai_results의 abuse_score, abuse_label에만 저장한다.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than 0.")
    if max_attempts <= 0:
        raise ValueError("max_attempts must be greater than 0.")

    print(
        "[classify] loading models "
        f"(p1={p1_model_dir}, p2={p2_model_dir}, device={device})"
    )
    detector = ArticleAbuseDetector(
        p1_model_dir=p1_model_dir,
        p2_model_dir=p2_model_dir,
        device=device,
    )

    classified = 0
    failed = 0
    batch_no = 0

    while True:
        # 이미 어뷰징 분류가 끝난 기사는 storage 조회 단계에서 제외된다.
        rows = load_pending_article_jobs(conn, batch_size)
        if not rows:
            break

        batch_no += 1
        batch_classified = 0
        batch_failed = 0
        finished_in_batch = 0
        print(f"[classify-batch] {batch_no} -> loaded {len(rows)} pending items")

        for row in rows:
            job_id = row["job_id"]
            article_id = row["article_id"]
            link = row["link"] or f"article:{article_id}"

            try:
                decision = detector.classify(
                    ArticleInput(
                        title=row["title"] or "",
                        subtitle=row["summary"] or "",
                        category=row["category"] or "",
                        content=row["content"] or "",
                    )
                )
            except Exception as exc:
                message = str(exc)
                next_attempt = (row["attempts"] or 0) + 1
                next_status = "failed" if next_attempt >= max_attempts else "pending"
                with conn.transaction():
                    mark_article_job_failed(conn, job_id, message, max_attempts)
                print(
                    "[warn] classify failed: "
                    f"job={job_id} article={article_id} link={link} "
                    f"(attempt={next_attempt}/{max_attempts}, next_status={next_status}, "
                    f"error={message})"
                )
                failed += 1
                batch_failed += 1
                finished_in_batch += 1
                continue

            with conn.transaction():
                save_article_ai_result(
                    conn,
                    article_id=article_id,
                    abuse_score=decision.abuse_score,
                    abuse_label=decision.abuse_label,
                )

            print(
                "[classify] "
                f"job={job_id} article={article_id} link={link} -> {decision.abuse_label} "
                f"score={decision.abuse_score:.4f} reason={decision.decision_reason} "
                f"p1={decision.p1.label}:{decision.p1.score:.4f}/{decision.p1.threshold:.4f} "
                f"p2={decision.p2.label}:{decision.p2.score:.4f}/{decision.p2.threshold:.4f} "
                "next=summary"
            )
            classified += 1
            batch_classified += 1
            finished_in_batch += 1

        print(
            f"[classify-batch] {batch_no} -> "
            f"classified={batch_classified}, failed={batch_failed}, total_done={classified}"
        )

        if finished_in_batch == 0:
            break

    if classified == 0 and failed == 0:
        print("[classify] no pending ready articles")
    else:
        print(f"[classify] completed -> classified={classified}, failed_attempts={failed}")

    return classified
