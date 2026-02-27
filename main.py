import os
import stripe
import discord
from discord import app_commands
from discord.ui import View, Button

# =========================
# ENV
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
CHECKOUT_SUCCESS_URL = os.getenv("CHECKOUT_SUCCESS_URL", "https://example.com/success")
CHECKOUT_CANCEL_URL = os.getenv("CHECKOUT_CANCEL_URL", "https://example.com/cancel")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")
if not STRIPE_SECRET_KEY:
    raise RuntimeError("STRIPE_SECRET_KEY is missing")

stripe.api_key = STRIPE_SECRET_KEY

# =========================
# PRICES (YOU SAID: $7 / $19 / $49)
# Put your REAL Stripe price IDs here.
# These must match your webhook PRICE_MAP keys.
# =========================
PRICE_RECRUIT = "price_1T50gsB9kGqOyQaKqsChMsDT"  # $7
PRICE_ELITE   = "price_1T50fgB9kGqOyQaKgkZfH2XZ"  # $19
PRICE_FIGHTER = "price_1T50dWB9kGqOyQaKddLCSgbC"  # $49

TIER_TO_PRICE = {
    "recruit": PRICE_RECRUIT,
    "elite": PRICE_ELITE,
    "fighter": PRICE_FIGHTER,
}

# =========================
# DISCORD CLIENT
# =========================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =========================
# STRIPE CHECKOUT CREATOR
# =========================
def create_checkout_session(*, discord_user_id: int, price_id: str) -> str:
    """
    Creates a Stripe Checkout Session and returns the hosted URL.
    IMPORTANT: includes metadata discord_id + price_id for your webhook.
    """
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=CHECKOUT_SUCCESS_URL,
        cancel_url=CHECKOUT_CANCEL_URL,
        metadata={
            "discord_id": str(discord_user_id),
            "price_id": price_id
        },
    )
    return session.url

# =========================
# UI BUTTONS (optional but clean)
# =========================
class BuyView(View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

        self.add_item(self.buy_button("Recruit ($7 / 14 days)", "recruit"))
        self.add_item(self.buy_button("Elite ($19 / 30 days)", "elite"))
        self.add_item(self.buy_button("Fighter ($49 / 60 days)", "fighter"))

    def buy_button(self, label: str, tier: str) -> Button:
        price_id = TIER_TO_PRICE[tier]

        async def callback(interaction: discord.Interaction):
            # Only allow the person who opened the menu to use it
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("This menu isn’t for you.", ephemeral=True)
                return

            try:
                url = create_checkout_session(
                    discord_user_id=interaction.user.id,
                    price_id=price_id,
                )
            except Exception as e:
                await interaction.response.send_message(f"Checkout error: {e}", ephemeral=True)
                return

            await interaction.response.send_message(
                f"✅ Click to pay for **{tier.upper()}**:\n{url}",
                ephemeral=True
            )

        btn = Button(label=label, style=discord.ButtonStyle.success)
        btn.callback = callback
        return btn

# =========================
# COMMANDS
# =========================
@tree.command(name="buy", description="Buy access (Recruit / Elite / Fighter)")
async def buy(interaction: discord.Interaction):
    view = BuyView(user_id=interaction.user.id)
    await interaction.response.send_message(
        "Choose a package to purchase:",
        view=view,
        ephemeral=True
    )

@tree.command(name="buy_recruit", description="Buy Recruit ($7 / 14 days)")
async def buy_recruit(interaction: discord.Interaction):
    url = create_checkout_session(discord_user_id=interaction.user.id, price_id=PRICE_RECRUIT)
    await interaction.response.send_message(f"✅ Recruit checkout link:\n{url}", ephemeral=True)

@tree.command(name="buy_elite", description="Buy Elite ($19 / 30 days)")
async def buy_elite(interaction: discord.Interaction):
    url = create_checkout_session(discord_user_id=interaction.user.id, price_id=PRICE_ELITE)
    await interaction.response.send_message(f"✅ Elite checkout link:\n{url}", ephemeral=True)

@tree.command(name="buy_fighter", description="Buy Fighter ($49 / 60 days)")
async def buy_fighter(interaction: discord.Interaction):
    url = create_checkout_session(discord_user_id=interaction.user.id, price_id=PRICE_FIGHTER)
    await interaction.response.send_message(f"✅ Fighter checkout link:\n{url}", ephemeral=True)

# =========================
# STARTUP
# =========================
@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")
    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} commands")
    except Exception as e:
        print("Sync error:", e)

client.run(DISCORD_TOKEN)