"""
Configuration management for score applications.

Uses environment variables with sensible defaults.
"""
import os


class AppConfig:
    """Configuration for score-app."""

    # Server
    HOST = os.getenv("APP_HOST", "0.0.0.0")
    PORT = int(os.getenv("APP_PORT", "8000"))

    # Database
    DB_PATH = os.getenv("APP_DB_PATH", "game.db")

    # Cloud API integration
    CLOUD_API_URL = os.getenv("CLOUD_API_URL", "http://localhost:8001")
    RINK_ID = os.getenv("RINK_ID", "rink-alpha")

    # Pusher
    DEVICE_ID = os.getenv("DEVICE_ID", "device-001")


class CloudConfig:
    """Configuration for score-cloud."""

    # Server
    HOST = os.getenv("CLOUD_HOST", "0.0.0.0")
    PORT = int(os.getenv("CLOUD_PORT", "8001"))

    # Database
    DB_PATH = os.getenv("CLOUD_DB_PATH", "cloud.db")


def get_app_config():
    """Get configuration for score-app."""
    return AppConfig


def get_cloud_config():
    """Get configuration for score-cloud."""
    return CloudConfig


def print_config(config_class):
    """Print configuration for debugging."""
    print(f"\n{'='*60}")
    print(f"{config_class.__name__} Configuration:")
    print(f"{'='*60}")
    for attr in dir(config_class):
        if attr.isupper():
            value = getattr(config_class, attr)
            # Mask sensitive values if needed
            print(f"  {attr:20} = {value}")
    print(f"{'='*60}\n")
