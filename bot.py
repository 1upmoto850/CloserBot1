import os
import re
import sqlite3
import asyncio
import discord
from discord.ext import tasks
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

CARRIERS = {
    "amam": "AMAM",
    "sbli": "SBLI",
    "ahl": "American Home Life",
    "americo": "Americo",
    "moo": "Mutual of Omaha",
    "nlg": "National Life Group",
    "trans": "Transamerica",
    "uhl": "United Home Life",
    "lga": "Legal & General America",
    "lb": "Liberty Bankers",
}

ADMIN_ROLE_NAMES = {
    "agency owner",
    "partner",
    "managing partner",
    "senior partner",
    "executive partner",
    "regional manager",
    "district manager",
    "sales manager",
    "admin",
    "administrator",
}

DB_FILE = "closerbot.db"
WHALE_THRESHOLD = 1700
CENTRAL = ZoneInfo("America/Chicago")

# ── Brand color palette ──────────────────────────────────────────────────────
C_GOLD    = 0xD4AF37   # Gold  — success, confirmations, AP recorded
C_NAVY    = 0x1B2A4A   # Navy  — neutral info, stats, history
C_GREEN   = 0x27AE60   # Bright green     — goals met, level-up, positive alerts
C_RED     = 0xC0392B   # Red              — deletions, errors
C_ORANGE  = 0xE67E22   # Orange           — whale alerts, weekly recap
C_PURPLE  = 0x8E44AD   # Purple           — rank changes, competitive alerts
C_SILVER  = 0x95A5A6   # Silver           — admin/utility embeds

STATUS_LEVELS = [
    (75000, "👑 God Mode"),
    (60000, "🤴 King"),
    (50000, "🏆 Legend"),
    (40000, "🦍 Beast Mode"),
    (30000, "🚀 Elite"),
    (20000, "💎 Expert"),
    (15000, "⚡ Pro"),
    (10000, "🔥 Closer"),
    (7500, "🌱 Rookie"),
    (0, "😅 Noob"),
]

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)

conn = sqlite3.connect(DB_FILE)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS ap_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    amount REAL NOT NULL,
    carrier TEXT NOT NULL,
    created_at TEXT NOT NULL
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS goals (
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    period TEXT NOT NULL,
    amount REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id, period)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS settings (
    guild_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (guild_id, key)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    admin_id TEXT NOT NULL,
    admin_name TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS ap_overrides (
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    period TEXT NOT NULL,
    amount REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id, period)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS period_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    period_type TEXT NOT NULL,
    period_label TEXT NOT NULL,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    total REAL NOT NULL,
    rank INTEGER NOT NULL,
    saved_at TEXT NOT NULL
)
""")

# ── Migration: add guild_id column to existing tables if missing ──────────────
# Safe to run on every startup — ALTER TABLE is a no-op if column exists would
# raise OperationalError, so we check first.
def _add_col_if_missing(table, col, col_def):
    cur.execute(f"PRAGMA table_info({table})")
    cols = [row["name"] for row in cur.fetchall()]
    if col not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
        # Backfill existing rows with a placeholder so NOT NULL is satisfied
        cur.execute(f"UPDATE {table} SET {col} = '0' WHERE {col} IS NULL")
        print(f"Migration: added {col} to {table}")

for tbl in ("ap_entries", "goals", "settings", "audit_log", "ap_overrides", "period_snapshots"):
    _add_col_if_missing(tbl, "guild_id", "TEXT NOT NULL DEFAULT '0'")

conn.commit()


def money(amount):
    return f"${amount:,.2f}"


def now_central():
    return datetime.now(CENTRAL)


def now_iso():
    return now_central().isoformat()


def get_start(period):
    now = now_central()

    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "week":
        start = now - timedelta(days=now.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    return now


def is_admin(member):
    if member.guild_permissions.administrator:
        return True

    role_names = {role.name.lower() for role in member.roles}

    return bool(role_names.intersection(ADMIN_ROLE_NAMES))


async def send_admin_error(channel):
    err = make_embed("❌ Admin Only", color=C_RED)
    err.description = "You need a manager or admin role to use this command."
    await channel.send(embed=err)


def audit(guild_id, admin, action, details):
    cur.execute("""
    INSERT INTO audit_log (guild_id, admin_id, admin_name, action, details, created_at)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (str(guild_id), str(admin.id), admin.display_name, action, details, now_iso()))
    conn.commit()


def add_ap(guild_id, user_id, username, amount, carrier):
    cur.execute("""
    INSERT INTO ap_entries (guild_id, user_id, username, amount, carrier, created_at)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (str(guild_id), str(user_id), username, amount, carrier, now_iso()))
    conn.commit()
    return cur.lastrowid


def get_entry(guild_id, entry_id):
    cur.execute("SELECT * FROM ap_entries WHERE guild_id = ? AND id = ?", (str(guild_id), entry_id))
    return cur.fetchone()


def edit_entry_amount(entry_id, new_amount):
    cur.execute("UPDATE ap_entries SET amount = ? WHERE id = ?", (new_amount, entry_id))
    conn.commit()


def edit_entry_carrier(entry_id, new_carrier):
    cur.execute("UPDATE ap_entries SET carrier = ? WHERE id = ?", (new_carrier, entry_id))
    conn.commit()


def delete_entry(entry_id):
    cur.execute("DELETE FROM ap_entries WHERE id = ?", (entry_id,))
    conn.commit()


def set_override(guild_id, user_id, username, period, amount):
    cur.execute("""
    INSERT OR REPLACE INTO ap_overrides (guild_id, user_id, username, period, amount, updated_at)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (str(guild_id), str(user_id), username, period, amount, now_iso()))
    conn.commit()


def get_override_row(guild_id, user_id, period):
    cur.execute("""
    SELECT amount, updated_at FROM ap_overrides
    WHERE guild_id = ? AND user_id = ? AND period = ?
    """, (str(guild_id), str(user_id), period))
    return cur.fetchone()


def get_override(guild_id, user_id, period):
    row = get_override_row(guild_id, user_id, period)
    return row["amount"] if row else None


def delete_override(guild_id, user_id, period):
    cur.execute("""
    DELETE FROM ap_overrides
    WHERE guild_id = ? AND user_id = ? AND period = ?
    """, (str(guild_id), str(user_id), period))
    conn.commit()


def set_goal(guild_id, user_id, period, amount):
    cur.execute("""
    INSERT OR REPLACE INTO goals (guild_id, user_id, period, amount)
    VALUES (?, ?, ?, ?)
    """, (str(guild_id), str(user_id), period, amount))
    conn.commit()


def get_goal(guild_id, user_id, period):
    cur.execute("""
    SELECT amount FROM goals
    WHERE guild_id = ? AND user_id = ? AND period = ?
    """, (str(guild_id), str(user_id), period))
    row = cur.fetchone()
    return row["amount"] if row else 0


def raw_user_total(guild_id, user_id, period):
    start = get_start(period)
    cur.execute("""
    SELECT COALESCE(SUM(amount), 0) total
    FROM ap_entries
    WHERE guild_id = ? AND user_id = ? AND created_at >= ?
    """, (str(guild_id), str(user_id), start.isoformat()))
    return cur.fetchone()["total"]


def user_total(guild_id, user_id, period):
    override = get_override_row(guild_id, user_id, period)
    if override is not None:
        cur.execute("""
        SELECT COALESCE(SUM(amount), 0) total
        FROM ap_entries
        WHERE guild_id = ? AND user_id = ? AND created_at > ?
        """, (str(guild_id), str(user_id), override["updated_at"]))
        post_override_total = cur.fetchone()["total"]
        return override["amount"] + post_override_total
    return raw_user_total(guild_id, user_id, period)


