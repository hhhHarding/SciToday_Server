"""Deprecated CLI compatibility entry point for the unified PDF pipeline.

External cron jobs should migrate to the backend API or import
``tasks.run_pdf_watch`` directly.  All PDF matching and summarization logic now
lives in ``tasks.py``.
"""

import logging

import tasks


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger(__name__).warning(
        "pdf_watch_summarize.py 已弃用；请迁移到 tasks.run_pdf_watch 或后端 API"
    )
    count = tasks.run_pdf_watch()
    print(f"PDF 监控完成: 新增 {count} 篇")


if __name__ == "__main__":
    main()
