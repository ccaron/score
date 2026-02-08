"""
Authentication and session management for Score Cloud.

Provides password hashing, session management, and authentication helpers
for the multi-tenant cloud API.
"""

import hashlib
import logging
import secrets
import sqlite3
import time
from typing import Optional

logger = logging.getLogger("score.auth")

# Session settings
SESSION_DURATION_SECONDS = 24 * 60 * 60  # 24 hours
SESSION_CLEANUP_THRESHOLD = 100  # Clean up expired sessions after this many sessions


# =============================================================================
# Password Hashing (using bcrypt-compatible approach)
# =============================================================================

def hash_password(password: str) -> str:
    """
    Hash a password using SHA-256 with salt.

    Note: In production, use bcrypt. This is a simplified implementation
    that provides reasonable security for development.

    Args:
        password: Plain text password

    Returns:
        Hashed password string in format: salt$hash
    """
    # Generate random salt
    salt = secrets.token_hex(16)

    # Hash password with salt
    pwd_hash = hashlib.sha256((salt + password).encode()).hexdigest()

    return f"{salt}${pwd_hash}"


def verify_password(password: str, password_hash: str) -> bool:
    """
    Verify a password against its hash.

    Args:
        password: Plain text password to verify
        password_hash: Stored hash in format: salt$hash

    Returns:
        True if password matches, False otherwise
    """
    try:
        salt, stored_hash = password_hash.split("$")
        pwd_hash = hashlib.sha256((salt + password).encode()).hexdigest()
        return pwd_hash == stored_hash
    except ValueError:
        return False


# =============================================================================
# User Management
# =============================================================================

def create_user(
    conn: sqlite3.Connection,
    email: str,
    password: str,
    client_id: Optional[str] = None,
    role: str = "admin",
) -> str:
    """
    Create a new user.

    Args:
        conn: Database connection
        email: User email (must be unique)
        password: Plain text password (will be hashed)
        client_id: Client ID (None for super admin)
        role: User role ('super_admin', 'admin', 'viewer')

    Returns:
        user_id of created user

    Raises:
        sqlite3.IntegrityError: If email already exists
    """
    user_id = secrets.token_urlsafe(16)
    password_hash = hash_password(password)
    now = int(time.time())

    conn.execute("""
        INSERT INTO users (user_id, client_id, email, password_hash, role, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)
    """, (user_id, client_id, email, password_hash, role, now))

    logger.info(f"Created user: {email} (role: {role}, client: {client_id or 'super_admin'})")
    return user_id


def authenticate_user(conn: sqlite3.Connection, email: str, password: str) -> Optional[dict]:
    """
    Authenticate a user by email and password.

    Args:
        conn: Database connection
        email: User email
        password: Plain text password

    Returns:
        User dict if authentication successful, None otherwise
    """
    user = conn.execute("""
        SELECT user_id, client_id, email, password_hash, role, is_active
        FROM users
        WHERE email = ?
    """, (email,)).fetchone()

    if not user:
        logger.warning(f"Authentication failed: user not found ({email})")
        return None

    if not user["is_active"]:
        logger.warning(f"Authentication failed: user inactive ({email})")
        return None

    if not verify_password(password, user["password_hash"]):
        logger.warning(f"Authentication failed: invalid password ({email})")
        return None

    # Update last login
    conn.execute("""
        UPDATE users SET last_login_at = ? WHERE user_id = ?
    """, (int(time.time()), user["user_id"]))

    logger.info(f"User authenticated: {email}")

    return {
        "user_id": user["user_id"],
        "client_id": user["client_id"],
        "email": user["email"],
        "role": user["role"],
    }


# =============================================================================
# Session Management
# =============================================================================

def create_session(
    conn: sqlite3.Connection,
    user_id: str,
    active_client_id: Optional[str] = None,
) -> str:
    """
    Create a new session for a user.

    Args:
        conn: Database connection
        user_id: User ID
        active_client_id: Client ID for super admin's active view context

    Returns:
        session_id
    """
    session_id = secrets.token_urlsafe(32)
    now = int(time.time())
    expires_at = now + SESSION_DURATION_SECONDS

    conn.execute("""
        INSERT INTO sessions (session_id, user_id, active_client_id, expires_at, last_activity_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (session_id, user_id, active_client_id, expires_at, now, now))

    # Periodically clean up expired sessions
    session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    if session_count > SESSION_CLEANUP_THRESHOLD:
        cleanup_expired_sessions(conn)

    return session_id


def get_session(conn: sqlite3.Connection, session_id: str) -> Optional[dict]:
    """
    Get session details if valid and not expired.

    Args:
        conn: Database connection
        session_id: Session ID

    Returns:
        Session dict with user info if valid, None otherwise
    """
    now = int(time.time())

    session = conn.execute("""
        SELECT
            s.session_id,
            s.user_id,
            s.active_client_id,
            s.expires_at,
            u.email,
            u.client_id,
            u.role
        FROM sessions s
        JOIN users u ON s.user_id = u.user_id
        WHERE s.session_id = ? AND s.expires_at > ?
    """, (session_id, now)).fetchone()

    if not session:
        return None

    # Update last activity
    conn.execute("""
        UPDATE sessions SET last_activity_at = ? WHERE session_id = ?
    """, (now, session_id))

    return {
        "session_id": session["session_id"],
        "user_id": session["user_id"],
        "email": session["email"],
        "client_id": session["client_id"],
        "active_client_id": session["active_client_id"],
        "role": session["role"],
    }


def delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    """Delete a session (logout)."""
    conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


def cleanup_expired_sessions(conn: sqlite3.Connection) -> int:
    """
    Delete expired sessions.

    Returns:
        Number of sessions deleted
    """
    now = int(time.time())
    result = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
    count = result.rowcount

    if count > 0:
        logger.info(f"Cleaned up {count} expired sessions")

    return count


def update_active_client(
    conn: sqlite3.Connection,
    session_id: str,
    active_client_id: Optional[str],
) -> None:
    """
    Update the active client for a super admin session.

    Args:
        conn: Database connection
        session_id: Session ID
        active_client_id: New active client ID (or None for all clients view)
    """
    conn.execute("""
        UPDATE sessions SET active_client_id = ? WHERE session_id = ?
    """, (active_client_id, session_id))


# =============================================================================
# Helper Functions
# =============================================================================

def get_current_client(session: Optional[dict]) -> Optional[str]:
    """
    Get the current client ID for filtering queries.

    For regular admins: returns their client_id
    For super admins: returns their active_client_id (or None for all clients)

    Args:
        session: Session dict from get_session()

    Returns:
        client_id to filter by, or None for all clients (super admin only)
    """
    if not session:
        return None

    # Super admin can switch between clients
    if session["role"] == "super_admin":
        return session["active_client_id"]

    # Regular admin/viewer is scoped to their client
    return session["client_id"]


def is_super_admin(session: Optional[dict]) -> bool:
    """Check if the current session is a super admin."""
    return session is not None and session["role"] == "super_admin"


def require_role(session: Optional[dict], required_role: str) -> bool:
    """
    Check if session has the required role.

    Role hierarchy: super_admin > admin > viewer

    Args:
        session: Session dict
        required_role: Required role ('viewer', 'admin', or 'super_admin')

    Returns:
        True if session has sufficient role
    """
    if not session:
        return False

    role_hierarchy = {"viewer": 1, "admin": 2, "super_admin": 3}

    user_level = role_hierarchy.get(session["role"], 0)
    required_level = role_hierarchy.get(required_role, 99)

    return user_level >= required_level