def team_total(guild_id, period):
    if period in {"week", "month"}:
        user_ids = set()
        start = get_start(period)
        cur.execute("""
        SELECT DISTINCT user_id FROM ap_entries
        WHERE guild_id = ? AND created_at >= ?
        """, (str(guild_id), start.isoformat()))
        user_ids.update(row["user_id"] for row in cur.fetchall())
        cur.execute("""
        SELECT DISTINCT user_id FROM ap_overrides
        WHERE guild_id = ? AND period = ?
        """, (str(guild_id), period))
        user_ids.update(row["user_id"] for row in cur.fetchall())
        return sum(user_total(guild_id, user_id, period) for user_id in user_ids)

    start = get_start(period)
    cur.execute("""
    SELECT COALESCE(SUM(amount), 0) total
    FROM ap_entries
    WHERE guild_id = ? AND created_at >= ?
    """, (str(guild_id), start.isoformat()))
    return cur.fetchone()["total"]


def leaderboard(guild_id, period, limit=10):
    if period in {"week", "month"}:
        users = {}
        start = get_start(period)
        cur.execute("""
        SELECT user_id, username, SUM(amount) total
        FROM ap_entries
        WHERE guild_id = ? AND created_at >= ?
        GROUP BY user_id
        """, (str(guild_id), start.isoformat()))
        for row in cur.fetchall():
            users[row["user_id"]] = {
                "user_id": row["user_id"],
                "username": row["username"],
                "total": row["total"] or 0,
            }
        cur.execute("""
        SELECT user_id, username, amount
        FROM ap_overrides
        WHERE guild_id = ? AND period = ?
        """, (str(guild_id), period))
        for row in cur.fetchall():
            users[row["user_id"]] = {
                "user_id": row["user_id"],
                "username": row["username"],
                "total": user_total(guild_id, row["user_id"], period),
            }
        sorted_rows = sorted(users.values(), key=lambda item: item["total"], reverse=True)[:limit]
        return sorted_rows

    start = get_start(period)
    cur.execute("""
    SELECT user_id, username, SUM(amount) total
    FROM ap_entries
    WHERE guild_id = ? AND created_at >= ?
    GROUP BY user_id
    ORDER BY total DESC
    LIMIT ?
    """, (str(guild_id), start.isoformat(), limit))
    return cur.fetchall()


def rank_for_user(guild_id, user_id, period):
    rows = leaderboard(guild_id, period, 10000)
    for index, row in enumerate(rows, start=1):
        if row["user_id"] == str(user_id):
            return index
    return None


def user_history(guild_id, user_id, limit=10):
    cur.execute("""
    SELECT * FROM ap_entries
    WHERE guild_id = ? AND user_id = ?
    ORDER BY id DESC
    LIMIT ?
    """, (str(guild_id), str(user_id), limit))
    return cur.fetchall()


def recent_entries(guild_id, limit=10):
    cur.execute("""
    SELECT * FROM ap_entries
    WHERE guild_id = ?
    ORDER BY id DESC
    LIMIT ?
    """, (str(guild_id), limit))
    return cur.fetchall()


def leaderboard_snapshot(guild_id, period="week", limit=10):
    rows = leaderboard(guild_id, period, limit)
    snapshot = {}
    for index, row in enumerate(rows, start=1):
        snapshot[row["user_id"]] = {
            "rank": index,
            "username": row["username"],
            "total": row["total"],
        }
    return snapshot


def progress_text(total, goal):
    if not goal:
        return "No goal set"
    return f"{money(total)} / {money(goal)} ({total / goal * 100:.1f}%)"


def progress_bar(value, max_value, size=12):
    if max_value <= 0:
        return "░" * size

    filled = round((value / max_value) * size)
    filled = min(filled, size)
    empty = size - filled

    return "█" * filled + "░" * empty


def current_status(month_total):
    for threshold, title in STATUS_LEVELS:
        if month_total >= threshold:
            return threshold, title
    return 0, "😅 Noob"


def next_status(month_total):
    ascending = sorted(STATUS_LEVELS, key=lambda item: item[0])

    for threshold, title in ascending:
        if threshold > month_total:
            return threshold, title

    return None, None


def status_progress_text(month_total):
    current_threshold, current_title = current_status(month_total)
    next_threshold, next_title = next_status(month_total)

    if not next_threshold:
        return f"{current_title}\nTop status reached."

    remaining = next_threshold - month_total
    bar = progress_bar(month_total - current_threshold, next_threshold - current_threshold, 12)

    return (
        f"{current_title}\n"
        f"Next: {next_title}\n"
        f"`{bar}` {money(remaining)} to go"
    )


def set_setting(guild_id, key, value):
    cur.execute("""
    INSERT OR REPLACE INTO settings (guild_id, key, value)
    VALUES (?, ?, ?)
    """, (str(guild_id), key, str(value)))
    conn.commit()


def get_setting(guild_id, key):
    cur.execute("SELECT value FROM settings WHERE guild_id = ? AND key = ?", (str(guild_id), key))
    row = cur.fetchone()
    return row["value"] if row else None


def get_output_channel(guild_id, fallback_channel):
    """Return the configured output channel for this guild, or fall back."""
    channel_id = get_setting(guild_id, "output_channel_id")
    if channel_id:
        ch = client.get_channel(int(channel_id))
        if ch:
            return ch
    return fallback_channel


def get_all_guild_ids():
    """Return all guild IDs that have any settings configured (for scheduler use)."""
    cur.execute("SELECT DISTINCT guild_id FROM settings WHERE guild_id != '0'")
    return [row["guild_id"] for row in cur.fetchall()]


def make_embed(title, description=None, color=C_GOLD):
    return discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=now_central()
    )


def leaderboard_embed(guild_id, period):
    rows = leaderboard(guild_id, period)

    title_map = {
        "today": "📅 Today's Leaderboard",
        "week": "📈 Weekly Leaderboard",
        "month": "👑 Monthly Leaderboard",
    }
    color_map = {
        "today": C_NAVY,
        "week": C_GOLD,
        "month": C_PURPLE,
    }

    embed = make_embed(title_map[period], color=color_map[period])

    if not rows:
        embed.description = "No AP submitted yet — be the first on the board."
        return embed

    medals = ["🥇", "🥈", "🥉"]
    text = ""

    for i, row in enumerate(rows, start=1):
        icon = medals[i - 1] if i <= 3 else f"{i}."
        text += f"{icon} **{row['username']}**: {money(row['total'])}\n"

    embed.description = text
    embed.add_field(name="Team Total", value=money(team_total(guild_id, period)), inline=False)
    return embed


def scoreboard_embed(guild_id):
    rows = leaderboard(guild_id, "week", 10)

    embed = make_embed("🏆 LIVE SCOREBOARD", color=C_NAVY)

    if not rows:
        embed.description = "No AP submitted this week yet.\nFirst one up wins the board. 👀"
    else:
        top_total = rows[0]["total"]
        medals = ["🥇", "🥈", "🥉"]
        text = ""

        for i, row in enumerate(rows, start=1):
            icon = medals[i - 1] if i <= 3 else f"`{i}.`"
            bar = progress_bar(row["total"], top_total)
            month_total_val = user_total(guild_id, row["user_id"], "month")
            _, status = current_status(month_total_val)
            text += (
                f"{icon} **{row['username']}**  ·  {status}\n"
                f"`{bar}` {money(row['total'])}\n\n"
            )

        embed.description = text

    embed.add_field(name="💰 Today", value=money(team_total(guild_id, "today")), inline=True)
    embed.add_field(name="📈 This Week", value=money(team_total(guild_id, "week")), inline=True)
    embed.add_field(name="👑 This Month", value=money(team_total(guild_id, "month")), inline=True)
    embed.set_footer(text=f"Updated {now_central().strftime('%I:%M %p CT')}")

    return embed


