"""
Database module for Bohemia Conductor Bot.
Uses SQLite for persistent storage of bot settings, admin list, and other data.
"""

import aiosqlite
import os
from datetime import datetime

# Database file path (stored in the bot directory)
DB_PATH = os.path.join(os.path.dirname(__file__), 'bot_data.db')


async def init_db():
    """
    Initialize the database and create tables if they don't exist.
    Should be called once when the bot starts.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Bot settings table (key-value store for configuration)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT,
                updated_by_user_id INTEGER,
                updated_by_username TEXT
            )
        ''')

        # Admins table (users who can run admin commands)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                added_at TEXT,
                added_by_user_id INTEGER,
                added_by_username TEXT
            )
        ''')

        # Analytics table (for tracking bot usage - future use)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                event_data TEXT,
                user_id INTEGER,
                username TEXT,
                timestamp TEXT
            )
        ''')

        await db.commit()
        print(f"[DEBUG] Database initialized at {DB_PATH}")


# =============================================================================
# BOT SETTINGS FUNCTIONS
# =============================================================================

async def get_setting(key: str, default=None):
    """
    Get a setting value from the database.
    Returns default if the key doesn't exist.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT value FROM bot_settings WHERE key = ?', (key,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]
            return default


async def set_setting(key: str, value: str, user_id: int = None, username: str = None):
    """
    Set a setting value in the database.
    Tracks who made the change and when.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO bot_settings (key, value, updated_at, updated_by_user_id, updated_by_username)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_by_username = excluded.updated_by_username
        ''', (key, value, datetime.now().isoformat(), user_id, username))
        await db.commit()
        print(f"[DEBUG] Setting '{key}' updated to '{value}' by {username} ({user_id})")


async def delete_setting(key: str):
    """Delete a setting from the database."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM bot_settings WHERE key = ?', (key,))
        await db.commit()
        print(f"[DEBUG] Setting '{key}' deleted")


async def get_setting_info(key: str):
    """
    Get full information about a setting including who set it and when.
    Returns dict with value, updated_at, updated_by_username or None if not found.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT value, updated_at, updated_by_username FROM bot_settings WHERE key = ?', (key,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    'value': row[0],
                    'updated_at': row[1],
                    'updated_by': row[2]
                }
            return None


# =============================================================================
# CURRENT GB HELPER FUNCTIONS
# =============================================================================

async def get_current_gb():
    """Get the currently set Group Buy form ID."""
    return await get_setting('current_gb_form_id')


async def set_current_gb(form_id: str, user_id: int = None, username: str = None):
    """Set the current Group Buy form ID."""
    await set_setting('current_gb_form_id', form_id, user_id, username)


async def clear_current_gb():
    """Clear the current GB setting (revert to auto-detection)."""
    await delete_setting('current_gb_form_id')


async def get_current_gb_info():
    """Get full info about who set the current GB and when."""
    return await get_setting_info('current_gb_form_id')


# =============================================================================
# ADMIN MANAGEMENT FUNCTIONS
# =============================================================================

async def is_admin(user_id: int) -> bool:
    """Check if a user is an admin."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT 1 FROM admins WHERE user_id = ?', (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None


async def add_admin(user_id: int, username: str, added_by_user_id: int = None, added_by_username: str = None):
    """Add a user as an admin."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO admins (user_id, username, added_at, added_by_user_id, added_by_username)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username
        ''', (user_id, username, datetime.now().isoformat(), added_by_user_id, added_by_username))
        await db.commit()
        print(f"[DEBUG] Admin added: {username} ({user_id}) by {added_by_username}")


async def remove_admin(user_id: int):
    """Remove a user from admins."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM admins WHERE user_id = ?', (user_id,))
        await db.commit()
        print(f"[DEBUG] Admin removed: {user_id}")


async def get_all_admins():
    """Get list of all admins."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id, username, added_at FROM admins') as cursor:
            rows = await cursor.fetchall()
            return [{'user_id': row[0], 'username': row[1], 'added_at': row[2]} for row in rows]


async def get_admin_count() -> int:
    """Get the number of admins."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT COUNT(*) FROM admins') as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


# =============================================================================
# ANALYTICS FUNCTIONS (for future use)
# =============================================================================

async def log_event(event_type: str, event_data: str = None, user_id: int = None, username: str = None):
    """Log an analytics event."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO analytics (event_type, event_data, user_id, username, timestamp)
            VALUES (?, ?, ?, ?, ?)
        ''', (event_type, event_data, user_id, username, datetime.now().isoformat()))
        await db.commit()


async def get_event_count(event_type: str, since: str = None) -> int:
    """Get count of events of a specific type, optionally since a timestamp."""
    async with aiosqlite.connect(DB_PATH) as db:
        if since:
            async with db.execute(
                'SELECT COUNT(*) FROM analytics WHERE event_type = ? AND timestamp >= ?',
                (event_type, since)
            ) as cursor:
                row = await cursor.fetchone()
        else:
            async with db.execute(
                'SELECT COUNT(*) FROM analytics WHERE event_type = ?', (event_type,)
            ) as cursor:
                row = await cursor.fetchone()
        return row[0] if row else 0
