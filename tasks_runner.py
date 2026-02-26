import os
import asyncio
from datetime import datetime, timezone

import discord
import psycopg

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

GUILD_ID = 1426996503880138815

ROLE_MAP = {
    "recruit": 1426996503880138815,
    "elite": 1475724667493810186,
    "fighter": 1476504028576743520
}

ROLE_VERIFIED = 1476479538807439404


intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)


def db_conn():
    return psycopg.connect(DATABASE_URL)


async def sync_roles():
    await client.wait_until_ready()
    guild = client.get_guild(GUILD_ID)

    while not client.is_closed():
        now = datetime.now(timezone.utc)

        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT discord_id, tier, expires_at FROM subscriptions;")
                rows = cur.fetchall()

        for discord_id, tier, expires in rows:
            member = guild.get_member(discord_id)
            if not member:
                continue

            expired = expires < now

            # remove all tier roles first
            roles_to_remove = [
                guild.get_role(rid) for rid in ROLE_MAP.values()
            ]
            await member.remove_roles(*roles_to_remove, reason="Tier refresh")

            if expired:
                print(f"Expired {member}")
                continue

            # assign correct role
            role = guild.get_role(ROLE_MAP[tier])
            await member.add_roles(role, reason="Active subscription")

            # ensure verified badge
            verified = guild.get_role(ROLE_VERIFIED)
            if verified not in member.roles:
                await member.add_roles(verified)

        await asyncio.sleep(60)


@client.event
async def on_ready():
    print("Task runner online")
    client.loop.create_task(sync_roles())


client.run(DISCORD_TOKEN)