async def update_scoreboard(guild_id):
    channel_id = get_setting(guild_id, "scoreboard_channel_id")
    message_id = get_setting(guild_id, "scoreboard_message_id")

    if not channel_id or not message_id:
        return

    channel = client.get_channel(int(channel_id))
    if not channel:
        return

    try:
        msg = await channel.fetch_message(int(message_id))
        await msg.edit(embed=scoreboard_embed(guild_id))
    except discord.NotFound:
        print(f"Scoreboard message missing for guild {guild_id}, re-pinning...")
        msg = await channel.send(embed=scoreboard_embed(guild_id))
        set_setting(guild_id, "scoreboard_message_id", msg.id)
    except discord.Forbidden:
        print(f"Scoreboard update failed for guild {guild_id}: missing permissions.")
    except Exception as e:
        print(f"Scoreboard update failed for guild {guild_id}: {e}")


async def send_position_change_alert(message, old_snapshot, new_snapshot, out):
    user_id = str(message.author.id)

    old_data = old_snapshot.get(user_id)
    new_data = new_snapshot.get(user_id)

    if not new_data:
        return

    old_rank = old_data["rank"] if old_data else None
    new_rank = new_data["rank"]

    moved_into_top_10 = old_rank is None and new_rank <= 10
    moved_up = old_rank is not None and new_rank < old_rank

    if not moved_into_top_10 and not moved_up:
        return

    passed_names = []

    if old_rank is None:
        for other_id, other_data in old_snapshot.items():
            if other_data["rank"] >= new_rank:
                passed_names.append(other_data["username"])
    else:
        for other_id, other_data in old_snapshot.items():
            if new_rank <= other_data["rank"] < old_rank:
                passed_names.append(other_data["username"])

    if new_rank == 1:
        alert = make_embed("👑 TAKING THE LEAD", color=C_GOLD)
        alert.description = (
            f"<@{message.author.id}> just moved to **#1** on the weekly board.\n\n"
            f"🥇 Weekly AP: **{money(new_data['total'])}**\n"
            f"Someone better start dialing. 🔥"
        )
        await out.send(embed=alert)
        return

    if moved_into_top_10:
        alert = make_embed("🚀 BREAKING INTO THE TOP 10", color=C_PURPLE)
        alert.description = (
            f"<@{message.author.id}> just cracked the weekly Top 10!\n\n"
            f"📈 New Rank: **#{new_rank}**  ·  AP: **{money(new_data['total'])}**"
        )
        await out.send(embed=alert)
        return

    if moved_up:
        passed_text = ", ".join(f"**{n}**" for n in passed_names[:3]) if passed_names else "the competition"
        alert = make_embed(f"⬆️ MOVING UP — #{old_rank} → #{new_rank}", color=C_PURPLE)
        alert.description = (
            f"<@{message.author.id}> just passed {passed_text}.\n\n"
            f"Weekly AP: **{money(new_data['total'])}**"
        )
        await out.send(embed=alert)


async def send_level_up_alert(message, old_month_total, new_month_total, out):
    old_threshold, old_status = current_status(old_month_total)
    new_threshold, new_status = current_status(new_month_total)

    if new_threshold <= old_threshold:
        return

    alert = make_embed("🎉 STATUS UPGRADE", color=C_GREEN)
    alert.description = (
        f"<@{message.author.id}> just leveled up!\n\n"
        f"**{old_status}  →  {new_status}**\n\n"
        f"Monthly AP: **{money(new_month_total)}**"
    )

    await out.send(embed=alert)


async def send_whale_alert(message, amount, carrier_code, week_total, out):
    if amount < WHALE_THRESHOLD:
        return

    whale = make_embed("🐋 WHALE ALERT", color=C_ORANGE)
    whale.description = (
        f"<@{message.author.id}> just landed a **{money(amount)}** policy.\n\n"
        f"🏢 Carrier: **{CARRIERS[carrier_code]}**\n"
        f"📈 Weekly Running Total: **{money(week_total)}**\n\n"
        f"That's how it's done. 💰"
    )

    await out.send(embed=whale)


def format_entry(entry):
    created = entry["created_at"].split("T")[0]
    return (
        f"**#{entry['id']}** | **{entry['username']}** | "
        f"{money(entry['amount'])} | {CARRIERS.get(entry['carrier'], entry['carrier'])} | {created}"
    )


def daily_scoreboard_embed(guild_id):
    """Embed for the nightly Mon–Fri 8PM Central automated post."""
    rows = leaderboard(guild_id, "today")
    now = now_central()

    embed = make_embed(
        f"📅 DAILY RECAP — {now.strftime('%A, %B %d')}",
        color=C_NAVY
    )

    if not rows:
        embed.description = "No AP submitted today.\nCome back stronger tomorrow. 💪"
    else:
        medals = ["🥇", "🥈", "🥉"]
        text = ""
        for i, row in enumerate(rows, start=1):
            icon = medals[i - 1] if i <= 3 else f"`{i}.`"
            text += f"{icon} **{row['username']}** — {money(row['total'])}\n"
        embed.description = text

    embed.add_field(name="💰 Today's Team Total", value=money(team_total(guild_id, "today")), inline=True)
    embed.add_field(name="📈 Week-to-Date", value=money(team_total(guild_id, "week")), inline=True)
    embed.add_field(name="👑 Month-to-Date", value=money(team_total(guild_id, "month")), inline=True)
    embed.set_footer(text="Every policy matters.")
    return embed


def weekly_recap_embed(guild_id):
    """Embed for the Friday end-of-week recap."""
    rows = leaderboard(guild_id, "week", 10)
    week_start = get_start("week")

    embed = make_embed(
        f"🏁 WEEKLY RECAP — Week of {week_start.strftime('%B %d')}",
        color=C_GOLD
    )

    if not rows:
        embed.description = "No AP submitted this week."
    else:
        medals = ["🥇", "🥈", "🥉"]
        text = ""
        for i, row in enumerate(rows, start=1):
            icon = medals[i - 1] if i <= 3 else f"`{i}.`"
            month_t = user_total(guild_id, row["user_id"], "month")
            _, status = current_status(month_t)
            text += f"{icon} **{row['username']}** — {money(row['total'])}  ·  {status}\n"
        embed.description = text

        top = rows[0]
        embed.add_field(
            name="🌟 Closer of the Week",
            value=f"**{top['username']}** with {money(top['total'])}",
            inline=False
        )

    embed.add_field(name="💰 Team Week Total", value=money(team_total(guild_id, "week")), inline=True)
    embed.add_field(name="👑 Team Month-to-Date", value=money(team_total(guild_id, "month")), inline=True)
    embed.set_footer(text="New week, new goal. Lock in. 🚀")
    return embed


def end_of_month_embed(guild_id):
    """Embed for the end-of-month recap."""
    rows = leaderboard(guild_id, "month", 20)
    now = now_central()

    embed = make_embed(
        f"👑 END OF MONTH — {now.strftime('%B %Y')}",
        color=C_PURPLE
    )

    if not rows:
        embed.description = "No AP recorded this month."
    else:
        medals = ["🥇", "🥈", "🥉"]
        text = ""
        for i, row in enumerate(rows, start=1):
            icon = medals[i - 1] if i <= 3 else f"`{i}.`"
            _, status = current_status(row["total"])
            text += f"{icon} **{row['username']}** — {money(row['total'])}  ·  {status}\n"
        embed.description = text

        top = rows[0]
        embed.add_field(
            name="🏆 Monthly MVP",
            value=f"**{top['username']}** — {money(top['total'])}",
            inline=False
        )

    embed.add_field(name="💰 Agency Month Total", value=money(team_total(guild_id, "month")), inline=False)
    embed.add_field(
        name="📋 Status Tiers",
        value=(
            "👑 God Mode $75k  ·  🤴 King $60k  ·  🏆 Legend $50k\n"
            "🦍 Beast Mode $40k  ·  🚀 Elite $30k  ·  💎 Expert $20k"
        ),
        inline=False
    )
    embed.set_footer(text="Month resets at midnight. New grind starts now.")
    return embed


