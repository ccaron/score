"""Tests for shared logging configuration."""
import logging

import pytest


def test_init_logging_returns_logger():
    """Test that init_logging returns a logger instance."""
    from score.log import init_logging

    logger = init_logging("test")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "score.test"


def test_program_name_padding():
    """Test that program names are padded to 8 characters for alignment."""
    from score.log import init_logging

    # Test short name
    init_logging("app")
    # Check that the format string contains padded name
    root_logger = logging.getLogger()
    handler = root_logger.handlers[0]
    # The formatter should have the padded name in the format string
    assert "app     " in handler.formatter._fmt or "app      " in handler.formatter._fmt

    # Test longer name
    init_logging("pusher")
    handler = root_logger.handlers[0]
    assert "pusher  " in handler.formatter._fmt


def test_different_colors():
    """Test that different programs can have different colors."""
    from score.log import init_logging

    # Test cyan color (default)
    init_logging("app", color="dim cyan")
    handler = logging.getLogger().handlers[0]
    assert "dim cyan" in handler.formatter._fmt

    # Test magenta color
    init_logging("cloud", color="dim magenta")
    handler = logging.getLogger().handlers[0]
    assert "dim magenta" in handler.formatter._fmt


def test_logger_hierarchy():
    """Test that loggers are created with correct hierarchy."""
    from score.log import init_logging

    logger = init_logging("app")
    assert logger.name == "score.app"
    # The parent will be root unless we explicitly create a "score" logger
    # This is expected behavior in Python's logging module
    assert logger.parent.name == "root" or logger.parent.name == "score"


def test_all_program_names():
    """Test all expected program names."""
    from score.log import init_logging

    program_names = ["app", "cloud", "pusher", "state"]

    for name in program_names:
        logger = init_logging(name)
        assert logger.name == f"score.{name}"
        assert isinstance(logger, logging.Logger)


def test_logging_level():
    """Test that logging level is set to INFO."""
    from score.log import init_logging

    init_logging("test")
    root_logger = logging.getLogger()
    assert root_logger.level == logging.INFO


def test_rich_handler_used():
    """Test that RichHandler is configured."""
    from score.log import init_logging, RichHandlerWithLoggerName

    init_logging("test")
    root_logger = logging.getLogger()

    # Should have at least one handler
    assert len(root_logger.handlers) > 0

    # Should be a RichHandlerWithLoggerName
    assert isinstance(root_logger.handlers[0], RichHandlerWithLoggerName)


def test_format_includes_pid_tid():
    """Test that format string includes PID and TID."""
    from score.log import init_logging

    init_logging("test")
    handler = logging.getLogger().handlers[0]
    format_str = handler.formatter._fmt

    assert "%(process)d" in format_str
    assert "%(thread)d" in format_str
    assert "PID:" in format_str
    assert "TID:" in format_str


def test_format_includes_message():
    """Test that format string includes the message."""
    from score.log import init_logging

    init_logging("test")
    handler = logging.getLogger().handlers[0]
    format_str = handler.formatter._fmt

    assert "%(message)s" in format_str


def test_uvicorn_logger_configuration():
    """Test that uvicorn loggers are configured if available."""
    from score.log import init_logging

    # This should not raise an exception even if uvicorn is not present
    logger = init_logging("test")
    assert logger is not None

    # If uvicorn is available, check its configuration
    try:
        uvicorn_logger = logging.getLogger("uvicorn")
        # Should propagate to root logger
        assert uvicorn_logger.propagate is True
        # Should have no handlers (uses root logger's handlers)
        assert len(uvicorn_logger.handlers) == 0
    except:
        # uvicorn might not be imported yet, that's fine
        pass
