import asyncio
import logging
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks


TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/warning_bot.db"))
WARNING_LOG_CHANNEL_ID = int(os.getenv("WARNING_LOG_CHANNEL_ID", "0"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))
ASSISTANT_ADMIN_ROLE_ID = int(os.getenv("ASSISTANT_ADMIN_ROLE_ID", "0"))
WARNING_ROLE_ID = int(os.getenv("WARNING_ROLE_ID", "0"))
PERMANENT_WARNING_ROLE_ID = int(os.getenv("PERMANENT_WARNING_ROLE_ID", "0"))
INHOUSE_SUSPENDED_ROLE_ID = int(os.getenv("INHOUSE_SUSPENDED_ROLE_ID", "0"))
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0"))

UTC = timezone.utc
WARNING_DURATION = timedelta(days=30)
COLOR_WARNING = 0x7666D8
COLOR_DEDUCTION = 0x63C49A
COLOR_EXPIRATION = 0x8A93A6
COLOR_SUSPENSION = 0xE36D6D
COLOR_SUSPENSION_RELEASE = 0x62B7A5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("warning-bot")


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def discord_time(value: datetime | None) -> str:
    if value is None:
        return "해당 없음"
    timestamp = int(value.timestamp())
    return f"<t:{timestamp}:F> (<t:{timestamp}:R>)"


class WarningStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS warning_state (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    warning_count INTEGER NOT NULL DEFAULT 0
                        CHECK (warning_count BETWEEN 0 AND 2),
                    temporary_expires_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS warning_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    action TEXT NOT NULL
                        CHECK (action IN ('grant', 'deduct', 'expire')),
                    reason TEXT NOT NULL,
                    actor_id INTEGER,
                    before_count INTEGER NOT NULL,
                    after_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_warning_history_member
                ON warning_history(guild_id, user_id, id DESC);

                CREATE INDEX IF NOT EXISTS idx_warning_expiration
                ON warning_state(warning_count, temporary_expires_at);

                CREATE TABLE IF NOT EXISTS inhouse_suspension_state (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
                    reason TEXT NOT NULL,
                    actor_id INTEGER,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS inhouse_suspension_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    action TEXT NOT NULL CHECK (action IN ('grant', 'release')),
                    reason TEXT NOT NULL,
                    actor_id INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_inhouse_suspension_history_member
                ON inhouse_suspension_history(guild_id, user_id, id DESC);
                """
            )
            connection.commit()

    async def get_state(self, guild_id: int, user_id: int) -> dict:
        async with self.lock:
            with closing(self.connect()) as connection:
                row = connection.execute(
                    """SELECT warning_count, temporary_expires_at, updated_at
                    FROM warning_state WHERE guild_id = ? AND user_id = ?""",
                    (guild_id, user_id),
                ).fetchone()
        if row is None:
            return {"warning_count": 0, "temporary_expires_at": None, "updated_at": None}
        return dict(row)

    async def save_transition(
        self,
        guild_id: int,
        user_id: int,
        before_count: int,
        after_count: int,
        expires_at: datetime | None,
        action: str,
        reason: str,
        actor_id: int | None,
    ) -> None:
        now = to_iso(utcnow())
        async with self.lock:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT warning_count FROM warning_state WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()
                actual_count = int(current[0]) if current else 0
                if actual_count != before_count:
                    connection.rollback()
                    raise RuntimeError("경고 상태가 다른 작업에 의해 변경되었습니다.")
                connection.execute(
                    """INSERT INTO warning_state
                    (guild_id, user_id, warning_count, temporary_expires_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                        warning_count = excluded.warning_count,
                        temporary_expires_at = excluded.temporary_expires_at,
                        updated_at = excluded.updated_at""",
                    (guild_id, user_id, after_count, to_iso(expires_at), now),
                )
                connection.execute(
                    """INSERT INTO warning_history
                    (guild_id, user_id, action, reason, actor_id,
                     before_count, after_count, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        guild_id,
                        user_id,
                        action,
                        reason,
                        actor_id,
                        before_count,
                        after_count,
                        now,
                    ),
                )
                connection.commit()

    async def due_temporary_warnings(self) -> list[dict]:
        async with self.lock:
            with closing(self.connect()) as connection:
                rows = connection.execute(
                    """SELECT guild_id, user_id, temporary_expires_at
                    FROM warning_state
                    WHERE warning_count = 1
                      AND temporary_expires_at IS NOT NULL
                      AND temporary_expires_at <= ?""",
                    (to_iso(utcnow()),),
                ).fetchall()
        return [dict(row) for row in rows]

    async def recent_history(self, guild_id: int, user_id: int, limit: int = 5) -> list[dict]:
        async with self.lock:
            with closing(self.connect()) as connection:
                rows = connection.execute(
                    """SELECT action, reason, actor_id, before_count, after_count, created_at
                    FROM warning_history
                    WHERE guild_id = ? AND user_id = ?
                    ORDER BY id DESC LIMIT ?""",
                    (guild_id, user_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    async def get_suspension(self, guild_id: int, user_id: int) -> dict:
        async with self.lock:
            with closing(self.connect()) as connection:
                row = connection.execute(
                    """SELECT active, reason, actor_id, updated_at
                    FROM inhouse_suspension_state
                    WHERE guild_id = ? AND user_id = ?""",
                    (guild_id, user_id),
                ).fetchone()
        if row is None:
            return {"active": 0, "reason": "", "actor_id": None, "updated_at": None}
        return dict(row)

    async def save_suspension(
        self,
        guild_id: int,
        user_id: int,
        expected_active: bool,
        active: bool,
        action: str,
        reason: str,
        actor_id: int | None,
    ) -> None:
        now = to_iso(utcnow())
        async with self.lock:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    """SELECT active FROM inhouse_suspension_state
                    WHERE guild_id = ? AND user_id = ?""",
                    (guild_id, user_id),
                ).fetchone()
                actual_active = bool(current[0]) if current else False
                if actual_active != expected_active:
                    connection.rollback()
                    raise RuntimeError("내전정지 상태가 다른 작업에 의해 변경되었습니다.")
                connection.execute(
                    """INSERT INTO inhouse_suspension_state
                    (guild_id, user_id, active, reason, actor_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                        active = excluded.active,
                        reason = excluded.reason,
                        actor_id = excluded.actor_id,
                        updated_at = excluded.updated_at""",
                    (guild_id, user_id, int(active), reason, actor_id, now),
                )
                connection.execute(
                    """INSERT INTO inhouse_suspension_history
                    (guild_id, user_id, action, reason, actor_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (guild_id, user_id, action, reason, actor_id, now),
                )
                connection.commit()

    async def recent_suspension_history(
        self, guild_id: int, user_id: int, limit: int = 5
    ) -> list[dict]:
        async with self.lock:
            with closing(self.connect()) as connection:
                rows = connection.execute(
                    """SELECT action, reason, actor_id, created_at
                    FROM inhouse_suspension_history
                    WHERE guild_id = ? AND user_id = ?
                    ORDER BY id DESC LIMIT ?""",
                    (guild_id, user_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]


intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
store = WarningStore(DATABASE_PATH)
member_locks: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)


def is_warning_manager(member: discord.Member) -> bool:
    allowed = {ADMIN_ROLE_ID, ASSISTANT_ADMIN_ROLE_ID} - {0}
    return bool(allowed.intersection(role.id for role in member.roles))


async def require_warning_manager(interaction: discord.Interaction) -> bool:
    if isinstance(interaction.user, discord.Member) and is_warning_manager(interaction.user):
        return True
    await interaction.response.send_message(
        "❌ 관리자 또는 보조관리자 역할을 가진 서버원만 사용할 수 있습니다.",
        ephemeral=True,
    )
    return False


def configured_roles(guild: discord.Guild) -> tuple[discord.Role, discord.Role]:
    warning_role = guild.get_role(WARNING_ROLE_ID)
    permanent_role = guild.get_role(PERMANENT_WARNING_ROLE_ID)
    if warning_role is None or permanent_role is None:
        raise RuntimeError("경고 역할을 찾을 수 없습니다. 역할 ID를 확인해 주세요.")
    if warning_role.id == permanent_role.id:
        raise RuntimeError("경고 1회 역할과 영구경고 역할은 서로 달라야 합니다.")
    return warning_role, permanent_role


def configured_suspension_role(guild: discord.Guild) -> discord.Role:
    role = guild.get_role(INHOUSE_SUSPENDED_ROLE_ID) if INHOUSE_SUSPENDED_ROLE_ID else None
    if role is None:
        role = discord.utils.get(guild.roles, name="내전정지")
    if role is None:
        raise RuntimeError(
            "내전정지 역할을 찾을 수 없습니다. 서버에 '내전정지' 역할을 만들거나 "
            "INHOUSE_SUSPENDED_ROLE_ID를 설정해 주세요."
        )
    return role


def validate_single_role_control(
    guild: discord.Guild, member: discord.Member, role: discord.Role
) -> None:
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        raise RuntimeError("봇에 역할 관리 권한이 없습니다.")
    if role >= me.top_role:
        raise RuntimeError("봇 역할을 내전정지 역할보다 위로 올려 주세요.")
    if member.id == guild.owner_id:
        raise RuntimeError("서버 소유자의 역할은 봇이 변경할 수 없습니다.")
    if member.top_role >= me.top_role:
        raise RuntimeError("대상의 역할이 봇 역할보다 높아 역할을 변경할 수 없습니다.")


async def set_suspension_role(
    member: discord.Member, active: bool, reason: str
) -> None:
    role = configured_suspension_role(member.guild)
    validate_single_role_control(member.guild, member, role)
    if active and role not in member.roles:
        await member.add_roles(role, reason=reason)
    elif not active and role in member.roles:
        await member.remove_roles(role, reason=reason)


def validate_role_control(
    guild: discord.Guild,
    member: discord.Member,
    warning_role: discord.Role,
    permanent_role: discord.Role,
) -> None:
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        raise RuntimeError("봇에 역할 관리 권한이 없습니다.")
    if warning_role >= me.top_role or permanent_role >= me.top_role:
        raise RuntimeError("봇 역할을 경고 역할보다 위로 올려 주세요.")
    if member.id != guild.owner_id and member.top_role >= me.top_role:
        raise RuntimeError("대상의 역할이 봇 역할보다 높아 역할을 변경할 수 없습니다.")
    if member.id == guild.owner_id:
        raise RuntimeError("서버 소유자의 역할은 봇이 변경할 수 없습니다.")


def desired_roles(
    member: discord.Member,
    warning_role: discord.Role,
    permanent_role: discord.Role,
    warning_count: int,
) -> list[discord.Role]:
    roles = [
        role
        for role in member.roles
        if role != member.guild.default_role and role.id not in {warning_role.id, permanent_role.id}
    ]
    if warning_count == 1:
        roles.append(warning_role)
    elif warning_count >= 2:
        roles.append(permanent_role)
    return roles


async def set_warning_roles(member: discord.Member, warning_count: int, reason: str) -> None:
    warning_role, permanent_role = configured_roles(member.guild)
    validate_role_control(member.guild, member, warning_role, permanent_role)
    roles = desired_roles(member, warning_role, permanent_role, warning_count)
    await member.edit(roles=roles, reason=reason)


async def warning_log_channel(guild: discord.Guild):
    if not WARNING_LOG_CHANNEL_ID:
        return None
    channel = guild.get_channel(WARNING_LOG_CHANNEL_ID) or bot.get_channel(WARNING_LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(WARNING_LOG_CHANNEL_ID)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None
    return channel if hasattr(channel, "send") else None


async def send_warning_log(guild: discord.Guild, embed: discord.Embed) -> discord.Message | None:
    channel = await warning_log_channel(guild)
    if channel is None:
        return None
    try:
        return await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        log.exception("경고 기록 채널 전송 실패: guild=%s channel=%s", guild.id, WARNING_LOG_CHANNEL_ID)
        return None


def state_label(count: int) -> str:
    if count <= 0:
        return "경고 없음"
    if count == 1:
        return "경고 1회 · 30일 후 자동 해제"
    return "영구경고 · 자동 만료 없음"


def history_label(row: dict) -> str:
    action = {"grant": "부여", "deduct": "차감", "expire": "자동 만료"}.get(row["action"], row["action"])
    timestamp = int(from_iso(row["created_at"]).timestamp())
    reason = discord.utils.escape_markdown(row["reason"])[:120]
    return f"<t:{timestamp}:d> · {action} · {row['before_count']}→{row['after_count']}회\n{reason}"


async def expire_warning(guild: discord.Guild, user_id: int) -> bool:
    async with member_locks[(guild.id, user_id)]:
        state = await store.get_state(guild.id, user_id)
        count = int(state["warning_count"])
        expires_at = from_iso(state["temporary_expires_at"])
        if count != 1 or expires_at is None or expires_at > utcnow():
            return False

        member = guild.get_member(user_id)
        if member is not None:
            try:
                await set_warning_roles(member, 0, "경고 1회 부여 후 30일 경과")
            except (RuntimeError, discord.Forbidden, discord.HTTPException) as error:
                log.warning("만료 경고 역할 제거 실패: guild=%s user=%s error=%s", guild.id, user_id, error)
                return False

        await store.save_transition(
            guild.id,
            user_id,
            1,
            0,
            None,
            "expire",
            "경고 1회 부여 후 30일 경과",
            bot.user.id if bot.user else None,
        )

        embed = discord.Embed(
            title="🕊️ 경고봇 · 경고 자동 해제",
            description=f"<@{user_id}>님의 경고 1회가 30일 경과로 자동 해제되었습니다.",
            color=COLOR_EXPIRATION,
            timestamp=utcnow(),
        )
        embed.add_field(name="현재 상태", value="**경고 없음**", inline=True)
        embed.add_field(name="처리 사유", value="경고 1회 부여 후 30일 경과", inline=False)
        embed.set_footer(text="경고봇 자동 처리")
        await send_warning_log(guild, embed)
        return True


async def normalize_expired_state(member: discord.Member) -> None:
    state = await store.get_state(member.guild.id, member.id)
    expires_at = from_iso(state["temporary_expires_at"])
    if int(state["warning_count"]) == 1 and expires_at and expires_at <= utcnow():
        await expire_warning(member.guild, member.id)


@bot.tree.command(name="경고부여", description="서버원에게 경고를 1회 부여합니다.")
@app_commands.describe(대상="경고를 받을 서버원", 사유="경고 부여 사유")
@app_commands.guild_only()
async def grant_warning(interaction: discord.Interaction, 대상: discord.Member, 사유: str):
    if not await require_warning_manager(interaction):
        return
    if 대상.bot:
        await interaction.response.send_message("❌ 봇에게는 경고를 부여할 수 없습니다.", ephemeral=True)
        return
    reason = 사유.strip()
    if len(reason) < 2:
        await interaction.response.send_message("❌ 경고 사유를 두 글자 이상 작성해 주세요.", ephemeral=True)
        return
    if len(reason) > 1000:
        await interaction.response.send_message("❌ 경고 사유는 1,000자 이하로 작성해 주세요.", ephemeral=True)
        return
    if await warning_log_channel(interaction.guild) is None:
        await interaction.response.send_message(
            "❌ 경고 기록 채널을 찾을 수 없습니다. WARNING_LOG_CHANNEL_ID와 채널 권한을 확인해 주세요.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    async with member_locks[(interaction.guild.id, 대상.id)]:
        state = await store.get_state(interaction.guild.id, 대상.id)
        before_count = int(state["warning_count"])
        expires_at = from_iso(state["temporary_expires_at"])
        if before_count == 1 and expires_at and expires_at <= utcnow():
            # 명령 실행 직전에 만료된 경고는 새 경고와 합산하지 않는다.
            await set_warning_roles(대상, 0, "만료된 경고 정리")
            await store.save_transition(
                interaction.guild.id,
                대상.id,
                1,
                0,
                None,
                "expire",
                "경고 1회 부여 후 30일 경과",
                bot.user.id if bot.user else None,
            )
            before_count = 0

        if before_count >= 2:
            await interaction.followup.send(
                f"ℹ️ {대상.mention}님은 이미 영구경고 상태입니다.", ephemeral=True
            )
            return

        after_count = before_count + 1
        new_expires_at = utcnow() + WARNING_DURATION if after_count == 1 else None
        old_roles = list(대상.roles[1:])
        try:
            await set_warning_roles(대상, after_count, f"경고 부여: {reason[:300]}")
            await store.save_transition(
                interaction.guild.id,
                대상.id,
                before_count,
                after_count,
                new_expires_at,
                "grant",
                reason,
                interaction.user.id,
            )
        except (RuntimeError, sqlite3.Error, discord.Forbidden, discord.HTTPException) as error:
            if not isinstance(error, RuntimeError) or "다른 작업" in str(error):
                try:
                    await 대상.edit(roles=old_roles, reason="경고 저장 실패 복구")
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("경고 역할 복구 실패: guild=%s user=%s", interaction.guild.id, 대상.id)
            await interaction.followup.send(f"❌ 경고를 처리하지 못했습니다: {error}", ephemeral=True)
            return

    result = "`경고 1회` 역할을 지급했습니다."
    if after_count == 2:
        result = "`경고 1회` 역할을 제거하고 `영구경고` 역할을 지급했습니다. 추가 자동 제재는 없습니다."
    embed = discord.Embed(
        title="🛡️ 경고봇 · 경고 부여",
        description=f"{대상.mention}님에게 경고가 부여되었습니다.",
        color=COLOR_WARNING,
        timestamp=utcnow(),
    )
    embed.set_thumbnail(url=대상.display_avatar.url)
    embed.add_field(name="대상", value=f"{대상.mention}\n`{대상.id}`", inline=True)
    embed.add_field(name="누적 경고", value=f"**{after_count}회**", inline=True)
    embed.add_field(name="현재 단계", value=state_label(after_count), inline=False)
    embed.add_field(name="경고 사유", value=discord.utils.escape_markdown(reason), inline=False)
    embed.add_field(name="처리 결과", value=result, inline=False)
    if new_expires_at:
        embed.add_field(name="자동 해제 예정", value=discord_time(new_expires_at), inline=False)
    embed.set_footer(text=f"처리 관리자: {interaction.user}")
    message = await send_warning_log(interaction.guild, embed)
    if message:
        await interaction.followup.send(f"✅ 경고를 처리하고 {message.jump_url}에 기록했습니다.", ephemeral=True)
    else:
        await interaction.followup.send("⚠️ 경고와 역할은 처리했지만 기록 채널 전송에 실패했습니다.", ephemeral=True)


@bot.tree.command(name="경고차감", description="사유를 기록하고 경고를 1회 차감합니다.")
@app_commands.describe(대상="경고를 차감할 서버원", 사유="경고 차감 사유")
@app_commands.guild_only()
async def deduct_warning(interaction: discord.Interaction, 대상: discord.Member, 사유: str):
    if not await require_warning_manager(interaction):
        return
    if 대상.bot:
        await interaction.response.send_message("❌ 봇은 경고 차감 대상이 될 수 없습니다.", ephemeral=True)
        return
    reason = 사유.strip()
    if len(reason) < 2:
        await interaction.response.send_message("❌ 경고 차감 사유를 두 글자 이상 작성해 주세요.", ephemeral=True)
        return
    if len(reason) > 1000:
        await interaction.response.send_message("❌ 경고 차감 사유는 1,000자 이하로 작성해 주세요.", ephemeral=True)
        return
    if await warning_log_channel(interaction.guild) is None:
        await interaction.response.send_message(
            "❌ 경고 기록 채널을 찾을 수 없습니다. 설정과 채널 권한을 확인해 주세요.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    async with member_locks[(interaction.guild.id, 대상.id)]:
        state = await store.get_state(interaction.guild.id, 대상.id)
        before_count = int(state["warning_count"])
        expires_at = from_iso(state["temporary_expires_at"])
        if before_count == 1 and expires_at and expires_at <= utcnow():
            await interaction.followup.send(
                "ℹ️ 해당 경고는 이미 만료 시각이 지났습니다. 잠시 후 자동 해제됩니다.",
                ephemeral=True,
            )
            return
        if before_count <= 0:
            await interaction.followup.send(
                f"ℹ️ {대상.mention}님은 차감할 경고가 없습니다.", ephemeral=True
            )
            return

        after_count = before_count - 1
        new_expires_at = utcnow() + WARNING_DURATION if after_count == 1 else None
        old_roles = list(대상.roles[1:])
        try:
            await set_warning_roles(대상, after_count, f"경고 차감: {reason[:300]}")
            await store.save_transition(
                interaction.guild.id,
                대상.id,
                before_count,
                after_count,
                new_expires_at,
                "deduct",
                reason,
                interaction.user.id,
            )
        except (RuntimeError, sqlite3.Error, discord.Forbidden, discord.HTTPException) as error:
            if not isinstance(error, RuntimeError) or "다른 작업" in str(error):
                try:
                    await 대상.edit(roles=old_roles, reason="경고 차감 저장 실패 복구")
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("차감 역할 복구 실패: guild=%s user=%s", interaction.guild.id, 대상.id)
            await interaction.followup.send(f"❌ 경고를 차감하지 못했습니다: {error}", ephemeral=True)
            return

    result = "모든 경고 역할을 제거했습니다."
    if after_count == 1:
        result = "`영구경고` 역할을 제거하고 `경고 1회` 역할을 지급했습니다. 30일 만료를 새로 시작합니다."
    embed = discord.Embed(
        title="🕊️ 경고봇 · 경고 차감",
        description=f"{대상.mention}님의 경고가 1회 차감되었습니다.",
        color=COLOR_DEDUCTION,
        timestamp=utcnow(),
    )
    embed.set_thumbnail(url=대상.display_avatar.url)
    embed.add_field(name="대상", value=f"{대상.mention}\n`{대상.id}`", inline=True)
    embed.add_field(name="남은 경고", value=f"**{after_count}회**", inline=True)
    embed.add_field(name="현재 단계", value=state_label(after_count), inline=False)
    embed.add_field(name="차감 사유", value=discord.utils.escape_markdown(reason), inline=False)
    embed.add_field(name="처리 결과", value=result, inline=False)
    if new_expires_at:
        embed.add_field(name="자동 해제 예정", value=discord_time(new_expires_at), inline=False)
    embed.set_footer(text=f"처리 관리자: {interaction.user}")
    message = await send_warning_log(interaction.guild, embed)
    if message:
        await interaction.followup.send(f"✅ 경고를 차감하고 {message.jump_url}에 기록했습니다.", ephemeral=True)
    else:
        await interaction.followup.send("⚠️ 경고와 역할은 처리했지만 기록 채널 전송에 실패했습니다.", ephemeral=True)


@bot.tree.command(name="경고조회", description="현재 경고 상태와 최근 처리 내역을 확인합니다.")
@app_commands.describe(대상="조회할 서버원 (관리자/보조관리자만 다른 사람 조회 가능)")
@app_commands.guild_only()
async def lookup_warning(interaction: discord.Interaction, 대상: discord.Member | None = None):
    target = 대상 or interaction.user
    if not isinstance(target, discord.Member):
        await interaction.response.send_message("❌ 서버 안에서만 조회할 수 있습니다.", ephemeral=True)
        return
    if target.id != interaction.user.id and not (
        isinstance(interaction.user, discord.Member) and is_warning_manager(interaction.user)
    ):
        await interaction.response.send_message("❌ 일반 서버원은 자신의 경고만 조회할 수 있습니다.", ephemeral=True)
        return

    await normalize_expired_state(target)
    state = await store.get_state(interaction.guild.id, target.id)
    count = int(state["warning_count"])
    expires_at = from_iso(state["temporary_expires_at"])
    history = await store.recent_history(interaction.guild.id, target.id)
    embed = discord.Embed(
        title="🛡️ 경고봇 · 경고 조회",
        description=f"{target.mention}님의 경고 현황입니다.",
        color=COLOR_WARNING,
        timestamp=utcnow(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="현재 경고", value=f"**{count}회**", inline=True)
    embed.add_field(name="현재 단계", value=state_label(count), inline=True)
    if count == 1:
        embed.add_field(name="자동 해제 예정", value=discord_time(expires_at), inline=False)
    embed.add_field(
        name="최근 처리 내역",
        value="\n\n".join(history_label(row) for row in history) if history else "처리 내역이 없습니다.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


def suspension_history_label(row: dict) -> str:
    action = {"grant": "정지 부여", "release": "정지 해제"}.get(
        row["action"], row["action"]
    )
    timestamp = int(from_iso(row["created_at"]).timestamp())
    reason = discord.utils.escape_markdown(row["reason"])[:120]
    return f"<t:{timestamp}:d> · {action}\n{reason}"


def valid_reason(value: str) -> str:
    reason = value.strip()
    if len(reason) < 2:
        raise ValueError("사유를 두 글자 이상 작성해 주세요.")
    if len(reason) > 1000:
        raise ValueError("사유는 1,000자 이하로 작성해 주세요.")
    return reason


@bot.tree.command(name="내전정지부여", description="서버원에게 내전정지 역할을 부여합니다.")
@app_commands.describe(대상="내전 참여를 정지할 서버원", 사유="내전정지 부여 사유")
@app_commands.guild_only()
async def grant_inhouse_suspension(
    interaction: discord.Interaction, 대상: discord.Member, 사유: str
):
    if not await require_warning_manager(interaction):
        return
    if 대상.bot:
        await interaction.response.send_message("❌ 봇은 내전정지 대상이 될 수 없습니다.", ephemeral=True)
        return
    try:
        reason = valid_reason(사유)
    except ValueError as error:
        await interaction.response.send_message(f"❌ {error}", ephemeral=True)
        return
    if await warning_log_channel(interaction.guild) is None:
        await interaction.response.send_message(
            "❌ 기록 채널을 찾을 수 없습니다. 설정과 채널 권한을 확인해 주세요.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    async with member_locks[(interaction.guild.id, 대상.id)]:
        state = await store.get_suspension(interaction.guild.id, 대상.id)
        if bool(state["active"]):
            await interaction.followup.send(
                f"ℹ️ {대상.mention}님은 이미 내전정지 상태입니다.", ephemeral=True
            )
            return
        role = configured_suspension_role(interaction.guild)
        previously_had_role = role in 대상.roles
        try:
            await set_suspension_role(대상, True, f"내전정지 부여: {reason[:300]}")
            await store.save_suspension(
                interaction.guild.id,
                대상.id,
                False,
                True,
                "grant",
                reason,
                interaction.user.id,
            )
        except (RuntimeError, sqlite3.Error, discord.Forbidden, discord.HTTPException) as error:
            if not previously_had_role:
                try:
                    await set_suspension_role(대상, False, "내전정지 저장 실패 복구")
                except (RuntimeError, discord.Forbidden, discord.HTTPException):
                    log.exception("내전정지 역할 복구 실패: guild=%s user=%s", interaction.guild.id, 대상.id)
            await interaction.followup.send(f"❌ 내전정지를 처리하지 못했습니다: {error}", ephemeral=True)
            return

    embed = discord.Embed(
        title="⛔ 경고봇 · 내전정지 부여",
        description=f"{대상.mention}님에게 내전정지 역할이 부여되었습니다.",
        color=COLOR_SUSPENSION,
        timestamp=utcnow(),
    )
    embed.set_thumbnail(url=대상.display_avatar.url)
    embed.add_field(name="대상", value=f"{대상.mention}\n`{대상.id}`", inline=True)
    embed.add_field(name="현재 상태", value="**내전 참여 정지**", inline=True)
    embed.add_field(name="정지 사유", value=discord.utils.escape_markdown(reason), inline=False)
    embed.set_footer(text=f"처리 관리자: {interaction.user}")
    message = await send_warning_log(interaction.guild, embed)
    if message:
        await interaction.followup.send(
            f"✅ 내전정지를 부여하고 {message.jump_url}에 기록했습니다.", ephemeral=True
        )
    else:
        await interaction.followup.send(
            "⚠️ 역할과 상태는 처리했지만 기록 채널 전송에 실패했습니다.", ephemeral=True
        )


@bot.tree.command(name="내전정지해제", description="사유를 기록하고 내전정지를 해제합니다.")
@app_commands.describe(대상="내전정지를 해제할 서버원", 사유="내전정지 해제 사유")
@app_commands.guild_only()
async def release_inhouse_suspension(
    interaction: discord.Interaction, 대상: discord.Member, 사유: str
):
    if not await require_warning_manager(interaction):
        return
    if 대상.bot:
        await interaction.response.send_message("❌ 봇은 내전정지 해제 대상이 될 수 없습니다.", ephemeral=True)
        return
    try:
        reason = valid_reason(사유)
    except ValueError as error:
        await interaction.response.send_message(f"❌ {error}", ephemeral=True)
        return
    if await warning_log_channel(interaction.guild) is None:
        await interaction.response.send_message(
            "❌ 기록 채널을 찾을 수 없습니다. 설정과 채널 권한을 확인해 주세요.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    async with member_locks[(interaction.guild.id, 대상.id)]:
        state = await store.get_suspension(interaction.guild.id, 대상.id)
        if not bool(state["active"]):
            await interaction.followup.send(
                f"ℹ️ {대상.mention}님은 내전정지 상태가 아닙니다.", ephemeral=True
            )
            return
        role = configured_suspension_role(interaction.guild)
        previously_had_role = role in 대상.roles
        try:
            await set_suspension_role(대상, False, f"내전정지 해제: {reason[:300]}")
            await store.save_suspension(
                interaction.guild.id,
                대상.id,
                True,
                False,
                "release",
                reason,
                interaction.user.id,
            )
        except (RuntimeError, sqlite3.Error, discord.Forbidden, discord.HTTPException) as error:
            if previously_had_role:
                try:
                    await set_suspension_role(대상, True, "내전정지 해제 저장 실패 복구")
                except (RuntimeError, discord.Forbidden, discord.HTTPException):
                    log.exception("내전정지 역할 복구 실패: guild=%s user=%s", interaction.guild.id, 대상.id)
            await interaction.followup.send(f"❌ 내전정지를 해제하지 못했습니다: {error}", ephemeral=True)
            return

    embed = discord.Embed(
        title="✅ 경고봇 · 내전정지 해제",
        description=f"{대상.mention}님의 내전정지가 해제되었습니다.",
        color=COLOR_SUSPENSION_RELEASE,
        timestamp=utcnow(),
    )
    embed.set_thumbnail(url=대상.display_avatar.url)
    embed.add_field(name="대상", value=f"{대상.mention}\n`{대상.id}`", inline=True)
    embed.add_field(name="현재 상태", value="**내전정지 없음**", inline=True)
    embed.add_field(name="해제 사유", value=discord.utils.escape_markdown(reason), inline=False)
    embed.set_footer(text=f"처리 관리자: {interaction.user}")
    message = await send_warning_log(interaction.guild, embed)
    if message:
        await interaction.followup.send(
            f"✅ 내전정지를 해제하고 {message.jump_url}에 기록했습니다.", ephemeral=True
        )
    else:
        await interaction.followup.send(
            "⚠️ 역할과 상태는 처리했지만 기록 채널 전송에 실패했습니다.", ephemeral=True
        )


@bot.tree.command(name="내전정지조회", description="현재 내전정지 상태와 최근 처리 내역을 확인합니다.")
@app_commands.describe(대상="조회할 서버원 (관리자/보조관리자만 다른 사람 조회 가능)")
@app_commands.guild_only()
async def lookup_inhouse_suspension(
    interaction: discord.Interaction, 대상: discord.Member | None = None
):
    target = 대상 or interaction.user
    if not isinstance(target, discord.Member):
        await interaction.response.send_message("❌ 서버 안에서만 조회할 수 있습니다.", ephemeral=True)
        return
    if target.id != interaction.user.id and not (
        isinstance(interaction.user, discord.Member) and is_warning_manager(interaction.user)
    ):
        await interaction.response.send_message(
            "❌ 일반 서버원은 자신의 내전정지 상태만 조회할 수 있습니다.", ephemeral=True
        )
        return

    state = await store.get_suspension(interaction.guild.id, target.id)
    history = await store.recent_suspension_history(interaction.guild.id, target.id)
    active = bool(state["active"])
    embed = discord.Embed(
        title="🎮 경고봇 · 내전정지 조회",
        description=f"{target.mention}님의 내전정지 현황입니다.",
        color=COLOR_SUSPENSION if active else COLOR_SUSPENSION_RELEASE,
        timestamp=utcnow(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(
        name="현재 상태",
        value="**⛔ 내전 참여 정지**" if active else "**✅ 내전정지 없음**",
        inline=False,
    )
    if active:
        embed.add_field(
            name="정지 사유",
            value=discord.utils.escape_markdown(state["reason"]),
            inline=False,
        )
        embed.add_field(
            name="부여 시각",
            value=discord_time(from_iso(state["updated_at"])),
            inline=False,
        )
    embed.add_field(
        name="최근 처리 내역",
        value=(
            "\n\n".join(suspension_history_label(row) for row in history)
            if history
            else "처리 내역이 없습니다."
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tasks.loop(minutes=5, reconnect=True)
async def enforce_expirations():
    for row in await store.due_temporary_warnings():
        guild = bot.get_guild(int(row["guild_id"]))
        if guild is not None:
            try:
                await expire_warning(guild, int(row["user_id"]))
            except Exception:
                log.exception(
                    "경고 자동 만료 처리 오류: guild=%s user=%s",
                    row["guild_id"],
                    row["user_id"],
                )


@enforce_expirations.before_loop
async def before_enforce_expirations():
    await bot.wait_until_ready()


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    async with member_locks[(member.guild.id, member.id)]:
        state = await store.get_state(member.guild.id, member.id)
        count = int(state["warning_count"])
        expires_at = from_iso(state["temporary_expires_at"])
        if count == 1 and expires_at and expires_at <= utcnow():
            await store.save_transition(
                member.guild.id,
                member.id,
                1,
                0,
                None,
                "expire",
                "재입장 시 만료된 경고 정리",
                bot.user.id if bot.user else None,
            )
            count = 0
        if count:
            try:
                await set_warning_roles(member, count, "재입장한 서버원의 경고 역할 복구")
            except (RuntimeError, discord.Forbidden, discord.HTTPException):
                log.exception("재입장 경고 역할 복구 실패: guild=%s user=%s", member.guild.id, member.id)

        suspension = await store.get_suspension(member.guild.id, member.id)
        if bool(suspension["active"]):
            try:
                await set_suspension_role(member, True, "재입장한 서버원의 내전정지 역할 복구")
            except (RuntimeError, discord.Forbidden, discord.HTTPException):
                log.exception(
                    "재입장 내전정지 역할 복구 실패: guild=%s user=%s",
                    member.guild.id,
                    member.id,
                )


@bot.event
async def on_ready():
    if not enforce_expirations.is_running():
        enforce_expirations.start()
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)


async def setup_hook():
    if TEST_GUILD_ID:
        guild = discord.Object(id=TEST_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()


bot.setup_hook = setup_hook


if __name__ == "__main__":
    required = {
        "DISCORD_TOKEN": TOKEN,
        "WARNING_LOG_CHANNEL_ID": WARNING_LOG_CHANNEL_ID,
        "ADMIN_ROLE_ID": ADMIN_ROLE_ID,
        "ASSISTANT_ADMIN_ROLE_ID": ASSISTANT_ADMIN_ROLE_ID,
        "WARNING_ROLE_ID": WARNING_ROLE_ID,
        "PERMANENT_WARNING_ROLE_ID": PERMANENT_WARNING_ROLE_ID,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError("필수 환경 변수가 없습니다: " + ", ".join(missing))
    bot.run(TOKEN)