async def post_to_announcements(guild_id, embed):
    """Post an embed to the configured announcements channel for this guild."""
    channel_id = get_setting(guild_id, "announcements_channel_id")
    if not channel_id:
        return
    channel = client.get_channel(int(channel_id))
    if channel:
        await channel.send(embed=embed)


@tasks.loop(minutes=1)
async def scheduler():
    """Main scheduler — runs for every known guild."""
    now = now_central()
    weekday = now.weekday()
    hour = now.hour
    minute = now.minute
    tomorrow = (now + timedelta(days=1)).date()

    for gid in get_all_guild_ids():
        # Daily scoreboard: Mon–Fri at 20:00 Central
        if weekday <= 4 and hour == 20 and minute == 0:
            await post_to_announcements(gid, daily_scoreboard_embed(gid))

        # Weekly recap + snapshot: every Friday at 20:05 Central
        if weekday == 4 and hour == 20 and minute == 5:
            save_period_snapshot(gid, "week")
            await post_to_announcements(gid, weekly_recap_embed(gid))

        # End-of-month recap + snapshot: last day of the month at 20:10 Central
        if tomorrow.month != now.date().month and hour == 20 and minute == 10:
            save_period_snapshot(gid, "month")
            await post_to_announcements(gid, end_of_month_embed(gid))

        # Monday 00:01 — clear this guild's weekly overrides
        if weekday == 0 and hour == 0 and minute == 1:
            cur.execute("DELETE FROM ap_overrides WHERE guild_id=? AND period='week'", (gid,))
            conn.commit()

        # 1st of month 00:01 — clear this guild's monthly overrides
        if now.day == 1 and hour == 0 and minute == 1:
            cur.execute("DELETE FROM ap_overrides WHERE guild_id=? AND period='month'", (gid,))
            conn.commit()




def period_label_week(dt=None):
    """Returns ISO week label like '2025-W22'."""
    d = (dt or now_central()).date()
    return f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"


def period_label_month(dt=None):
    """Returns month label like '2025-06'."""
    d = (dt or now_central()).date()
    return f"{d.year}-{d.month:02d}"


