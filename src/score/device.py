"""
Device identification and configuration.

Generates a unique device ID based on hardware MAC address and persists it.
"""
import logging
import os
import uuid

logger = logging.getLogger("score.device")


def get_mac_address():
    """
    Get the MAC address of the primary network interface.

    Returns:
        str: MAC address in format "aa:bb:cc:dd:ee:ff"
    """
    try:
        # Try to get MAC from uuid.getnode() which uses hardware address
        mac_int = uuid.getnode()
        mac_hex = f"{mac_int:012x}"
        mac_address = ":".join([mac_hex[i:i+2] for i in range(0, 12, 2)])
        return mac_address
    except Exception as e:
        logger.warning(f"Failed to get MAC address: {e}")
        return None


def generate_device_id():
    """
    Generate a unique device ID based on MAC address.

    Format: "dev-{mac-last-6-chars}" (e.g., "dev-aabbcc")

    Returns:
        str: Device ID
    """
    mac = get_mac_address()

    if mac:
        # Use last 6 characters of MAC (last 3 bytes)
        mac_clean = mac.replace(":", "")[-6:]
        device_id = f"dev-{mac_clean}"
        logger.info(f"Generated device ID from MAC: {device_id}")
        return device_id
    else:
        # Fallback: generate random UUID
        random_id = uuid.uuid4().hex[:8]
        device_id = f"dev-{random_id}"
        logger.warning(f"Using random device ID (no MAC available): {device_id}")
        return device_id


def get_device_id(persist_path="/tmp/score-device-id"):
    """
    Get or generate device ID, persisting it to disk.

    On first run, generates ID and saves to persist_path.
    On subsequent runs, reads from persist_path.

    Args:
        persist_path: Path to file storing device ID

    Returns:
        str: Device ID
    """
    # Check if device ID already exists
    if os.path.exists(persist_path):
        try:
            with open(persist_path, 'r') as f:
                device_id = f.read().strip()
                if device_id:
                    logger.info(f"Loaded device ID from {persist_path}: {device_id}")
                    return device_id
        except Exception as e:
            logger.warning(f"Failed to read device ID from {persist_path}: {e}")

    # Generate new device ID
    device_id = generate_device_id()

    # Persist to disk
    try:
        # Create directory if needed
        persist_dir = os.path.dirname(persist_path)
        if persist_dir:
            os.makedirs(persist_dir, exist_ok=True)

        with open(persist_path, 'w') as f:
            f.write(device_id)
        logger.info(f"Persisted device ID to {persist_path}: {device_id}")
    except Exception as e:
        logger.error(f"Failed to persist device ID to {persist_path}: {e}")

    return device_id


def format_device_id_for_display(device_id):
    """
    Format device ID for display on screen.

    Args:
        device_id: Raw device ID (e.g., "dev-aabbcc")

    Returns:
        str: Formatted ID for display (e.g., "dev-aabbcc")
    """
    return device_id
