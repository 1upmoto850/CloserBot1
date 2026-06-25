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

cur.execute("""
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id TEXT NOT NULL,
    admin_name TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
)
""")

conn.commit()


def money(amount):
    return f"${amount:,.2f}"


def now_iso():
    return datetime.now().isoformat()


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


def is_admin(member):
    if member.guild_permissions.administrator:
        return True

    role_names = {role.name.lower() for role in member.roles}

    return bool(role_names.intersection(ADMIN_ROLE_NAMES))


def require_admin_message():
    return "❌ Admin only. You need a manager or admin role to use this command."


def audit(admin, action, details):
    cur.execute("""
    INSERT INTO audit_log (admin_id, admin_name, action, details, created_at)
    VALUES (?, ?, ?, ?, ?)
    """, (str(admin.id), admin.display_name, action, details, now_iso()))
    conn.commit()


def add_ap(user_id, username, amount, carrier):
    cur.execute("""
    INSERT INTO ap_entries (user_id, username, amount, carrier, created_at)
    VALUES (?, ?, ?, ?, ?)
    """, (str(user_id), username, amount, carrier, now_iso()))
    conn.commit()
    return cur.lastrowid


def get_entry(entry_id):
    cur.execute("SELECT * FROM ap_entries WHERE id = ?", (entry_id,))
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


def user_history(user_id, limit=10):
    cur.execute("""
    SELECT * FROM ap_entries
    WHERE user_id = ?
    ORDER BY id DESC
    LIMIT ?
    """, (str(user_id), limit))
    return cur.fetchall()


def recent_entries(limit=10):
    cur.execute("""
    SELECT * FROM ap_entries
    ORDER BY id DESC
    LIMIT ?
    """, (limit,))
    return cur.fetchall()


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


