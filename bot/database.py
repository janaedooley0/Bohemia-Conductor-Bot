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

        # Forms list table (curated list of forms to show in /listforms)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS forms_list (
                form_id TEXT PRIMARY KEY,
                form_title TEXT,
                added_at TEXT,
                added_by_user_id INTEGER,
                added_by_username TEXT
            )
        ''')

        # Reminder subscriptions table (users who want deadline reminders)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reminder_subscriptions (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                username TEXT,
                subscribed_at TEXT,
                enabled INTEGER DEFAULT 1
            )
        ''')

        # Scheduled reminders table (for tracking sent reminders)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reminder_type TEXT NOT NULL,
                target_date TEXT,
                message TEXT,
                sent_at TEXT,
                sent_to_count INTEGER DEFAULT 0
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
# DEADLINE HELPER FUNCTIONS
# =============================================================================

async def get_deadline():
    """Get the manually set deadline for the current GB."""
    return await get_setting('current_gb_deadline')


async def set_deadline(deadline: str, user_id: int = None, username: str = None):
    """Set the deadline for the current GB."""
    await set_setting('current_gb_deadline', deadline, user_id, username)


async def clear_deadline():
    """Clear the manually set deadline."""
    await delete_setting('current_gb_deadline')


async def get_deadline_info():
    """Get full info about who set the deadline and when."""
    return await get_setting_info('current_gb_deadline')


# =============================================================================
# VENDORS HELPER FUNCTIONS
# =============================================================================

async def get_vendors():
    """Get the manually set vendors for the current GB."""
    return await get_setting('current_gb_vendors')


async def set_vendors(vendors: str, user_id: int = None, username: str = None):
    """Set the vendors for the current GB."""
    await set_setting('current_gb_vendors', vendors, user_id, username)


async def clear_vendors():
    """Clear the manually set vendors."""
    await delete_setting('current_gb_vendors')


async def get_vendors_info():
    """Get full info about who set the vendors and when."""
    return await get_setting_info('current_gb_vendors')


# =============================================================================
# STATUS HELPER FUNCTIONS
# =============================================================================

async def get_status():
    """Get the current GB status."""
    return await get_setting('current_gb_status')


async def set_status(status: str, user_id: int = None, username: str = None):
    """Set the status for the current GB."""
    await set_setting('current_gb_status', status, user_id, username)


async def clear_status():
    """Clear the manually set status."""
    await delete_setting('current_gb_status')


async def get_status_info():
    """Get full info about who set the status and when."""
    return await get_setting_info('current_gb_status')


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


async def get_analytics_summary(days: int = 7):
    """
    Get analytics summary for the last N days.
    Returns dict with event counts by type and daily breakdown.
    """
    from datetime import datetime, timedelta

    since = (datetime.now() - timedelta(days=days)).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        # Get counts by event type
        async with db.execute(
            '''SELECT event_type, COUNT(*) as count
               FROM analytics
               WHERE timestamp >= ?
               GROUP BY event_type
               ORDER BY count DESC''',
            (since,)
        ) as cursor:
            rows = await cursor.fetchall()
            by_type = {row[0]: row[1] for row in rows}

        # Get total events
        async with db.execute(
            'SELECT COUNT(*) FROM analytics WHERE timestamp >= ?',
            (since,)
        ) as cursor:
            row = await cursor.fetchone()
            total = row[0] if row else 0

        # Get unique users
        async with db.execute(
            'SELECT COUNT(DISTINCT user_id) FROM analytics WHERE timestamp >= ? AND user_id IS NOT NULL',
            (since,)
        ) as cursor:
            row = await cursor.fetchone()
            unique_users = row[0] if row else 0

        # Get daily counts
        async with db.execute(
            '''SELECT DATE(timestamp) as date, COUNT(*) as count
               FROM analytics
               WHERE timestamp >= ?
               GROUP BY DATE(timestamp)
               ORDER BY date DESC''',
            (since,)
        ) as cursor:
            rows = await cursor.fetchall()
            daily = {row[0]: row[1] for row in rows}

        return {
            'total_events': total,
            'unique_users': unique_users,
            'by_type': by_type,
            'daily': daily,
            'period_days': days
        }


