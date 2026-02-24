import discord
from discord.ext import tasks
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo
import json

import db
from config import (
    TZ, TIMEZONE,
    CH_WEEKLY, CH_ARCHIVE, CH_WINS, CH_DAILY,
    WEEKLY_POST_HOUR, WEEKLY_POST_MIN,
    LEADERBOARD_POST_HOUR, LEADERBOARD_POST_MIN,
    ROLE_CONSISTENT, CONSISTENT_REQUIRED, CONSISTENT_WINDOW_DAYS
)
from commands import get_role, ensure_consistent_role

def load_weekly_videos():
    try:
        with open("weekly_videos.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

async def post_weekly_video(guild: discord.Guild, bot_user: discord.ClientUser):
    weekly_ch = discord.utils.get(guild.text_channels, name=CH_WEEKLY)
    if not weekly_ch:
        return

    archive_ch = discord.utils.get(guild.text_channels, name=CH_ARCHIVE)

    videos = load_weekly_videos()
    if not videos:
        await weekly_ch.send("⚠️ weekly_videos.json is empty. Add videos to rotate.")
        return

    # Rotate by ISO week
    week_index = (datetime.now(TZ).date().isocalendar().week - 1) % len(videos)
    vid = videos[week_index]

    # Unpin old pins in weekly channel
    try:
        pins = await weekly_ch.pins()
        for p in pins:
            await p.unpin()
    except Exception:
        pass

    # Archive last bot weekly post (optional)
    if archive_ch:
        try:
            async for msg in weekly_ch.history(limit=10):
                if msg.author == bot_user and "WEEKLY TRAINING VIDEO" in msg.content:
                    await archive_ch.send(f"📦 Archived weekly post:\n{msg.content}")
                    break
        except Exception:
            pass

    content = (
        f"🎥 **WEEKLY TRAINING VIDEO**\n"
        f"**{vid.get('title','Weekly Lesson')}**\n"
        f"{vid.get('url','(missing url)')}\n\n"
        f"Reply **DONE** after you train."
    )
    sent = await weekly_ch.send(content)
    try:
        await sent.pin()
    except Exception:
        pass

async def post_leaderboard(guild: discord.Guild):
    ch = discord.utils.get(guild.text_channels, name=CH_DAILY) or discord.utils.get(guild.text_channels, name=CH_WEEKLY)
    if not ch:
        return

    since = datetime.now(timezone.utc) - timedelta(days=7)
    rows = db.weekly_leaderboard(since, limit=10)

    if not rows:
        await ch.send("🏆 Weekly leaderboard: no DONEs logged yet. Be the first — reply DONE after training.")
        return

    lines = ["🏆 **Weekly Leaderboard (last 7 days)**"]
    for i, r in enumerate(rows, start=1):
        uid = int(r["discord_id"])
        n = int(r["n"])
        lines.append(f"{i}. <@{uid}> — **{n}** DONEs")

    await ch.send("\n".join(lines))

async def expiry_sweep(guild: discord.Guild):
    # Optional: if you set expires_at in DB later, this enforces it
    now = datetime.now(timezone.utc)
    rows = db.get_expiring(now)
    if not rows:
        return

    for r in rows:
        uid = int(r["discord_id"])
        tier = r["tier"]
        expires_at = r["expires_at"]
        if not expires_at:
            continue

        try:
            member = guild.get_member(uid)
            if not member:
                continue

            # Reminder windows
            days_left = (expires_at - now).total_seconds() / 86400.0
            # if expired -> force free tier
            if days_left <= 0:
                db.set_tier(uid, "free")
                from commands import apply_tier_roles
                await apply_tier_roles(guild, member, "free")
                try:
                    await member.send("⚠️ Your subscription expired. You’ve been moved to FREE tier.")
                except Exception:
                    pass
            elif 0 < days_left <= 3.1:
                # gentle reminder (once per day is fine)
                try:
                    await member.send(f"⏳ Subscription reminder: {days_left:.0f} day(s) left. Renew to keep access.")
                except Exception:
                    pass
        except Exception:
            continue

def start_tasks(client: discord.Client):

    @tasks.loop(time=time(hour=WEEKLY_POST_HOUR, minute=WEEKLY_POST_MIN, tzinfo=TZ))
    async def weekly_rotation():
        # Only Mondays
        if datetime.now(TZ).weekday() != 0:
            return
        for guild in client.guilds:
            await post_weekly_video(guild, client.user)

    @tasks.loop(time=time(hour=LEADERBOARD_POST_HOUR, minute=LEADERBOARD_POST_MIN, tzinfo=TZ))
    async def weekly_leaderboard_post():
        # Only Sundays
        if datetime.now(TZ).weekday() != 6:
            return
        for guild in client.guilds:
            await post_leaderboard(guild)

    @tasks.loop(hours=24)
    async def daily_maintenance():
        # once per day: enforce expiry + consistent role checks
        for guild in client.guilds:
            await expiry_sweep(guild)

            # ensure consistent role for members who have been active
            # (lightweight: only check recent talkers would be ideal, but this is fine for ~100 members)
            consistent_role = get_role(guild, ROLE_CONSISTENT)
            if not consistent_role:
                continue
            for m in guild.members:
                if m.bot:
                    continue
                try:
                    await ensure_consistent_role(guild, m)
                except Exception:
                    pass

    if not weekly_rotation.is_running():
        weekly_rotation.start()
    if not weekly_leaderboard_post.is_running():
        weekly_leaderboard_post.start()
    if not daily_maintenance.is_running():
        daily_maintenance.start()
