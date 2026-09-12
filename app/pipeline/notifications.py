"""Process-owned notifications for long-running pipeline runs."""
import json
import logging
import time
import urllib.error
import urllib.request

from app.config.settings import FULL_PIPELINE_NTFY_TOPIC

logger = logging.getLogger(__name__)

_PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5}


def send_pipeline_notification(
    *,
    title: str,
    message: str,
    tags: list[str],
    priority: str,
    attempts: int = 3,
) -> bool:
    """Send UTF-8 JSON to ntfy, retrying without masking stage failures."""
    if not FULL_PIPELINE_NTFY_TOPIC:
        logger.error("FULL_PIPELINE_NTFY_TOPIC is not configured; notification not sent")
        return False

    body = json.dumps(
        {
            "topic": FULL_PIPELINE_NTFY_TOPIC,
            "title": title,
            "message": message,
            "tags": tags,
            "priority": _PRIORITIES[priority],
        },
        ensure_ascii=False,
    ).encode("utf-8")

    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            "https://ntfy.sh/",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                if response.status == 200:
                    return True
                logger.warning(
                    "ntfy returned HTTP %s (attempt %s/%s)",
                    response.status,
                    attempt,
                    attempts,
                )
        except (OSError, urllib.error.URLError) as exc:
            logger.warning("ntfy attempt %s/%s failed: %s", attempt, attempts, exc)
        if attempt < attempts:
            time.sleep(attempt)

    logger.error("ntfy notification failed after %s attempts", attempts)
    return False
