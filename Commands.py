import discord
from discord import app_commands
from datetime import datetime, timedelta, timezone

import db
from config import (
    ROLE_VISITORS, ROLE_MEMBER, ROLE_COACH, ROLE_CONSISTENT,
    TIER_ROLE_MAP, CONSISTENT_REQUIRED, CONSISTENT_WINDOW_DAYS
)

def get_role(guild: discord.Guild, name: str):
    return discord.utils.get(guild.roles, name=name)

async def apply_tier_roles(guild: discord.Guild, member: discord.Member, tier: str):
    # remove all tier roles
    for role_name in set(TIER_ROLE_MAP.values()):
        r = get_role(guild, role_name)
        if r and r in member.roles:
            await member.remove_roles(r, reason="Tier sync")

    # add correct role
    target_name = TIER_ROLE_MAP.get(tier, ROLE_MEMBER)
    target = get_role(guild, target_name)
    if target:
        await member.add_roles(target, reason="Tier sync")

async def ensure_consistent_role(guild: discord.Guild, member: discord.Member):
    since = datetime.now(timezone.utc) - timedelta(days=CONSISTENT_WINDOW_DAYS)
    n = db.count_done_in_window(member.id, since)
    consistent = get_role(guild, ROLE_CONSISTENT)
    if not consistent:
        return
    if n >= CONSISTENT_REQUIRED and consistent not in member.roles:
        await member.add_roles(consistent, reason="Earned Consistent")
    if n < CONSISTENT_REQUIRED and consistent in member.roles:
        await member.remove_roles(consistent, reason="Lost Consistent")

def register_commands(tree: app_commands.CommandTree, client: discord.Client):

    @tree.command(name="ping", description="Test if the bot is online")
    async def ping(interaction: discord.Interaction):
        await interaction.response.send_message("pong 🥋", ephemeral=True)

    @tree.command(name="start", description="Unlock training access (Visitors -> Member)")
    async def start(interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user

        visitors = get_role(guild, ROLE_VISITORS)
        member_role = get_role(guild, ROLE_MEMBER)

        if visitors and visitors in member.roles:
            await member.remove_roles(visitors, reason="Started program")
        if member_role:
            await member.add_roles(member_role, reason="Started program")

        db.ensure_user(member.id)
        await interaction.response.send_message("✅ You’re set. Go to training and reply DONE when you finish drills.", ephemeral=True)

    @tree.command(name="set_tier", description="Set a user's tier (free/premium/elite) and sync roles")
    @app_commands.describe(user="User to update", tier="free, premium, or elite")
    async def set_tier(interaction: discord.Interaction, user: discord.Member, tier: str):
        tier = tier.lower().strip()
        if tier not in {"free", "premium", "elite"}:
            await interaction.response.send_message("Tier must be: free, premium, or elite.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        db.ensure_user(user.id)
        db.set_tier(user.id, tier)

        await apply_tier_roles(interaction.guild, user, tier)
        await interaction.response.send_message(f"✅ {user.mention} set to **{tier.upper()}** and roles synced.", ephemeral=True)

    @tree.command(name="review", description="Coach review a submission by ID (approve/needs_work/reject)")
    @app_commands.describe(action="approve / needs_work / reject", submission_id="Submission ID", note="Optional coach note")
    async def review(interaction: discord.Interaction, action: str, submission_id: int, note: str | None = None):
        action = action.lower().strip()
        if action not in {"approve", "needs_work", "reject"}:
            await interaction.response.send_message("Action must be: approve, needs_work, reject", ephemeral=True)
            return

        # Coach-only (role OR admin)
        coach_role = get_role(interaction.guild, ROLE_COACH)
        is_coach = coach_role in interaction.user.roles if coach_role else False
        if not (is_coach or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Coach/Admin only.", ephemeral=True)
            return

        row = db.set_submission_status(submission_id, action, note)
        if not row:
            await interaction.response.send_message("Submission ID not found.", ephemeral=True)
            return

        target_id = int(row["discord_id"])
        msg_url = row["message_url"]

        # DM the user
        try:
            user = await client.fetch_user(target_id)
            text = f"🥋 Your video submission **#{submission_id}** was marked **{action.upper()}**.\n{msg_url}"
            if note:
                text += f"\n\nCoach note: {note}"
            await user.send(text)
        except Exception:
            pass

        await interaction.response.send_message(f"✅ Submission #{submission_id} set to **{action}**.", ephemeral=True)

    @tree.command(name="my_progress", description="See your training progress")
    async def my_progress(interaction: discord.Interaction):
        p = db.user_progress(interaction.user.id)
        await interaction.response.send_message(
            f"📈 Progress:\n• DONE last 7 days: **{p['last7']}**\n• Total DONEs: **{p['total']}**",
            ephemeral=True
        )
