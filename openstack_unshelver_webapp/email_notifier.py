"""Send email notifications on unshelve failures via local sendmail."""
from __future__ import annotations

import logging
import subprocess
from typing import Optional

_LOGGER = logging.getLogger(__name__)


def send_failure_notification(
    *,
    recipient: str,
    instance_name: str,
    error_message: str,
    sender: str = "unshelver@jetstream-controller",
) -> bool:
    """Send an email notification when unshelve fails.

    Uses the local sendmail binary so we don't need SMTP credentials.
    Returns True on success, False on failure.
    """
    subject = f"[Cosmosage Unshelver] Failed to wake {instance_name}"
    body = (
        f"The unshelver failed to wake up instance '{instance_name}'.\n\n"
        f"Error: {error_message}\n\n"
        f"This is typically caused by GPU capacity shortages on Jetstream2.\n"
        f"The unshelver will retry automatically on the next wake request.\n\n"
        f"— Cosmosage Unshelver (controller VM)\n"
    )

    try:
        proc = subprocess.run(
            ["sendmail", "-t"],
            input=f"From: {sender}\nTo: {recipient}\nSubject: {subject}\n\n{body}\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            _LOGGER.info("Sent failure notification to %s for %s", recipient, instance_name)
            return True
        _LOGGER.warning("sendmail returned %d: %s", proc.returncode, proc.stderr)
        return False
    except Exception:
        _LOGGER.exception("Failed to send email notification to %s", recipient)
        return False