def save_period_snapshot(guild_id, period_type):
    """
    Archive every rep's current total for this period before it rolls over.
    period_type: "week" or "month"
    """
    label = period_label_week() if period_type == "week" else period_label_month()

    cur.execute(
        "SELECT COUNT(*) FROM period_snapshots WHERE guild_id=? AND period_type=? AND period_label=?",
        (str(guild_id), period_type, label)
    )
    if cur.fetchone()[0] > 0:
        return  # Already saved

    rows = leaderboard(guild_id, period_type, 10000)
    saved_at = now_iso()

    for rank, row in enumerate(rows, start=1):
        if row["total"] > 0:
            cur.execute("""
            INSERT INTO period_snapshots
                (guild_id, period_type, period_label, user_id, username, total, rank, saved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(guild_id), period_type, label, row["user_id"], row["username"],
                  row["total"], rank, saved_at))

    conn.commit()
    print(f"Snapshot saved: guild={guild_id} {period_type} {label} ({len(rows)} reps)")


def get_snapshot(guild_id, period_type, period_label):
    """Fetch archived rows for a specific past period, sorted by rank."""
    cur.execute("""
    SELECT username, user_id, total, rank
    FROM period_snapshots
    WHERE guild_id=? AND period_type=? AND period_label=?
    ORDER BY rank ASC
    """, (str(guild_id), period_type, period_label))
    return cur.fetchall()


def get_alltime_totals(guild_id):
    """Sum all snapshots per rep across all time for a career leaderboard."""
    cur.execute("""
    SELECT user_id, username, SUM(total) as career_total, COUNT(*) as periods
    FROM period_snapshots
    WHERE guild_id=?
    GROUP BY user_id
    ORDER BY career_total DESC
    """, (str(guild_id),))
    return cur.fetchall()


def list_saved_periods(guild_id, period_type):
    """Return all saved period labels for a given type, newest first."""
    cur.execute("""
    SELECT DISTINCT period_label FROM period_snapshots
    WHERE guild_id=? AND period_type=?
    ORDER BY period_label DESC
    """, (str(guild_id), period_type))
    return [row["period_label"] for row in cur.fetchall()]


def reset_all_data(guild_id, wipe_snapshots=False):
    """Wipe all AP entries, overrides, goals, and audit log for this guild only."""
    g = str(guild_id)
    cur.execute("DELETE FROM ap_entries WHERE guild_id=?", (g,))
    cur.execute("DELETE FROM ap_overrides WHERE guild_id=?", (g,))
    cur.execute("DELETE FROM goals WHERE guild_id=?", (g,))
    cur.execute("DELETE FROM audit_log WHERE guild_id=?", (g,))
    if wipe_snapshots:
        cur.execute("DELETE FROM period_snapshots WHERE guild_id=?", (g,))
    conn.commit()

@client.event
async def on_ready():
    print("====================================")
    print("        CLOSERBOT v2.0 — SCHEDULED POSTS")
    print("====================================")
    print(f"✅ Logged in as {client.user}")
    print("Ready to Track Closers 🚀")
    if not scheduler.is_running():
        scheduler.start()


async def process_line(message, raw_line, guild_id, out):
    """Process a single line of a message as a potential bot command or AP entry."""
    raw = raw_line.strip()
    if not raw:
        return
    content = raw.lower()

    if content == "setupscoreboard":
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        # Scoreboard pin must live in the channel where the command was typed
        embed = scoreboard_embed(guild_id)
        msg = await message.channel.send(embed=embed)

        set_setting(guild_id, "scoreboard_channel_id", message.channel.id)
        set_setting(guild_id, "scoreboard_message_id", msg.id)

        confirm = make_embed("✅ Live Scoreboard Set", color=C_GREEN)
        confirm.description = f"Scoreboard pinned in <#{message.channel.id}>. It will auto-update with every AP entry."
        await out.send(embed=confirm)
        return

    if content == "resetalldata confirm":
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        entry_count = cur.execute("SELECT COUNT(*) FROM ap_entries WHERE guild_id=?", (guild_id,)).fetchone()[0]
        reset_all_data(guild_id)
        await update_scoreboard(guild_id)

        embed = make_embed("🗑️ ALL DATA RESET", color=C_RED)
        embed.description = (
            f"**{entry_count} entries** deleted.\n"
            f"All AP totals, overrides, goals, and audit history cleared.\n\n"
            f"Entry IDs will restart from #1.\n"
            f"Scoreboard has been refreshed."
        )
        embed.set_footer(text=f"Reset by {message.author.display_name}")
        await out.send(embed=embed)
        return

    if content == "resetalldata":
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        entry_count = cur.execute("SELECT COUNT(*) FROM ap_entries WHERE guild_id=?", (guild_id,)).fetchone()[0]

        embed = make_embed("⚠️ CONFIRM FULL RESET", color=C_ORANGE)
        embed.description = (
            f"This will permanently delete:\n\n"
            f"• **{entry_count} AP entries**\n"
            f"• All overrides and manual adjustments\n"
            f"• All rep goals\n"
            f"• Full audit log\n\n"
            f"**Kept:** Settings, channel config, and all historical snapshots.\n"
            f"Past week/month results will still be accessible via `pastweek` and `pastmonth`.\n\n"
            f"Type `resetalldata confirm` within 60 seconds to proceed."
        )
        await out.send(embed=embed)
        return

    if content == "setupoutput":
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        set_setting(guild_id, "output_channel_id", message.channel.id)
        embed = make_embed("📤 OUTPUT CHANNEL SET", color=C_GREEN)
        embed.description = (
            f"All bot responses will now post here.\n\n"
            f"Reps can submit AP in any channel — confirmations, alerts, "
            f"leaderboards, and errors will all route to <#{message.channel.id}>.\n\n"
            f"To reset, run `setupoutput` again in a different channel."
        )
        await message.channel.send(embed=embed)
        return

    if content == "setupannouncements":
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        set_setting(guild_id, "announcements_channel_id", message.channel.id)
        embed = make_embed("📣 ANNOUNCEMENTS CHANNEL SET", color=C_NAVY)
        embed.description = (
            f"This channel will now receive:\n\n"
            f"📅 **Daily scoreboard** — Mon–Fri at 8:00 PM Central\n"
            f"🏁 **Weekly recap** — Fridays at 8:05 PM Central\n"
            f"👑 **End-of-month recap** — Last day of month at 8:10 PM Central"
        )
        await out.send(embed=embed)
        return

    # ── AP entry detection ────────────────────────────────────────────────────
    # Tries strict format first, then falls back to fuzzy parsing.
    # Fuzzy: extract any dollar amount + any carrier keyword from anywhere in the message.

    def try_parse_ap(text):
        """
        Returns (amount, carrier_code, was_fuzzy) or None if nothing parseable found.
        Handles inputs like:
          $876 AP MOO 6 months
          $1,236 AP UHL GI 24 mos
          AP 1315.20 Trans
          AP $1051 | UHL GI | 5 MONTHS
          1284 Moo 24mo HH
        """
        # Amount: optional $, digits with optional commas, optional decimal
        amount_pattern = r"\$?([\d,]+(?:\.\d{1,2})?)"
        amount_match = re.search(amount_pattern, text)
        if not amount_match:
            return None
        try:
            amount = float(amount_match.group(1).replace(",", ""))
        except ValueError:
            return None
        if amount <= 0:
            return None

        # Carrier: scan every word for a known carrier code
        words = re.split(r"[\s|]+", text)
        carrier_code = None
        for word in words:
            w = word.lower().strip("$.,")
            if w in CARRIERS:
                carrier_code = w
                break
        if not carrier_code:
            return None

        return amount, carrier_code

    ap_match = re.match(r"^ap\s+\$?([\d,]+(?:\.\d{1,2})?)\s+([a-z]+)$", content)

    # Determine if this message looks like an AP attempt at all
    looks_like_ap = (
        ap_match is not None
        or re.search(r"\bap\b", content) is not None
        or re.search(r"\$[\d,]+", content) is not None
    )

    if looks_like_ap:
        # Try strict format first
        if ap_match:
            parsed = (float(ap_match.group(1).replace(",", "")), ap_match.group(2))
            was_fuzzy = False
        else:
            result = try_parse_ap(content)
            if result:
                parsed = result
                was_fuzzy = True
            else:
                parsed = None
                was_fuzzy = False

        if parsed is None:
            err = make_embed("❌ Couldn't Parse Your Entry", color=C_RED)
            err.description = (
                "I found something that looked like an AP entry but couldn't read the amount or carrier.\n\n"
                "**Format:** `ap [amount] [carrier]`\n"
                "**Example:** `ap 1209 americo`\n\n"
                "**Carrier codes:** `amam` `sbli` `ahl` `americo` `moo` `nlg` `trans` `uhl` `lga`"
            )
            await out.send(embed=err)
            return

        # amount and carrier_code resolved by try_parse_ap above

        old_top_10 = leaderboard_snapshot(guild_id, "week", 10)
        old_month_total = user_total(guild_id, message.author.id, "month")

        entry_id = add_ap(
            guild_id,
            message.author.id,
            message.author.display_name,
            amount,
            carrier_code
        )

        today_total = user_total(guild_id, message.author.id, "today")
        week_total = user_total(guild_id, message.author.id, "week")
        month_total = user_total(guild_id, message.author.id, "month")

        week_goal = get_goal(guild_id, message.author.id, "week")
        month_goal = get_goal(guild_id, message.author.id, "month")

        week_rank = rank_for_user(guild_id, message.author.id, "week")
        month_rank = rank_for_user(guild_id, message.author.id, "month")

        embed = make_embed(f"💰 AP RECORDED — {CARRIERS[carrier_code]}", color=C_GOLD)

        embed.add_field(name="Rep", value=f"<@{message.author.id}>", inline=True)
        embed.add_field(name="AP Submitted", value=f"**{money(amount)}**", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)

        week_rank_display = f"#{week_rank}" if week_rank else "—"
        month_rank_display = f"#{month_rank}" if month_rank else "—"

        embed.add_field(name="📅 Today", value=money(today_total), inline=True)
        embed.add_field(
            name=f"📈 Week  ·  Rank {week_rank_display}",
            value=f"{money(week_total)}\n{progress_text(week_total, week_goal)}",
            inline=True
        )
        embed.add_field(
            name=f"👑 Month  ·  Rank {month_rank_display}",
            value=f"{money(month_total)}\n{progress_text(month_total, month_goal)}",
            inline=True
        )
        embed.add_field(name="Status", value=status_progress_text(month_total), inline=False)

        if was_fuzzy:
            embed.add_field(
                name="⚠️ Auto-Parsed",
                value=(
                    f"I read this from your message and logged it automatically.\n"
                    f"Amount: **{money(amount)}**  ·  Carrier: **{CARRIERS[carrier_code]}**\n"
                    f"If that's wrong, use `deleteap {entry_id}` to remove it."
                ),
                inline=False
            )

        scoreboard_ch_id = get_setting(guild_id, "scoreboard_channel_id")
        output_ch_id = get_setting(guild_id, "output_channel_id")
        typed_in_wrong_channel = (
            output_ch_id and scoreboard_ch_id
            and str(message.channel.id) != str(scoreboard_ch_id)
            and str(message.channel.id) != str(output_ch_id)
        )
        if typed_in_wrong_channel:
            embed.add_field(
                name="📍 Heads up",
                value=f"Log AP in <#{scoreboard_ch_id}> so the live board stays current.",
                inline=False
            )

        embed.set_footer(text=f"Entry #{entry_id}")

        await out.send(embed=embed)
        await update_scoreboard(guild_id)

        new_top_10 = leaderboard_snapshot(guild_id, "week", 10)
        await send_position_change_alert(message, old_top_10, new_top_10, out)
        await send_level_up_alert(message, old_month_total, month_total, out)
        await send_whale_alert(message, amount, carrier_code, week_total, out)
        return



    goal_match = re.match(r"^goal\s+(week|month)\s+\$?([\d,]+(?:\.\d{1,2})?)$", content)

    # Fuzzy goal catch: handle "goal weekly", "goal wk", "goal monthly", wrong format
    if not goal_match and content.startswith("goal"):
        # Try to extract a period and amount from whatever they typed
        fuzzy_period = None
        if re.search(r"\bweek(ly|s)?\b|\bwk\b", content):
            fuzzy_period = "week"
        elif re.search(r"\bmonth(ly|s)?\b|\bmo\b", content):
            fuzzy_period = "month"

        amount_match = re.search(r"\$?([\d,]+(?:\.\d{1,2})?)", content)
        fuzzy_amount = float(amount_match.group(1).replace(",", "")) if amount_match else None

        if fuzzy_period and fuzzy_amount:
            set_goal(guild_id, message.author.id, fuzzy_period, fuzzy_amount)
            embed = make_embed("🎯 GOAL SET", color=C_GREEN)
            embed.description = (
                f"<@{message.author.id}> set a {fuzzy_period} goal of **{money(fuzzy_amount)}**.\n"
                f"Now go hit it."
            )
            await out.send(embed=embed)
            await update_scoreboard(guild_id)
        else:
            err = make_embed("❌ Check Your Goal Format", color=C_RED)
            err.description = (
                "**Examples:**\n"
                "`goal week 10000`\n"
                "`goal month 40000`\n\n"
                "Use `week` or `month`, followed by your target amount."
            )
            await out.send(embed=err)
        return

    if goal_match:
        period = goal_match.group(1)
        amount = float(goal_match.group(2).replace(",", ""))

        set_goal(guild_id, message.author.id, period, amount)

        embed = make_embed("🎯 GOAL SET", color=C_GREEN)
        embed.description = (
            f"<@{message.author.id}> set a {period} goal of **{money(amount)}**.\n"
            f"Now go hit it."
        )

        await out.send(embed=embed)
        await update_scoreboard(guild_id)
        return


    # Admin: manually set a rep's weekly or monthly AP.
    set_period_match = re.match(r"^set(week|month)(?:\s+<@!?(\d+)>)?\s+\$?([\d,]+(?:\.\d{1,2})?)$", content)
    if set_period_match:
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        period = set_period_match.group(1)
        mentioned_user_id = set_period_match.group(2)
        amount = float(set_period_match.group(3).replace(",", ""))

        if mentioned_user_id:
            target_id = mentioned_user_id
            member = message.guild.get_member(int(target_id))
            username = member.display_name if member else f"User {target_id}"
        else:
            target_id = str(message.author.id)
            username = message.author.display_name

        old_total = user_total(guild_id, target_id, period)
        set_override(guild_id, target_id, username, period, amount)
        audit(guild_id, message.author, f"SET{period.upper()}", f"{username}: {money(old_total)} -> {money(amount)}")

        await update_scoreboard(guild_id)

        embed = make_embed(f"✅ {period.title()} AP Set")
        embed.description = (
            f"Rep: **{username}**\n"
            f"Old {period.title()} AP: **{money(old_total)}**\n"
            f"New {period.title()} AP: **{money(amount)}**"
        )
        await out.send(embed=embed)
        return

    # Admin: adjust a rep's weekly or monthly AP up or down.
    adjust_period_match = re.match(r"^adjust(week|month)(?:\s+<@!?(\d+)>)?\s+([+-]?\$?[\d,]+(?:\.\d{1,2})?)$", content)
    if adjust_period_match:
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        period = adjust_period_match.group(1)
        mentioned_user_id = adjust_period_match.group(2)
        raw_amount = adjust_period_match.group(3).replace("$", "").replace(",", "")
        adjustment = float(raw_amount)

        if mentioned_user_id:
            target_id = mentioned_user_id
            member = message.guild.get_member(int(target_id))
            username = member.display_name if member else f"User {target_id}"
        else:
            target_id = str(message.author.id)
            username = message.author.display_name

        old_total = user_total(guild_id, target_id, period)
        new_total = old_total + adjustment
        if new_total < 0:
            new_total = 0

        set_override(guild_id, target_id, username, period, new_total)
        audit(guild_id, message.author, f"ADJUST{period.upper()}", f"{username}: {money(old_total)} -> {money(new_total)} ({adjustment:+,.2f})")

        await update_scoreboard(guild_id)

        embed = make_embed(f"✅ {period.title()} AP Adjusted")
        embed.description = (
            f"Rep: **{username}**\n"
            f"Adjustment: **{adjustment:+,.2f}**\n"
            f"Old {period.title()} AP: **{money(old_total)}**\n"
            f"New {period.title()} AP: **{money(new_total)}**"
        )
        await out.send(embed=embed)
        return

    # Admin: remove manual weekly or monthly AP override.
    clear_period_match = re.match(r"^clear(week|month)(?:\s+<@!?(\d+)>)?$", content)
    if clear_period_match:
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        period = clear_period_match.group(1)
        mentioned_user_id = clear_period_match.group(2)

        if mentioned_user_id:
            target_id = mentioned_user_id
            member = message.guild.get_member(int(target_id))
            username = member.display_name if member else f"User {target_id}"
        else:
            target_id = str(message.author.id)
            username = message.author.display_name

        old_total = user_total(guild_id, target_id, period)
        delete_override(guild_id, target_id, period)
        new_total = user_total(guild_id, target_id, period)
        audit(guild_id, message.author, f"CLEAR{period.upper()}", f"{username}: cleared override, {money(old_total)} -> {money(new_total)}")

        await update_scoreboard(guild_id)

        embed = make_embed(f"✅ {period.title()} Override Cleared")
        embed.description = (
            f"Rep: **{username}**\n"
            f"Old {period.title()} AP: **{money(old_total)}**\n"
            f"Now Calculated From Entries: **{money(new_total)}**"
        )
        await out.send(embed=embed)
        return

    # Admin: add AP for someone else.
    addap_match = re.match(r"^addap\s+<@!?(\d+)>\s+\$?([\d,]+(?:\.\d{1,2})?)\s+([a-z]+)$", content)
    if addap_match:
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        user_id = addap_match.group(1)
        amount = float(addap_match.group(2).replace(",", ""))
        carrier_code = addap_match.group(3)

        if carrier_code not in CARRIERS:
            err = make_embed("❌ Unknown Carrier", color=C_RED)
            err.description = "Valid codes: `amam` `sbli` `ahl` `americo` `moo` `nlg` `trans` `uhl` `lga`"
            await out.send(embed=err)
            return

        member = message.guild.get_member(int(user_id))
        username = member.display_name if member else f"User {user_id}"

        entry_id = add_ap(guild_id, user_id, username, amount, carrier_code)
        audit(guild_id, message.author, "ADDAP", f"Added {money(amount)} {carrier_code} for {username}, entry #{entry_id}")

        await update_scoreboard(guild_id)

        embed = make_embed("✅ AP ADDED BY ADMIN", color=C_GOLD)
        embed.description = f"Entry **#{entry_id}** logged for **{username}**: {money(amount)} via {CARRIERS[carrier_code]}"
        await out.send(embed=embed)
        return

    # Admin: edit AP amount.
    editap_match = re.match(r"^editap\s+(\d+)\s+\$?([\d,]+(?:\.\d{1,2})?)$", content)
    if editap_match:
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        entry_id = int(editap_match.group(1))
        new_amount = float(editap_match.group(2).replace(",", ""))
        entry = get_entry(guild_id, entry_id)

        if not entry:
            err = make_embed("❌ Entry Not Found", color=C_RED)
            err.description = f"No entry with ID **#{entry_id}** exists. Use `entries` to see recent IDs."
            await out.send(embed=err)
            return

        old_amount = entry["amount"]
        edit_entry_amount(entry_id, new_amount)
        audit(guild_id, message.author, "EDITAP", f"Entry #{entry_id}: {money(old_amount)} -> {money(new_amount)}")

        await update_scoreboard(guild_id)

        embed = make_embed("✏️ ENTRY UPDATED", color=C_SILVER)
        embed.description = (
            f"Entry **#{entry_id}** updated.\n\n"
            f"Rep: **{entry['username']}**\n"
            f"Old AP: **{money(old_amount)}**\n"
            f"New AP: **{money(new_amount)}**"
        )
        await out.send(embed=embed)
        return

    # Admin: edit carrier.
    editcarrier_match = re.match(r"^editcarrier\s+(\d+)\s+([a-z]+)$", content)
    if editcarrier_match:
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        entry_id = int(editcarrier_match.group(1))
        new_carrier = editcarrier_match.group(2)
        entry = get_entry(guild_id, entry_id)

        if not entry:
            err = make_embed("❌ Entry Not Found", color=C_RED)
            err.description = f"No entry with ID **#{entry_id}** exists. Use `entries` to see recent IDs."
            await out.send(embed=err)
            return

        if new_carrier not in CARRIERS:
            err = make_embed("❌ Unknown Carrier", color=C_RED)
            err.description = "Valid codes: `amam` `sbli` `ahl` `americo` `moo` `nlg` `trans` `uhl` `lga`"
            await out.send(embed=err)
            return

        old_carrier = entry["carrier"]
        edit_entry_carrier(entry_id, new_carrier)
        audit(guild_id, message.author, "EDITCARRIER", f"Entry #{entry_id}: {old_carrier} -> {new_carrier}")

        await update_scoreboard(guild_id)

        embed = make_embed("✏️ CARRIER UPDATED", color=C_SILVER)
        embed.description = (
            f"Entry **#{entry_id}** updated.\n\n"
            f"Rep: **{entry['username']}**\n"
            f"Old Carrier: **{CARRIERS.get(old_carrier, old_carrier)}**\n"
            f"New Carrier: **{CARRIERS[new_carrier]}**"
        )
        await out.send(embed=embed)
        return

    # Admin: delete entry.
    deleteap_match = re.match(r"^deleteap\s+(\d+)$", content)
    if deleteap_match:
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        entry_id = int(deleteap_match.group(1))
        entry = get_entry(guild_id, entry_id)

        if not entry:
            err = make_embed("❌ Entry Not Found", color=C_RED)
            err.description = f"No entry with ID **#{entry_id}** exists. Use `entries` to see recent IDs."
            await out.send(embed=err)
            return

        delete_entry(entry_id)
        audit(guild_id, message.author, "DELETEAP", f"Deleted entry #{entry_id}: {entry['username']} {money(entry['amount'])} {entry['carrier']}")

        await update_scoreboard(guild_id)

        embed = make_embed("🗑️ ENTRY DELETED", color=C_RED)
        embed.description = (
            f"Deleted Entry **#{entry_id}**.\n\n"
            f"Rep: **{entry['username']}**\n"
            f"AP: **{money(entry['amount'])}**\n"
            f"Carrier: **{CARRIERS.get(entry['carrier'], entry['carrier'])}**"
        )
        await out.send(embed=embed)
        return

    # Admin: recent entries.
    if content in {"entries", "recent"}:
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        rows = recent_entries(guild_id, 15)
        embed = make_embed("📋 Recent AP Entries")

        if not rows:
            embed.description = "No entries yet."
        else:
            embed.description = "\n".join(format_entry(row) for row in rows)

        await out.send(embed=embed)
        return

    # Admin: user history by mention.
    history_match = re.match(r"^history\s+<@!?(\d+)>$", content)
    if history_match:
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        user_id = history_match.group(1)
        rows = user_history(guild_id, user_id, 15)
        member = message.guild.get_member(int(user_id))
        username = member.display_name if member else f"User {user_id}"

        embed = make_embed(f"📋 AP History: {username}")

        if not rows:
            embed.description = "No entries found."
        else:
            embed.description = "\n".join(format_entry(row) for row in rows)

        await out.send(embed=embed)
        return

    # Admin: audit log.
    if content == "audit":
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        cur.execute("""
        SELECT * FROM audit_log
        WHERE guild_id=?
        ORDER BY id DESC
        LIMIT 10
        """, (guild_id,))
        rows = cur.fetchall()

        embed = make_embed("🧾 Admin Audit Log")

        if not rows:
            embed.description = "No admin actions yet."
        else:
            text = ""
            for row in rows:
                created = row["created_at"].split("T")[0]
                text += f"**#{row['id']}** {created} | **{row['admin_name']}** | {row['action']}\n{row['details']}\n\n"
            embed.description = text[:3900]

        await out.send(embed=embed)
        return

    if content == "adminhelp":
        embed = make_embed("CloserBot — Admin Commands", color=C_SILVER)
        embed.add_field(
            name="📋 View Data",
            value="`entries` / `recent` — last 15 entries\n`history @rep` — rep's AP history\n`audit` — admin action log",
            inline=False
        )
        embed.add_field(
            name="✏️ Edit Entries",
            value="`addap @rep 1800 americo`\n`editap 42 1500`\n`editcarrier 42 moo`\n`deleteap 42`",
            inline=True
        )
        embed.add_field(
            name="⚙️ Override Totals",
            value="`setweek @rep 25000`\n`setmonth @rep 64000`\n`adjustweek @rep +1800`\n`adjustmonth @rep -500`\n`clearweek @rep`\n`clearmonth @rep`",
            inline=True
        )
        embed.add_field(
            name="🖥️ Setup",
            value="`setupscoreboard` — pin live board in this channel\n`setupoutput` — route all bot responses to this channel\n`setupannouncements` — schedule daily/weekly/EOM posts here\n`refresh` — force scoreboard refresh\n`resetalldata` — wipe all entries and stats (testing only)",
            inline=False
        )
        embed.set_footer(text="Admin roles: Agency Owner, Partner, Managing Partner, Senior Partner, Executive Partner, Regional Manager, District Manager, Sales Manager, Admin")
        await out.send(embed=embed)
        return

    if content == "alltime":
        rows = get_alltime_totals(guild_id)
        embed = make_embed("🏆 ALL-TIME LEADERBOARD", color=C_PURPLE)

        if not rows:
            embed.description = "No historical data saved yet. Snapshots are taken at the end of each week and month."
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for i, row in enumerate(rows, start=1):
                icon = medals[i - 1] if i <= 3 else f"`{i}.`"
                periods = f"{row['periods']} period{'s' if row['periods'] != 1 else ''}"
                lines.append(f"{icon} **{row['username']}** — {money(row['career_total'])}  ·  {periods}")
            embed.description = "\n".join(lines)

        await out.send(embed=embed)
        return

    # pastweek [label] — e.g. "pastweek 2025-W22" or just "pastweek" for most recent
    pastweek_match = re.match(r"^pastweek(?:\s+(\S+))?$", content)
    if pastweek_match:
        label = pastweek_match.group(1)

        if not label:
            saved = list_saved_periods(guild_id, "week")
            if not saved:
                err = make_embed("📭 No Weekly History Yet", color=C_NAVY)
                err.description = "Snapshots are saved every Friday at 8:05 PM Central."
                await out.send(embed=err)
                return
            label = saved[0]  # most recent

        rows = get_snapshot(guild_id, "week", label)
        embed = make_embed(f"📈 WEEK — {label}", color=C_GOLD)

        if not rows:
            available = list_saved_periods(guild_id, "week")
            embed.description = f"No data found for `{label}`."
            if available:
                embed.add_field(
                    name="Available weeks",
                    value=" · ".join(f"`{p}`" for p in available[:10]),
                    inline=False
                )
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for row in rows:
                i = row["rank"]
                icon = medals[i - 1] if i <= 3 else f"`{i}.`"
                lines.append(f"{icon} **{row['username']}** — {money(row['total'])}")
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Type pastweek to see available weeks")

        await out.send(embed=embed)
        return

    # pastmonth [label] — e.g. "pastmonth 2025-06" or just "pastmonth" for most recent
    pastmonth_match = re.match(r"^pastmonth(?:\s+(\S+))?$", content)
    if pastmonth_match:
        label = pastmonth_match.group(1)

        if not label:
            saved = list_saved_periods(guild_id, "month")
            if not saved:
                err = make_embed("📭 No Monthly History Yet", color=C_NAVY)
                err.description = "Snapshots are saved on the last day of each month at 8:10 PM Central."
                await out.send(embed=err)
                return
            label = saved[0]  # most recent

        rows = get_snapshot(guild_id, "month", label)
        embed = make_embed(f"👑 MONTH — {label}", color=C_PURPLE)

        if not rows:
            available = list_saved_periods(guild_id, "month")
            embed.description = f"No data found for `{label}`."
            if available:
                embed.add_field(
                    name="Available months",
                    value=" · ".join(f"`{p}`" for p in available[:12]),
                    inline=False
                )
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for row in rows:
                i = row["rank"]
                icon = medals[i - 1] if i <= 3 else f"`{i}.`"
                _, status = current_status(row["total"])
                lines.append(f"{icon} **{row['username']}** — {money(row['total'])}  ·  {status}")
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Type pastmonth to see available months")

        await out.send(embed=embed)
        return

    if content == "stats":
        today_total = user_total(guild_id, message.author.id, "today")
        week_total = user_total(guild_id, message.author.id, "week")
        month_total = user_total(guild_id, message.author.id, "month")

        week_goal = get_goal(guild_id, message.author.id, "week")
        month_goal = get_goal(guild_id, message.author.id, "month")

        week_rank = rank_for_user(guild_id, message.author.id, "week")
        month_rank = rank_for_user(guild_id, message.author.id, "month")

        week_rank_display = f"#{week_rank}" if week_rank else "—"
        month_rank_display = f"#{month_rank}" if month_rank else "—"

        embed = make_embed(f"📊 {message.author.display_name}", color=C_NAVY)
        embed.add_field(name="📅 Today", value=money(today_total), inline=True)
        embed.add_field(name=f"📈 Week  ·  {week_rank_display}",
            value=f"{money(week_total)}\n{progress_text(week_total, week_goal)}", inline=True)
        embed.add_field(name=f"👑 Month  ·  {month_rank_display}",
            value=f"{money(month_total)}\n{progress_text(month_total, month_goal)}", inline=True)
        embed.add_field(name="Status", value=status_progress_text(month_total), inline=False)

        hints = []
        if not week_goal:
            hints.append("Set a weekly goal: `goal week 10000`")
        if not month_goal:
            hints.append("Set a monthly goal: `goal month 40000`")
        if hints:
            embed.add_field(name="💡 Tip", value="\n".join(hints), inline=False)

        embed.set_footer(text="CloserBot")

        await out.send(embed=embed)
        return

    if content == "levels":
        text = (
            "😅 **Noob** — $0\n"
            "🌱 **Rookie** — $7,500\n"
            "🔥 **Closer** — $10,000\n"
            "⚡ **Pro** — $15,000\n"
            "💎 **Expert** — $20,000\n"
            "🚀 **Elite** — $30,000\n"
            "🦍 **Beast Mode** — $40,000\n"
            "🏆 **Legend** — $50,000\n"
            "🤴 **King** — $60,000\n"
            "👑 **God Mode** — $75,000+"
        )
        await out.send(embed=make_embed("🏆 Monthly Status Levels", text, color=C_GOLD))
        return

    if content == "daily":
        await out.send(embed=leaderboard_embed(guild_id, "today"))
        return

    if content == "weekly":
        await out.send(embed=leaderboard_embed(guild_id, "week"))
        return

    if content == "monthly":
        await out.send(embed=leaderboard_embed(guild_id, "month"))
        return

    if content == "refresh":
        if not is_admin(message.author):
            await send_admin_error(out)
            return

        await update_scoreboard(guild_id)
        await out.send("✅ Scoreboard refreshed.")
        return

    if content == "help":
        embed = make_embed("CloserBot — How to Use", color=C_NAVY)
        embed.add_field(
            name="📥 Logging AP",
            value=(
                "Type `ap` followed by your amount and carrier — in any order, "
                "with or without `$`. Extra notes are ignored automatically.\n\n"
                "`ap 1209 americo`\n"
                "`ap $1,236 uhl GI 24 mos`\n"
                "`$876 ap moo 6 months`"
            ),
            inline=False
        )
        embed.add_field(
            name="🎯 Setting Goals",
            value="`goal week 10000`\n`goal month 40000`",
            inline=True
        )
        embed.add_field(
            name="📊 Your Stats",
            value="`stats` — your totals + rank\n`levels` — status tiers",
            inline=True
        )
        embed.add_field(
            name="🏆 Leaderboards",
            value="`daily`  ·  `weekly`  ·  `monthly`",
            inline=False
        )
        embed.add_field(
            name="📚 History",
            value="`alltime` — career totals\n`pastweek` — last week's results\n`pastmonth` — last month's results\n`pastweek 2025-W22` — specific week\n`pastmonth 2025-06` — specific month",
            inline=False
        )
        embed.add_field(
            name="🏢 Carrier Codes",
            value=(
                "`amam` — AMAM\n"
                "`sbli` — SBLI\n"
                "`ahl` — American Home Life\n"
                "`americo` — Americo\n"
                "`moo` — Mutual of Omaha\n"
                "`nlg` — National Life Group\n"
                "`trans` — Transamerica\n"
                "`uhl` — United Home Life\n"
                "`lga` — Legal & General America\n"
                "`lb` — Liberty Bankers"
            ),
            inline=False
        )
        embed.set_footer(text="Managers: type adminhelp for management commands.")
        await out.send(embed=embed)
        return

    # Unknown command catch — only fires if the message looks like a bot command attempt
    known_prefixes = ("ap ", "goal ", "addap ", "editap ", "editcarrier ",
                      "deleteap ", "setweek", "setmonth", "adjustweek",
                      "adjustmonth", "clearweek", "clearmonth", "history ")
    single_commands = {"stats", "levels", "daily", "weekly", "monthly",
                       "help", "adminhelp", "entries", "recent", "audit",
                       "refresh", "setupscoreboard", "setupoutput", "setupannouncements", "resetalldata", "alltime", "pastweek", "pastmonth"}

    is_command_attempt = (
        content in single_commands
        or any(content.startswith(p) for p in known_prefixes)
        # Short single words that look like mistyped commands
        or (len(content.split()) == 1 and len(content) <= 15 and content.isalpha())
    )

    if is_command_attempt and content not in single_commands:
        err = make_embed("❓ Command Not Recognized", color=C_NAVY)
        err.description = (
            "That command didn't match anything.\n\n"
            "Type `help` to see all available commands.\n"
            "Type `adminhelp` if you're a manager."
        )
        await out.send(embed=err)



@client.event
async def on_message(message):
    if message.author.bot:
        return

    if message.guild is None:
        return  # Ignore DMs

    guild_id = str(message.guild.id)
    out = get_output_channel(guild_id, message.channel)

    # Split on newlines so reps can type multiple entries in one message
    lines = message.content.split("\n")
    for line in lines:
        await process_line(message, line, guild_id, out)


@client.event
async def on_member_join(member):
    """Send a welcome DM to every new member with a quick-start guide."""
    gid = str(member.guild.id)
    output_ch_id = get_setting(gid, "output_channel_id")
    scoreboard_ch_id = get_setting(gid, "scoreboard_channel_id")

    embed = make_embed("👋 Welcome to CloserBot", color=C_GOLD)
    embed.description = (
        f"Hey {member.display_name}! Here\'s everything you need to get started.\n\n"
        f"**Log a sale:**\n"
        f"`ap 1209 americo`\n"
        f"`ap $1,800 moo`\n"
        f"Just type `ap`, your amount, and your carrier — in any order.\n\n"
        f"**Set your goals:**\n"
        f"`goal week 10000`\n"
        f"`goal month 40000`\n\n"
        f"**Check your stats:** `stats`\n"
        f"**See the leaderboard:** `weekly` or `monthly`\n"
        f"**Full command list:** `help`"
    )
    if scoreboard_ch_id:
        embed.add_field(
            name="📍 Where to log AP",
            value=f"Type your entries in <#{scoreboard_ch_id}>.",
            inline=False
        )
    embed.add_field(
        name="🏢 Carrier Codes",
        value=(
            "`amam` `sbli` `ahl` `americo` `moo`\n"
            "`nlg` `trans` `uhl` `lga` `lb`"
        ),
        inline=False
    )
    embed.set_footer(text="Type help anytime to see this again.")

    try:
        await member.send(embed=embed)
    except discord.Forbidden:
        # DMs disabled — post to output channel instead
        if output_ch_id:
            ch = client.get_channel(int(output_ch_id))
            if ch:
                embed.description = (
                    f"Hey <@{member.id}>! Here\'s everything you need to get started.\n\n"
                    f"**Log a sale:**\n"
                    f"`ap 1209 americo`\n"
                    f"`ap $1,800 moo`\n"
                    f"Just type `ap`, your amount, and your carrier — in any order.\n\n"
                    f"**Set your goals:**\n"
                    f"`goal week 10000`\n"
                    f"`goal month 40000`\n\n"
                    f"**Check your stats:** `stats`\n"
                    f"**See the leaderboard:** `weekly` or `monthly`\n"
                    f"**Full command list:** `help`"
                )
                await ch.send(embed=embed)

client.run(TOKEN)
