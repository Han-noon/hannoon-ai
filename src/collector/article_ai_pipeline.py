from __future__ import annotations

from abuse_detector import ArticleAbuseDetector, ArticleInput
from summarizer import ExtractiveBertSummarizer

from .storage import (
    load_pending_ai_pipeline_jobs,
    mark_article_job_failed,
    mark_article_job_sent,
    save_article_ai_result,
    save_article_summary_result,
)


def process_pending_articles(
    conn,
    *,
    p1_model_dir: str,
    p2_model_dir: str,
    summary_model_path: str,
    summary_tokenizer_dir: str,
    batch_size: int,
    sentence_count: int,
    max_candidates: int,
    head_candidates: int,
    middle_candidates: int,
    tail_candidates: int,
    abuse_device: str = "auto",
    summary_device: str = "auto",
    classify_max_attempts: int = 3,
    summary_max_attempts: int = 3,
) -> int:
    """기사별로 어뷰징 분류 후 정상 기사만 바로 요약까지 처리한다."""
    _validate_args(
        batch_size=batch_size,
        sentence_count=sentence_count,
        max_candidates=max_candidates,
        head_candidates=head_candidates,
        middle_candidates=middle_candidates,
        tail_candidates=tail_candidates,
        classify_max_attempts=classify_max_attempts,
        summary_max_attempts=summary_max_attempts,
    )

    detector: ArticleAbuseDetector | None = None
    summarizer: ExtractiveBertSummarizer | None = None

    completed = 0
    abuse_done = 0
    summarized = 0
    classify_failed = 0
    summary_failed = 0
    batch_no = 0

    while True:
        rows = load_pending_ai_pipeline_jobs(conn, batch_size)
        if not rows:
            break

        batch_no += 1
        batch_completed = 0
        batch_abuse_done = 0
        batch_summarized = 0
        batch_classify_failed = 0
        batch_summary_failed = 0
        print(f"[pipeline-batch] {batch_no} -> loaded {len(rows)} pending articles")

        for row in rows:
            job_id = row["job_id"]
            article_id = row["article_id"]
            link = row["link"] or f"article:{article_id}"
            print(f"[pipeline] job={job_id} article={article_id} link={link} -> start")

            abuse_label = row["abuse_label"]
            abuse_score = row["abuse_score"]
            needs_classify = abuse_label not in {"abuse", "normal"} or abuse_score is None

            if needs_classify:
                if detector is None:
                    print(
                        "[pipeline] loading abuse models "
                        f"(p1={p1_model_dir}, p2={p2_model_dir}, device={abuse_device})"
                    )
                    detector = ArticleAbuseDetector(
                        p1_model_dir=p1_model_dir,
                        p2_model_dir=p2_model_dir,
                        device=abuse_device,
                    )
                try:
                    decision = detector.classify(
                        ArticleInput(
                            title=row["title"] or "",
                            subtitle=row["rss_summary"] or "",
                            category=row["category"] or "",
                            content=row["content"] or "",
                        )
                    )
                except Exception as exc:
                    message = str(exc)
                    next_attempt = (row["attempts"] or 0) + 1
                    next_status = "failed" if next_attempt >= classify_max_attempts else "pending"
                    with conn.transaction():
                        mark_article_job_failed(conn, job_id, message, classify_max_attempts)
                    print(
                        "[warn] pipeline classify failed: "
                        f"job={job_id} article={article_id} "
                        f"(attempt={next_attempt}/{classify_max_attempts}, "
                        f"next_status={next_status}, error={message})"
                    )
                    classify_failed += 1
                    batch_classify_failed += 1
                    continue

                abuse_label = decision.abuse_label
                abuse_score = decision.abuse_score
                with conn.transaction():
                    save_article_ai_result(
                        conn,
                        article_id=article_id,
                        abuse_score=decision.abuse_score,
                        abuse_label=decision.abuse_label,
                    )
                    if decision.is_abuse:
                        mark_article_job_sent(conn, job_id)

                if decision.is_abuse:
                    print(
                        "[pipeline] "
                        f"job={job_id} article={article_id} -> "
                        f"abuse_result=saved label={decision.abuse_label} "
                        f"score={decision.abuse_score:.4f} reason={decision.decision_reason} "
                        f"p1={decision.p1.label}:{decision.p1.score:.4f}/{decision.p1.threshold:.4f} "
                        f"p2={decision.p2.label}:{decision.p2.score:.4f}/{decision.p2.threshold:.4f} "
                        "summary=skipped job_status=sent"
                    )
                    completed += 1
                    abuse_done += 1
                    batch_completed += 1
                    batch_abuse_done += 1
                    continue

                print(
                    "[pipeline] "
                    f"job={job_id} article={article_id} -> "
                    f"abuse_result=saved label={decision.abuse_label} "
                    f"score={decision.abuse_score:.4f} reason={decision.decision_reason} "
                    f"p1={decision.p1.label}:{decision.p1.score:.4f}/{decision.p1.threshold:.4f} "
                    f"p2={decision.p2.label}:{decision.p2.score:.4f}/{decision.p2.threshold:.4f} "
                    "next=summary"
                )
            else:
                print(
                    "[pipeline] "
                    f"job={job_id} article={article_id} -> "
                    f"abuse_result=already_saved label={abuse_label} "
                    f"score={float(abuse_score):.4f}"
                )

            if abuse_label == "abuse":
                with conn.transaction():
                    mark_article_job_sent(conn, job_id)
                print(
                    "[pipeline] "
                    f"job={job_id} article={article_id} -> "
                    "summary=skipped job_status=sent"
                )
                completed += 1
                abuse_done += 1
                batch_completed += 1
                batch_abuse_done += 1
                continue

            existing_summary = row["ai_summary"]
            if existing_summary:
                with conn.transaction():
                    mark_article_job_sent(conn, job_id)
                print(
                    "[pipeline] "
                    f"job={job_id} article={article_id} -> "
                    f"summary=already_saved chars={len(existing_summary)} job_status=sent"
                )
                completed += 1
                summarized += 1
                batch_completed += 1
                batch_summarized += 1
                continue

            if summarizer is None:
                print(
                    "[pipeline] loading summary model "
                    f"(model={summary_model_path}, tokenizer={summary_tokenizer_dir}, "
                    f"device={summary_device})"
                )
                summarizer = ExtractiveBertSummarizer(
                    model_path=summary_model_path,
                    tokenizer_dir=summary_tokenizer_dir,
                    device=summary_device,
                )

            try:
                summary = summarizer.summarize(
                    title=row["title"] or "",
                    content=row["content"] or "",
                    sentence_count=sentence_count,
                    max_candidates=max_candidates,
                    head_candidates=head_candidates,
                    middle_candidates=middle_candidates,
                    tail_candidates=tail_candidates,
                )
            except Exception as exc:
                message = str(exc)
                next_attempt = (row["attempts"] or 0) + 1
                next_status = "failed" if next_attempt >= summary_max_attempts else "pending"
                with conn.transaction():
                    mark_article_job_failed(conn, job_id, message, summary_max_attempts)
                print(
                    "[warn] pipeline summary failed: "
                    f"job={job_id} article={article_id} "
                    f"(attempt={next_attempt}/{summary_max_attempts}, "
                    f"next_status={next_status}, error={message})"
                )
                summary_failed += 1
                batch_summary_failed += 1
                continue

            with conn.transaction():
                save_article_summary_result(conn, article_id=article_id, summary=summary)
                mark_article_job_sent(conn, job_id)

            print(
                "[pipeline] "
                f"job={job_id} article={article_id} -> "
                f"summary=saved chars={len(summary)} job_status=sent"
            )
            completed += 1
            summarized += 1
            batch_completed += 1
            batch_summarized += 1

        print(
            f"[pipeline-batch] {batch_no} -> "
            f"completed={batch_completed}, abuse={batch_abuse_done}, "
            f"summarized={batch_summarized}, classify_failed={batch_classify_failed}, "
            f"summary_failed={batch_summary_failed}, total_completed={completed}"
        )

    if completed == 0 and classify_failed == 0 and summary_failed == 0:
        print("[pipeline] no pending ready articles")
    else:
        print(
            "[pipeline] completed -> "
            f"completed={completed}, abuse={abuse_done}, summarized={summarized}, "
            f"classify_failed={classify_failed}, summary_failed={summary_failed}"
        )

    return completed


def _validate_args(
    *,
    batch_size: int,
    sentence_count: int,
    max_candidates: int,
    head_candidates: int,
    middle_candidates: int,
    tail_candidates: int,
    classify_max_attempts: int,
    summary_max_attempts: int,
) -> None:
    """파이프라인 실행 전에 배치와 요약 후보 설정을 검증한다."""
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than 0.")
    if sentence_count <= 0:
        raise ValueError("sentence_count must be greater than 0.")
    if max_candidates <= 0:
        raise ValueError("max_candidates must be greater than 0.")
    if head_candidates < 0 or middle_candidates < 0 or tail_candidates < 0:
        raise ValueError("candidate counts must be greater than or equal to 0.")
    if head_candidates + middle_candidates + tail_candidates <= 0:
        raise ValueError("at least one candidate count must be greater than 0.")
    if head_candidates + middle_candidates + tail_candidates > max_candidates:
        raise ValueError("head/middle/tail candidate counts must not exceed max_candidates.")
    if classify_max_attempts <= 0:
        raise ValueError("classify_max_attempts must be greater than 0.")
    if summary_max_attempts <= 0:
        raise ValueError("summary_max_attempts must be greater than 0.")
