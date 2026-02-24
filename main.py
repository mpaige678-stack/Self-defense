import os
import discord
from discord import app_commands
import psycopg

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # Railway will provide this after you add Postgres

intents = discord.Intents.default()
# If you want prefix commands / reading messages, enable message_content intent:
# intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set. Add Postgres on Railway and set DATABASE_URL.")
    return psycopg.connect(DATABASE_URL)

def init_db():
    # Creates basic tables for tiers + video submissions
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    discord_id BIGINT PRIMARY KEY,
                    tier TEXT NOT NULL DEFAULT 'free',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS video_submissions (
                    id BIGSERIAL PRIMARY KEY,
                    discord_id BIGINT NOT NULL,
                    message_url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
        conn.commit()

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user} (ID: {client.user.id})")
    # Initialize DB if available
    if DATABASE_URL:
        try:
            init_db()
            print("✅ Database ready")
        except Exception as e:
            print(f"⚠️ Database init failed: {e}")

    # Sync slash commands
    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"⚠️ Slash command sync failed: {e}")

@tree.command(name="ping", description="Test if the bot is online")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong 🥋", ephemeral=True)

@tree.command(name="set_tier", description="Set a user's tier (free/premium/elite)")
@app_commands.describe(user="User to update", tier="free, premium, or elite")
async def set_tier(interaction: discord.Interaction, user: discord.Member, tier: str):
    tier = tier.lower().strip()
    if tier not in {"free", "premium", "elite"}:
        await interaction.response.send_message("Tier must be: free, premium, or elite.", ephemeral=True)
        return

    # Simple permission check: only allow server admins
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return

    if not DATABASE_URL:
        await interaction.response.send_message("DATABASE_URL not set yet. Add Postgres in Railway.", ephemeral=True)
        return

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (discord_id, tier)
                VALUES (%s, %s)
                ON CONFLICT (discord_id) DO UPDATE SET tier = EXCLUDED.tier;
            """, (user.id, tier))
        conn.commit()

    await interaction.response.send_message(f"✅ Set {user.mention} to **{tier}**.", ephemeral=True)

@tree.command(name="submit_video", description="Submit a training video for review")
@app_commands.describe(message_url="Paste the Discord message link or video link")
async def submit_video(interaction: discord.Interaction, message_url: str):
    if not DATABASE_URL:
        await interaction.response.send_message("DATABASE_URL not set yet. Add Postgres in Railway.", ephemeral=True)
        return

    with db_conn() as conn:
        with conn.cursor() as cur:
            # Ensure user exists
            cur.execute("""
                INSERT INTO users (discord_id) VALUES (%s)
                ON CONFLICT (discord_id) DO NOTHING;
            """, (interaction.user.id,))
            # Insert submission
            cur.execute("""
                INSERT INTO video_submissions (discord_id, message_url)
                VALUES (%s, %s);
            """, (interaction.user.id, message_url))
        conn.commit()

    await interaction.response.send_message("✅ Video submitted for review. Coach will respond soon.", ephemeral=True)

client.run(DISCORD_TOKEN)
