"""Sentry error reporting configuration."""
import os
import logging

logger = logging.getLogger(__name__)


def init_sentry() -> bool:
    """
    Initialize Sentry SDK if SENTRY_DSN env var is set.
    Returns True if initialized, False if skipped (no DSN configured).
    """
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("SENTRY_DSN not set — Sentry error reporting disabled")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            environment=os.getenv("APP_ENV", "production"),
            release=os.getenv("APP_VERSION", "1.0.0"),
            integrations=[
                FastApiIntegration(),
                SqlalchemyIntegration(),
                LoggingIntegration(
                    level=logging.WARNING,
                    event_level=logging.ERROR,
                ),
            ],
            before_send=_filter_event,
        )
        logger.info("Sentry initialized")
        return True
    except ImportError:
        logger.warning("sentry-sdk not installed — run: pip install sentry-sdk[fastapi]")
        return False
    except Exception as e:
        logger.warning(f"Sentry init failed: {e}")
        return False


def _filter_event(event, hint):
    """Filter out low-signal events before sending to Sentry."""
    # Don't report 4xx client errors
    if "exc_info" in hint:
        exc_type, exc_value, _ = hint["exc_info"]
        if hasattr(exc_value, "status_code") and exc_value.status_code < 500:
            return None
    return event


def capture_ai_error(error: Exception, context: dict = None) -> None:
    """Capture an AI agent error to Sentry with extra context."""
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("component", "ai_agent")
            if context:
                for k, v in context.items():
                    scope.set_extra(k, v)
            sentry_sdk.capture_exception(error)
    except Exception:
        pass
