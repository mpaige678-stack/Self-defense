import os
import discord
from discord import app_commands
import psycopg
import stripe

# =========================
# ENV
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")

stripe.api_key = STRIPE_SECRET_KEY

# =========================
# STRIPE PRICE IDS
# =========================
PRICE_RECRUIT = "price_1T50gsB9kGqOyQaKqsChMsDT"
PRICE_ELITE   = "price_1T50fgB9kGqOyQaKgkZfH2XZ"
PRICE_FIGHTER = "price_1T50dWB9kGqOyQaKddLCSgbC"

# =========================
# INTENTS
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =========================
# VIDEO SYSTEM
# =========================
UPLOAD_CHANNEL = "coach-uploads"

CHANNEL_MAP = {
    "#weekly": "weekly-video",
    "#premium": "premium-training",
    "#elite": "elite-training"
}

ARCHIVE_CHANNEL = "video-archive"


async def handle_video_distribution(message):
    if not message.attachments:
        return

    content = message.content.lower()
    targets = []

    if "#all" in content:
        targets = list(CHANNEL_MAP.values())
    else:
        for tag, channel in CHANNEL_MAP.items():
            if tag in content:
                targets.append(channel)

    if not targets:
        return

    guild = message.guild
    archive = discord.utils.get(guild.text_channels, name=ARCHIVE_CHANNEL)

    for channel_name in targets:
        channel = discord.utils.get(guild.text_channels, name=channel_name)
        if not channel:
            continue

        if channel_name == "weekly-video":
            pins = await channel.pins()
            for p in pins:
                await p.unpin()
                if archive:
                    await archive.send(f"Archived weekly video:\n{p.content}")

        sent = await channel.send(
            content=message.content,
            file=await message.attachments[0].to_file()
        )

        if channel_name == "weekly-video":
            await sent.pin()

        await channel.send("📢 New training video dropped!")

# =========================
# DATABASE
# =========================
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg.connect(DATABASE_URL)


def init_db():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    discord_id BIGINT PRIMARY KEY,
                    tier TEXT NOT NULL DEFAULT 'free',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS video_submissions (
                    id BIGSERIAL PRIMARY KEY,
                    discord_id BIGINT NOT NULL,
                    message_url TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
        conn.commit()

# =========================
# EVENTS
# =========================
@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")

    if DATABASE_URL:
        try:
            init_db()
            print("✅ Database ready")
        except Exception as e:
            print("DB error:", e)

    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} commands")
    except Exception as e:
        print("Sync error:", e)


@client.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.name == UPLOAD_CHANNEL:
        await handle_video_distribution(message)

# =========================
# COMMANDS
# =========================
@tree.command(name="ping", description="Test bot")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong 🥋", ephemeral=True)


@tree.command(name="set_tier", description="Set a user's tier")
async def set_tier(interaction: discord.Interaction, user: discord.Member, tier: str):

    if tier not in ["free","premium","elite"]:
        await interaction.response.send_message("Invalid tier.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (discord_id, tier)
                VALUES (%s,%s)
                ON CONFLICT (discord_id)
                DO UPDATE SET tier=EXCLUDED.tier;
            """,(user.id,tier))
        conn.commit()

    await interaction.response.send_message(f"{user.mention} set to {tier}", ephemeral=True)

# =========================
# STRIPE BUY COMMAND
# =========================
@tree.command(name="buy", description="Buy training access")
@app_commands.describe(plan="recruit / elite / fighter")
async def buy(interaction: discord.Interaction, plan: str):

    plan = plan.lower()

    if plan not in ["recruit","elite","fighter"]:
        await interaction.response.send_message("Choose recruit / elite / fighter", ephemeral=True)
        return

    price_map = {
        "recruit": PRICE_RECRUIT,
        "elite": PRICE_ELITE,
        "fighter": PRICE_FIGHTER
    }

    price_id = price_map[plan]

    checkout = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price": price_id,
            "quantity": 1,
        }],
        mode="payment",
        success_url="https://google.com",
        cancel_url="https://google.com",

        # ⭐ IMPORTANT PART
        metadata={
            "discord_id": str(interaction.user.id),
            "plan": plan
        }
    )

    await interaction.response.send_message(
        f"💳 Checkout link for **{plan}**:\n{checkout.url}",
        ephemeral=True
    )

# =========================
# START BOT
# =========================
client.run(DISCORD_TOKEN)