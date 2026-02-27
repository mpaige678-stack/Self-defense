import os
import discord
from discord.ext import commands
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Controller bot ready: {bot.user}")
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(e)

@bot.tree.command(name="control", description="Open control panel", guild=discord.Object(id=GUILD_ID))
async def control(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Controller Bot Active ✅\n\nNext phase ready.",
        ephemeral=True
    )

bot.run(TOKEN)