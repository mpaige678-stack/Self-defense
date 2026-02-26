import os
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
import psycopg

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

GUILD_ID = 1426996503880138815

# ✅ PUT YOUR REAL ROLE IDS HERE
ROLE_VERIFIED = 1476479538807439404

ROLE_RECRUIT   = 0  # <- replace with Recruit access role id
ROLE_ELITE     = 1475724667493810186
ROLE_FIGHTER   = 1476504028576743520

TIER_TO_ROLE = {
    "recruit": ROLE_RECRUIT,
    "elite": ROLE_ELITE,
    "fighter": ROLE_FIGHTER,
}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set.")
    return psycopg.connect(DATABASE_URL)

def init_db():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    discord_id BIGINT PRIMARY KEY,
                    tier TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
        conn.commit()

async def sync_roles_once():
    guild = client.get_guild(GUILD_ID)
    if not guild:
        print("❌ Guild not found (bot not ready?)")
        return

    now = datetime.now(timezone.utc)

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT discord_id, tier, expires_at FROM subscriptions;")
            rows = cur.fetchall()

    for discord_id, tier, expires_at in rows:
        member = guild.get_member(int(discord_id))
        if not member:
            continue

        verified_role = guild.get_role(ROLE_VERIFIED)

        # Remove all tier roles first (clean state)
        tier_roles = [guild.get_role(rid) for rid in TIER_TO_ROLE.values() if rid]
        tier_roles = [r for r in tier_roles if r is not None]

        # If expired -> remove tier roles + optionally remove verified (your choice)
        if expires_at <= now:
            try:
                if tier_roles:
                    await member.remove_roles(*tier_roles, reason="Subscription expired")
                # Optional: keep Verified forever OR remove it when expired:
                # await member.remove_roles(verified_role, reason="Subscription expired")
                print(f"⛔ Expired: removed tier roles from {member} ({discord_id})")
            except Exception as e:
                print("role remove error:", e)
            continue

        # Active subscription -> ensure Verified + correct tier role
        target_role_id = TIER_TO_ROLE.get(tier)
        target_role = guild.get_role(target_role_id) if target_role_id else None

        try:
            if verified_role and verified_role not in member.roles:
                await member.add_roles(verified_role, reason="Active subscriber")

            # Remove other tier roles
            if tier_roles:
                await member.remove_roles(*tier_roles, reason="Tier sync (cleanup)")

            # Add correct tier
            if target_role:
                await member.add_roles(target_role, reason="Tier sync (active)")
                print(f"✅ Active: {member} set to {tier} until {expires_at}")
            else:
                print(f"⚠️ Missing role id for tier={tier}. Set ROLE_RECRUIT/ROLE_ELITE/ROLE_FIGHTER correctly.")
        except Exception as e:
            print("role sync error:", e)

async def subscription_loop():
    await client.wait_until_ready()
    print("🔁 Subscription expiration loop started")
    while not client.is_closed():
        try:
            await sync_roles_once()
        except Exception as e:
            print("loop error:", e)
        await asyncio.sleep(60)  # check every 60 seconds

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")
    if DATABASE_URL:
        init_db()
        print("✅ Database ready")

    # start expiration loop
    client.loop.create_task(subscription_loop())

    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} commands")
    except Exception as e:
        print("Sync error:", e)

# Optional admin command to force a role sync
@tree.command(name="sync_subs", description="Force sync subscription roles now")
async def sync_subs(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    await sync_roles_once()
    await interaction.response.send_message("✅ Synced subscriptions.", ephemeral=True)

client.run(DISCORD_TOKEN)