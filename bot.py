import os
import re
import sqlite3
import discord
from datetime import datetime, timedelta
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
}

DB_FILE = "closerbot.db"
WHALE_THRESHOLD = 1700

# Monthly status ladder
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
client = discord.Client(intents=intents)

conn = sqlite3.connect(DB_FILE)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS ap_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    amount REAL NOT NULL,
    carrier TEXT NOT NULL,
    created_at TEXT NOT NULL
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS goals (
    user_id TEXT NOT NULL,
    period TEXT NOT NULL,
    amount REAL NOT NULL,
    PRIMARY KEY (user_id, period)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""")

conn.commit()


def money(amount):
    return f"${amount:,.2f}"


def get_start(period):
    now = datetime.now()

    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "week":
        start = now - timedelta(days=now.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    return now


def add_ap(user_id, username, amount, carrier):
    cur.execute("""
    INSERT INTO ap_entries (user_id, username, amount, carrier, created_at)
    VALUES (?, ?, ?, ?, ?)
    """, (str(user_id), username, amount, carrier, datetime.now().isoformat()))
    conn.commit()
    return cur.lastrowid


def set_goal(user_id, period, amount):
    cur.execute("""
    INSERT OR REPLACE INTO goals (user_id, period, amount)
    VALUES (?, ?, ?)
    """, (str(user_id), period, amount))
    conn.commit()


def get_goal(user_id, period):
    cur.execute("""
    SELECT amount FROM goals
    WHERE user_id = ? AND period = ?
    """, (str(user_id), period))
    row = cur.fetchone()
    return row["amount"] if row else 0


def user_total(user_id, period):
    start = get_start(period)
    cur.execute("""
    SELECT COALESCE(SUM(amount), 0) total
    FROM ap_entries
    WHERE user_id = ? AND created_at >= ?
    """, (str(user_id), start.isoformat()))
    return cur.fetchone()["total"]


def team_total(period):
    start = get_start(period)
    cur.execute("""
    SELECT COALESCE(SUM(amount), 0) total
    FROM ap_entries
    WHERE created_at >= ?
    """, (start.isoformat(),))
    return cur.fetchone()["total"]


def leaderboard(period, limit=10):
    start = get_start(period)
    cur.execute("""
    SELECT user_id, username, SUM(amount) total
    FROM ap_entries
    WHERE created_at >= ?
    GROUP BY user_id
    ORDER BY total DESC
    LIMIT ?
    """, (start.isoformat(), limit))
    return cur.fetchall()


def rank_for_user(user_id, period):
    start = get_start(period)
    cur.execute("""
    SELECT user_id, SUM(amount) total
    FROM ap_entries
    WHERE created_at >= ?
    GROUP BY user_id
    ORDER BY total DESC
    """, (start.isoformat(),))

    rows = cur.fetchall()

    for index, row in enumerate(rows, start=1):
        if row["user_id"] == str(user_id):
            return index

    return None


def leaderboard_snapshot(period="week", limit=10):
    rows = leaderboard(period, limit)
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


def set_setting(key, value):
    cur.execute("""
    INSERT OR REPLACE INTO settings (key, value)
    VALUES (?, ?)
    """, (key, str(value)))
    conn.commit()


def get_setting(key):
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    return row["value"] if row else None


def make_embed(title, description=None):
    return discord.Embed(
        title=title,
        description=description,
        color=0x2ECC71,
        timestamp=datetime.now()
    )


def leaderboard_embed(period):
    rows = leaderboard(period)

    title_map = {
        "today": "🏆 Daily AP Leaderboard",
        "week": "🏆 Weekly AP Leaderboard",
        "month": "👑 Monthly AP Leaderboard",
    }

    embed = make_embed(title_map[period])

    if not rows:
        embed.description = "No AP submitted yet."
        return embed

    medals = ["🥇", "🥈", "🥉"]
    text = ""

    for i, row in enumerate(rows, start=1):
        icon = medals[i - 1] if i <= 3 else f"{i}."
        text += f"{icon} **{row['username']}**: {money(row['total'])}\n"

    embed.description = text
    embed.add_field(name="Team Total", value=money(team_total(period)), inline=False)
    return embed


def scoreboard_embed():
    rows = leaderboard("week", 10)

    embed = make_embed("🏆 CLOSERBOT LIVE SCOREBOARD")

    if not rows:
        embed.description = "No AP submitted this week yet."
    else:
        top_total = rows[0]["total"]
        medals = ["🥇", "🥈", "🥉"]
        text = ""

        for i, row in enumerate(rows, start=1):
            icon = medals[i - 1] if i <= 3 else f"{i}."
            bar = progress_bar(row["total"], top_total)
            month_total = user_total(row["user_id"], "month")
            _, status = current_status(month_total)
            text += (
                f"{icon} **{row['username']}**\n"
                f"`{bar}` {money(row['total'])}\n"
                f"Monthly Status: {status}\n\n"
            )

        embed.description = text

    embed.add_field(name="💰 Agency Today", value=money(team_total("today")), inline=True)
    embed.add_field(name="📈 Agency Week", value=money(team_total("week")), inline=True)
    embed.add_field(name="👑 Agency Month", value=money(team_total("month")), inline=True)
    embed.set_footer(text=f"Last updated: {datetime.now().strftime('%I:%M %p')}")

    return embed


async def update_scoreboard():
    channel_id = get_setting("scoreboard_channel_id")
    message_id = get_setting("scoreboard_message_id")

    if not channel_id or not message_id:
        return

    try:
        channel = client.get_channel(int(channel_id))
        if not channel:
            return

        msg = await channel.fetch_message(int(message_id))
        await msg.edit(embed=scoreboard_embed())

    except Exception as e:
        print("Scoreboard update failed:", e)


async def send_position_change_alert(message, old_snapshot, new_snapshot):
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
        alert = make_embed("👑 NEW #1")
        alert.description = (
            f"**{message.author.display_name}** has taken the weekly lead!\n\n"
            f"🥇 Weekly Rank: **#1**\n"
            f"📈 Weekly AP: **{money(new_data['total'])}**"
        )
        await message.channel.send(embed=alert)
        return

    if moved_into_top_10:
        alert = make_embed("🚀 ENTERED THE TOP 10")
        alert.description = (
            f"**{message.author.display_name}** just entered the weekly Top 10!\n\n"
            f"📈 New Rank: **#{new_rank}**\n"
            f"Weekly AP: **{money(new_data['total'])}**"
        )
        await message.channel.send(embed=alert)
        return

    if moved_up:
        passed_text = ", ".join(passed_names[:3]) if passed_names else "the competition"
        alert = make_embed("📈 POSITION CHANGE")
        alert.description = (
            f"**{message.author.display_name}** just passed **{passed_text}**!\n\n"
            f"⬆️ **#{old_rank} ➜ #{new_rank}**\n"
            f"Weekly AP: **{money(new_data['total'])}**"
        )
        await message.channel.send(embed=alert)


async def send_level_up_alert(message, old_month_total, new_month_total):
    old_threshold, old_status = current_status(old_month_total)
    new_threshold, new_status = current_status(new_month_total)

    if new_threshold <= old_threshold:
        return

    alert = make_embed("🎉 LEVEL UP")
    alert.description = (
        f"**{message.author.display_name}** just reached\n\n"
        f"{new_status}\n\n"
        f"Monthly AP: **{money(new_month_total)}**"
    )

    await message.channel.send(embed=alert)


async def send_whale_alert(message, amount, carrier_code, week_total):
    if amount < WHALE_THRESHOLD:
        return

    whale = make_embed("🐋 WHALE ALERT")
    whale.description = (
        f"**{message.author.display_name}** just submitted a whale.\n\n"
        f"💰 AP: **{money(amount)}**\n"
        f"🏢 Carrier: **{CARRIERS[carrier_code]}**\n"
        f"🔥 Weekly Total: **{money(week_total)}**"
    )

    await message.channel.send(embed=whale)


@client.event
async def on_ready():
    print("====================================")
    print("        CLOSERBOT v1.1")
    print("====================================")
    print(f"✅ Logged in as {client.user}")
    print("Ready to Track Closers 🚀")


@client.event
async def on_message(message):
    if message.author.bot:
        return

    raw = message.content.strip()
    content = raw.lower()

    if content == "setupscoreboard":
        embed = scoreboard_embed()
        msg = await message.channel.send(embed=embed)

        set_setting("scoreboard_channel_id", message.channel.id)
        set_setting("scoreboard_message_id", msg.id)

        await message.channel.send("✅ Live scoreboard is now connected to this channel.")
        return

    ap_match = re.match(r"^ap\s+\$?([\d,]+(?:\.\d{1,2})?)\s+([a-z]+)$", content)

    if ap_match:
        amount = float(ap_match.group(1).replace(",", ""))
        carrier_code = ap_match.group(2)

        if carrier_code not in CARRIERS:
            await message.channel.send(
                "❌ Invalid carrier.\n\n"
                "Use one of these:\n"
                "`amam, sbli, ahl, americo, moo, nlg, trans, uhl, lga`\n\n"
                "Example: `ap 1209 americo`"
            )
            return

        old_top_10 = leaderboard_snapshot("week", 10)
        old_month_total = user_total(message.author.id, "month")

        entry_id = add_ap(
            message.author.id,
            message.author.display_name,
            amount,
            carrier_code
        )

        today_total = user_total(message.author.id, "today")
        week_total = user_total(message.author.id, "week")
        month_total = user_total(message.author.id, "month")

        week_goal = get_goal(message.author.id, "week")
        month_goal = get_goal(message.author.id, "month")

        week_rank = rank_for_user(message.author.id, "week")
        month_rank = rank_for_user(message.author.id, "month")

        _, status = current_status(month_total)

        embed = make_embed("✅ AP RECORDED")
        embed.add_field(name="Rep", value=message.author.display_name, inline=True)
        embed.add_field(name="Carrier", value=CARRIERS[carrier_code], inline=True)
        embed.add_field(name="AP", value=money(amount), inline=True)

        embed.add_field(name="Today", value=money(today_total), inline=False)

        embed.add_field(
            name="This Week",
            value=f"{money(week_total)}\nRank: #{week_rank}\nGoal: {progress_text(week_total, week_goal)}",
            inline=True
        )

        embed.add_field(
            name="This Month",
            value=f"{money(month_total)}\nRank: #{month_rank}\nGoal: {progress_text(month_total, month_goal)}",
            inline=True
        )

        embed.add_field(
            name="Monthly Status",
            value=status_progress_text(month_total),
            inline=False
        )

        embed.set_footer(text=f"Entry ID: {entry_id}")

        await message.channel.send(embed=embed)
        await update_scoreboard()

        new_top_10 = leaderboard_snapshot("week", 10)

        await send_position_change_alert(message, old_top_10, new_top_10)
        await send_level_up_alert(message, old_month_total, month_total)
        await send_whale_alert(message, amount, carrier_code, week_total)

        return

    goal_match = re.match(r"^goal\s+(week|month)\s+\$?([\d,]+(?:\.\d{1,2})?)$", content)

    if goal_match:
        period = goal_match.group(1)
        amount = float(goal_match.group(2).replace(",", ""))

        set_goal(message.author.id, period, amount)

        embed = make_embed("🎯 Goal Set")
        embed.add_field(name="Rep", value=message.author.display_name, inline=True)
        embed.add_field(name="Period", value=period.title(), inline=True)
        embed.add_field(name="Goal", value=money(amount), inline=True)

        await message.channel.send(embed=embed)
        await update_scoreboard()
        return

    if content == "stats":
        today_total = user_total(message.author.id, "today")
        week_total = user_total(message.author.id, "week")
        month_total = user_total(message.author.id, "month")

        week_goal = get_goal(message.author.id, "week")
        month_goal = get_goal(message.author.id, "month")

        week_rank = rank_for_user(message.author.id, "week")
        month_rank = rank_for_user(message.author.id, "month")

        embed = make_embed(f"📊 {message.author.display_name}'s Stats")
        embed.add_field(name="Today", value=money(today_total), inline=False)
        embed.add_field(
            name="This Week",
            value=f"{money(week_total)}\nRank: #{week_rank or 'N/A'}\nGoal: {progress_text(week_total, week_goal)}",
            inline=True
        )
        embed.add_field(
            name="This Month",
            value=f"{money(month_total)}\nRank: #{month_rank or 'N/A'}\nGoal: {progress_text(month_total, month_goal)}",
            inline=True
        )
        embed.add_field(
            name="Monthly Status",
            value=status_progress_text(month_total),
            inline=False
        )

        await message.channel.send(embed=embed)
        return

    if content == "levels":
        text = (
            "😅 **Noob**: $0\n"
            "🌱 **Rookie**: $7,500\n"
            "🔥 **Closer**: $10,000\n"
            "⚡ **Pro**: $15,000\n"
            "💎 **Expert**: $20,000\n"
            "🚀 **Elite**: $30,000\n"
            "🦍 **Beast Mode**: $40,000\n"
            "🏆 **Legend**: $50,000\n"
            "🤴 **King**: $60,000\n"
            "👑 **God Mode**: $75,000+"
        )

        await message.channel.send(embed=make_embed("🏆 Monthly Status Levels", text))
        return

    if content == "daily":
        await message.channel.send(embed=leaderboard_embed("today"))
        return

    if content == "weekly":
        await message.channel.send(embed=leaderboard_embed("week"))
        return

    if content == "monthly":
        await message.channel.send(embed=leaderboard_embed("month"))
        return

    if content == "refresh":
        await update_scoreboard()
        await message.channel.send("✅ Scoreboard refreshed.")
        return

    if content == "help":
        await message.channel.send(
            "**CloserBot Commands**\n\n"
            "`ap 1209 americo`\n"
            "`goal week 10000`\n"
            "`goal month 40000`\n"
            "`stats`\n"
            "`levels`\n"
            "`daily`\n"
            "`weekly`\n"
            "`monthly`\n"
            "`setupscoreboard`\n"
            "`refresh`\n\n"
            "Carriers: `amam, sbli, ahl, americo, moo, nlg, trans, uhl, lga`"
        )


client.run(TOKEN)
