import os
from zoneinfo import ZoneInfo

# --- Discord config ---
GUILD_ID = int(os.getenv("GUILD_ID", "0"))  # optional; if 0, sync commands globally (slower)
TIMEZONE = os.getenv("TIMEZONE", "America/Chicago")
TZ = ZoneInfo(TIMEZONE)

# Channel names (you confirmed “use channel names”)
CH_WEEKLY = "weekly-video"
CH_ARCHIVE = "video-archive"
CH_SUBMIT = "submit-video"
CH_WINS = "wins"
CH_DAILY = "daily-training"

# Role names (you confirmed “roles are correct”)
ROLE_VISITORS = "Visitors"
ROLE_MEMBER = "Member"
ROLE_PREMIUM = "Premium Member"
ROLE_ELITE = "PT"
ROLE_COACH = "Coach"

# Gamification roles
ROLE_CONSISTENT = "🔥 Consistent"

# Tier role map (DB tier -> role)
TIER_ROLE_MAP = {
    "free": ROLE_MEMBER,
    "premium": ROLE_PREMIUM,
    "elite": ROLE_ELITE,
}

# Streak rule: 7 DONEs in last 7 days -> Consistent
CONSISTENT_REQUIRED = 7
CONSISTENT_WINDOW_DAYS = 7

# Weekly schedule
WEEKLY_POST_HOUR = 5   # Monday 5am
WEEKLY_POST_MIN = 0

# Leaderboard post schedule
LEADERBOARD_POST_HOUR = 23  # Sunday 11pm
LEADERBOARD_POST_MIN = 0
