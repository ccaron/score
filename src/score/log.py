"""
Shared logging configuration for all score modules.

Provides Rich-based logging with program name prefixes.
"""
import logging
from rich.console import ConsoleRenderable
from rich.logging import RichHandler


class RichHandlerWithLoggerName(RichHandler):
    """Custom RichHandler that displays actual file path and line number."""

    def render(
        self,
        *,
        record: logging.LogRecord,
        traceback,
        message_renderable: ConsoleRenderable,
    ):
        # Keep the actual file path and line number for display
        # Rich will show this on the right side
        return super().render(
            record=record,
            traceback=traceback,
            message_renderable=message_renderable,
        )


def init_logging(program_name: str, color: str = "dim cyan"):
    """
    Configure Rich logging with process/thread info and logger names.

    Args:
        program_name: Name of the program (e.g., "app", "cloud", "pusher")
        color: Rich color for PID/TID display (e.g., "dim cyan", "dim magenta")
    """
    # Pad program name to 8 characters for alignment
    padded_name = f"{program_name:<8}"

    logging.basicConfig(
        level=logging.INFO,
        format=f"[bold]{padded_name}[/bold] [{color}][PID: %(process)d TID: %(thread)d][/{color}] %(message)s",
        datefmt="[%X]",
        handlers=[RichHandlerWithLoggerName(markup=True)],
        force=True,
    )

    # Configure uvicorn's loggers to use our Rich handler if present
    try:
        for logger_name in ["uvicorn", "uvicorn.error"]:
            uvicorn_logger = logging.getLogger(logger_name)
            uvicorn_logger.handlers = []
            uvicorn_logger.propagate = True

        # Reduce noise from access logs
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    except:
        pass  # uvicorn might not be imported

    # Set logger name prefix for this program
    logger = logging.getLogger(f"score.{program_name}")
    logger.info(f"Logging initialized for {program_name}")

    return logger