async def get_recent_events(limit: int = 20, event_type: str = None):
    """Get the most recent events, optionally filtered by type."""
    async with aiosqlite.connect(DB_PATH) as db:
        if event_type:
            async with db.execute(
                '''SELECT event_type, event_data, user_id, username, timestamp
                   FROM analytics
                   WHERE event_type = ?
                   ORDER BY timestamp DESC
                   LIMIT ?''',
                (event_type, limit)
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute(
                '''SELECT event_type, event_data, user_id, username, timestamp
                   FROM analytics
                   ORDER BY timestamp DESC
                   LIMIT ?''',
                (limit,)
            ) as cursor:
                rows = await cursor.fetchall()

        return [
            {
                'event_type': row[0],
                'event_data': row[1],
                'user_id': row[2],
                'username': row[3],
                'timestamp': row[4]
            }
            for row in rows
        ]


# =============================================================================
# FORMS LIST FUNCTIONS (curated list for /listforms)
# =============================================================================

async def add_form_to_list(form_id: str, form_title: str, user_id: int = None, username: str = None):
    """Add a form to the curated forms list."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO forms_list (form_id, form_title, added_at, added_by_user_id, added_by_username)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(form_id) DO UPDATE SET
                form_title = excluded.form_title,
                added_at = excluded.added_at,
                added_by_user_id = excluded.added_by_user_id,
                added_by_username = excluded.added_by_username
        ''', (form_id, form_title, datetime.now().isoformat(), user_id, username))
        await db.commit()
        print(f"[DEBUG] Form added to list: {form_title} ({form_id}) by {username}")


async def remove_form_from_list(form_id: str):
    """Remove a form from the curated forms list."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM forms_list WHERE form_id = ?', (form_id,))
        await db.commit()
        print(f"[DEBUG] Form removed from list: {form_id}")


async def get_forms_list():
    """Get all forms in the curated list."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT form_id, form_title, added_at, added_by_username FROM forms_list ORDER BY added_at DESC'
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    'form_id': row[0],
                    'form_title': row[1],
                    'added_at': row[2],
                    'added_by': row[3]
                }
                for row in rows
            ]


async def is_form_in_list(form_id: str) -> bool:
    """Check if a form is in the curated list."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT 1 FROM forms_list WHERE form_id = ?', (form_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None


# =============================================================================
# REMINDER SUBSCRIPTION FUNCTIONS
# =============================================================================

async def subscribe_to_reminders(user_id: int, chat_id: int, username: str = None):
    """Subscribe a user to deadline reminders."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO reminder_subscriptions (user_id, chat_id, username, subscribed_at, enabled)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                username = excluded.username,
                enabled = 1
        ''', (user_id, chat_id, username, datetime.now().isoformat()))
        await db.commit()
        print(f"[DEBUG] User {username} ({user_id}) subscribed to reminders")


async def unsubscribe_from_reminders(user_id: int):
    """Unsubscribe a user from deadline reminders."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'UPDATE reminder_subscriptions SET enabled = 0 WHERE user_id = ?',
            (user_id,)
        )
        await db.commit()
        print(f"[DEBUG] User {user_id} unsubscribed from reminders")


async def is_subscribed_to_reminders(user_id: int) -> bool:
    """Check if a user is subscribed to reminders."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT enabled FROM reminder_subscriptions WHERE user_id = ?',
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None and row[0] == 1


async def get_all_reminder_subscribers():
    """Get all users who are subscribed to reminders."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT user_id, chat_id, username FROM reminder_subscriptions WHERE enabled = 1'
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {'user_id': row[0], 'chat_id': row[1], 'username': row[2]}
                for row in rows
            ]


async def get_reminder_subscriber_count() -> int:
    """Get the count of reminder subscribers."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT COUNT(*) FROM reminder_subscriptions WHERE enabled = 1'
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def log_sent_reminder(reminder_type: str, target_date: str, message: str, sent_count: int):
    """Log a sent reminder for tracking."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO scheduled_reminders (reminder_type, target_date, message, sent_at, sent_to_count)
            VALUES (?, ?, ?, ?, ?)
        ''', (reminder_type, target_date, message, datetime.now().isoformat(), sent_count))
        await db.commit()