def make_embed(title, description=None, color=0x2ECC71):
    return discord.Embed(
        title=title,
        description=description,
        color=color,
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


def format_entry(entry):
    created = entry["created_at"].split("T")[0]
    return (
        f"**#{entry['id']}** | **{entry['username']}** | "
        f"{money(entry['amount'])} | {CARRIERS.get(entry['carrier'], entry['carrier'])} | {created}"
    )


@client.event
async def on_ready():
    print("====================================")
    print("        CLOSERBOT v1.2 ADMIN")
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
        if not is_admin(message.author):
            await message.channel.send(require_admin_message())
            return

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

    # Admin: add AP for someone else.
    addap_match = re.match(r"^addap\s+<@!?(\d+)>\s+\$?([\d,]+(?:\.\d{1,2})?)\s+([a-z]+)$", content)
    if addap_match:
        if not is_admin(message.author):
            await message.channel.send(require_admin_message())
            return

        user_id = addap_match.group(1)
        amount = float(addap_match.group(2).replace(",", ""))
        carrier_code = addap_match.group(3)

        if carrier_code not in CARRIERS:
            await message.channel.send("❌ Invalid carrier.")
            return

        member = message.guild.get_member(int(user_id))
        username = member.display_name if member else f"User {user_id}"

        entry_id = add_ap(user_id, username, amount, carrier_code)
        audit(message.author, "ADDAP", f"Added {money(amount)} {carrier_code} for {username}, entry #{entry_id}")

        await update_scoreboard()

        embed = make_embed("✅ ADMIN AP ADDED")
        embed.description = f"Entry **#{entry_id}** added for **{username}**: {money(amount)} with {CARRIERS[carrier_code]}"
        await message.channel.send(embed=embed)
        return

    # Admin: edit AP amount.
    editap_match = re.match(r"^editap\s+(\d+)\s+\$?([\d,]+(?:\.\d{1,2})?)$", content)
    if editap_match:
        if not is_admin(message.author):
            await message.channel.send(require_admin_message())
            return

        entry_id = int(editap_match.group(1))
        new_amount = float(editap_match.group(2).replace(",", ""))
        entry = get_entry(entry_id)

        if not entry:
            await message.channel.send(f"❌ Entry #{entry_id} not found.")
            return

        old_amount = entry["amount"]
        edit_entry_amount(entry_id, new_amount)
        audit(message.author, "EDITAP", f"Entry #{entry_id}: {money(old_amount)} -> {money(new_amount)}")

        await update_scoreboard()

        embed = make_embed("✏️ AP EDITED")
        embed.description = (
            f"Entry **#{entry_id}** updated.\n\n"
            f"Rep: **{entry['username']}**\n"
            f"Old AP: **{money(old_amount)}**\n"
            f"New AP: **{money(new_amount)}**"
        )
        await message.channel.send(embed=embed)
        return

    # Admin: edit carrier.
    editcarrier_match = re.match(r"^editcarrier\s+(\d+)\s+([a-z]+)$", content)
    if editcarrier_match:
        if not is_admin(message.author):
            await message.channel.send(require_admin_message())
            return

        entry_id = int(editcarrier_match.group(1))
        new_carrier = editcarrier_match.group(2)
        entry = get_entry(entry_id)

        if not entry:
            await message.channel.send(f"❌ Entry #{entry_id} not found.")
            return

        if new_carrier not in CARRIERS:
            await message.channel.send("❌ Invalid carrier.")
            return

        old_carrier = entry["carrier"]
        edit_entry_carrier(entry_id, new_carrier)
        audit(message.author, "EDITCARRIER", f"Entry #{entry_id}: {old_carrier} -> {new_carrier}")

        await update_scoreboard()

        embed = make_embed("✏️ CARRIER EDITED")
        embed.description = (
            f"Entry **#{entry_id}** updated.\n\n"
            f"Rep: **{entry['username']}**\n"
            f"Old Carrier: **{CARRIERS.get(old_carrier, old_carrier)}**\n"
            f"New Carrier: **{CARRIERS[new_carrier]}**"
        )
        await message.channel.send(embed=embed)
        return

    # Admin: delete entry.
    deleteap_match = re.match(r"^deleteap\s+(\d+)$", content)
    if deleteap_match:
        if not is_admin(message.author):
            await message.channel.send(require_admin_message())
            return

        entry_id = int(deleteap_match.group(1))
        entry = get_entry(entry_id)

        if not entry:
            await message.channel.send(f"❌ Entry #{entry_id} not found.")
            return

        delete_entry(entry_id)
        audit(message.author, "DELETEAP", f"Deleted entry #{entry_id}: {entry['username']} {money(entry['amount'])} {entry['carrier']}")

        await update_scoreboard()

        embed = make_embed("🗑 AP DELETED", color=0xE74C3C)
        embed.description = (
            f"Deleted Entry **#{entry_id}**.\n\n"
            f"Rep: **{entry['username']}**\n"
            f"AP: **{money(entry['amount'])}**\n"
            f"Carrier: **{CARRIERS.get(entry['carrier'], entry['carrier'])}**"
        )
        await message.channel.send(embed=embed)
        return

    # Admin: recent entries.
    if content in {"entries", "recent"}:
        if not is_admin(message.author):
            await message.channel.send(require_admin_message())
            return

        rows = recent_entries(15)
        embed = make_embed("📋 Recent AP Entries")

        if not rows:
            embed.description = "No entries yet."
        else:
            embed.description = "\n".join(format_entry(row) for row in rows)

        await message.channel.send(embed=embed)
        return

    # Admin: user history by mention.
    history_match = re.match(r"^history\s+<@!?(\d+)>$", content)
    if history_match:
        if not is_admin(message.author):
            await message.channel.send(require_admin_message())
            return

        user_id = history_match.group(1)
        rows = user_history(user_id, 15)
        member = message.guild.get_member(int(user_id))
        username = member.display_name if member else f"User {user_id}"

        embed = make_embed(f"📋 AP History: {username}")

        if not rows:
            embed.description = "No entries found."
        else:
            embed.description = "\n".join(format_entry(row) for row in rows)

        await message.channel.send(embed=embed)
        return

    # Admin: audit log.
    if content == "audit":
        if not is_admin(message.author):
            await message.channel.send(require_admin_message())
            return

        cur.execute("""
        SELECT * FROM audit_log
        ORDER BY id DESC
        LIMIT 10
        """)
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

        await message.channel.send(embed=embed)
        return

    if content == "adminhelp":
        await message.channel.send(
            "**CloserBot Admin Commands**\n\n"
            "`entries` or `recent` - show last 15 AP entries\n"
            "`history @rep` - show a rep's recent AP entries\n"
            "`addap @rep 1800 americo` - add AP for a rep\n"
            "`editap 42 1500` - edit entry #42 amount\n"
            "`editcarrier 42 moo` - edit entry #42 carrier\n"
            "`deleteap 42` - delete entry #42\n"
            "`audit` - show admin changes\n"
            "`refresh` - refresh live scoreboard\n\n"
            "Admin access is based on Discord roles: Sales Manager, District Manager, Regional Manager, Agency Owner, Partner, Managing Partner, Senior Partner, Executive Partner, Admin."
        )
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
        if not is_admin(message.author):
            await message.channel.send(require_admin_message())
            return

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
            "`help`\n\n"
            "**Admin:** `adminhelp`\n\n"
            "Carriers: `amam, sbli, ahl, americo, moo, nlg, trans, uhl, lga`"
        )


client.run(TOKEN)
