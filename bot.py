"""디스코드 팀 나누기 봇.

대기방(로비) 음성 채널에 모인 멤버를 무작위로 두 팀으로 나누고,
미리 만들어 둔 두 개의 음성 채널로 한 번에 이동시켜 줍니다.

명령어
    /팀나누기  팀1_채널 팀2_채널   → 대기방 인원을 반반으로 나눠 이동
    /모이기    대상_채널1 [모일_채널] [대상_채널2] → 흩어진 인원을 한 채널로 모음

결과 메시지의 버튼
    🔀 다시 섞기          같은 인원을 다시 섞어 이동
    📢 대기방으로 모으기   두 팀 채널의 인원을 처음 대기방으로 복귀

실행 방법은 같은 폴더의 README.md 를 참고하세요.

헬스체크 서버 (무료 클라우드 호스팅용, 선택)
    PORT 환경변수가 있거나 ENABLE_HEALTH_SERVER 를 켜면
    http://0.0.0.0:<포트>/health 로 200 OK 를 돌려주는 아주 가벼운 서버가
    백그라운드 스레드로 함께 뜹니다. (Render·Koyeb·Cloudtype 같은
    웹 서비스 타입 호스팅이 '포트를 안 열었다'며 컨테이너를 죽이지 않게)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable, Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()

# (선택) 서버 ID 를 넣으면 슬래시 명령어가 그 서버에 곧바로 등록됩니다.
# 비워 두면 전역 등록이라 디스코드에 반영되기까지 최대 1시간 정도 걸릴 수 있어요.
GUILD_ID_RAW = os.getenv("DISCORD_GUILD_ID", "").strip()
GUILD_ID: Optional[int] = int(GUILD_ID_RAW) if GUILD_ID_RAW.isdigit() else None

BUTTON_TIMEOUT = 60 * 30          # 결과 버튼을 누를 수 있는 시간(초)
MOVE_REASON = "팀 나누기 봇: 팀 이동"
PLACEHOLDER_TOKEN = "여기에_봇_토큰_입력"

DEFAULT_HEALTH_PORT = 8080        # PORT 없이 ENABLE_HEALTH_SERVER 만 켰을 때
HEALTH_PATHS = {"/", "/health", "/healthz"}

LOG = logging.getLogger("team_bot")


# ---------------------------------------------------------------------------
# 오류 타입
# ---------------------------------------------------------------------------


class TeamBotError(commands.CommandError):
    """사용자에게 그대로 보여 줄 한국어 안내가 담긴 오류."""


# ---------------------------------------------------------------------------
# 공용 도우미
# ---------------------------------------------------------------------------


def human_voice_members(channel: Optional[discord.VoiceChannel]) -> list[discord.Member]:
    """음성 채널에 있는 사람(봇 제외) 목록. 채널이 없으면 빈 목록."""
    if channel is None:
        return []
    return [member for member in channel.members if not member.bot]


def split_two(members: Sequence[discord.Member]) -> tuple[list[discord.Member], list[discord.Member]]:
    """무작위로 섞어 두 팀으로 나눕니다. (인원이 홀수면 팀 1이 한 명 더)"""
    pool = list(members)
    random.shuffle(pool)
    half = math.ceil(len(pool) / 2)
    return pool[:half], pool[half:]


async def move_members(
    members: Iterable[discord.Member],
    channel: discord.VoiceChannel,
    *,
    reason: str = MOVE_REASON,
) -> tuple[list[discord.Member], list[tuple[discord.Member, str]]]:
    """멤버들을 한 명씩 옮기고 (성공 목록, 실패 목록)을 돌려줍니다."""
    moved: list[discord.Member] = []
    failed: list[tuple[discord.Member, str]] = []

    for member in members:
        current = member.voice.channel if member.voice else None
        if current is not None and current.id == channel.id:
            moved.append(member)          # 이미 그 채널에 있으면 그대로 성공 처리
            continue
        try:
            await member.move_to(channel, reason=reason)
        except discord.Forbidden:
            failed.append((member, "'멤버 이동(Move Members)' 권한이 없어요"))
        except discord.HTTPException as exc:
            failed.append((member, f"디스코드 오류({exc.status})"))
        except Exception as exc:          # 예상 못 한 오류도 봇이 죽지 않게
            LOG.exception("멤버 이동 중 알 수 없는 오류: %s", member)
            failed.append((member, f"알 수 없는 오류({type(exc).__name__})"))
        else:
            moved.append(member)

    return moved, failed


def ensure_can_move(guild: discord.Guild, *channels: discord.VoiceChannel) -> None:
    """봇이 해당 채널들에서 멤버를 옮길 권한이 있는지 미리 확인합니다."""
    me = guild.me
    if me is None:
        raise TeamBotError("봇 정보를 확인할 수 없어요. 잠시 후 다시 시도해 주세요.")
    if not me.guild_permissions.move_members:
        raise TeamBotError(
            "봇에게 **'멤버 이동(Move Members)'** 권한이 없어요.\n"
            "서버 설정 → 역할 → 봇 역할에서 이 권한을 켜 주세요."
        )
    for channel in channels:
        if not channel.permissions_for(me).move_members:
            raise TeamBotError(
                f"'{channel.name}' 채널에서 봇이 멤버를 옮길 권한이 없어요.\n"
                "채널 설정 → 권한 → 봇 역할의 **'멤버 이동'** 을 허용해 주세요."
            )


def _field_value(members: Sequence[discord.Member], limit: int = 1000) -> str:
    """임베드 필드 글자 수 제한(1024자)에 맞춰 멘션 목록을 만듭니다."""
    if not members:
        return "—"
    lines: list[str] = []
    used = 0
    for member in members:
        line = member.mention
        if used + len(line) + 1 > limit:
            break
        lines.append(line)
        used += len(line) + 1
    hidden = len(members) - len(lines)
    text = "\n".join(lines)
    if hidden > 0:
        text += f"\n… 외 {hidden}명"
    return text


def build_result_embed(
    team1: Sequence[discord.Member],
    team2: Sequence[discord.Member],
    *,
    title: str = "🎲 팀 나누기 결과",
    description: str = "아래처럼 팀을 나눠 각 채널로 이동시켰어요.",
    footer_extra: str = "",
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        colour=discord.Colour.blurple(),
    )
    embed.add_field(name=f"🟦 팀 1 · {len(team1)}명", value=_field_value(team1), inline=True)
    embed.add_field(name=f"🟥 팀 2 · {len(team2)}명", value=_field_value(team2), inline=True)
    footer = f"총 {len(team1) + len(team2)}명 무작위 배정"
    if footer_extra:
        footer += f" · {footer_extra}"
    embed.set_footer(text=footer)
    return embed


def _failure_lines(failed: Sequence[tuple[discord.Member, str]]) -> str:
    return "\n".join(f"• {member.display_name}: {reason}" for member, reason in failed)


# ---------------------------------------------------------------------------
# 헬스체크 서버 (무료 클라우드 호스팅에서 컨테이너가 죽지 않게)
# ---------------------------------------------------------------------------


def health_port() -> Optional[int]:
    """헬스체크 서버를 띄울 포트. 필요 없으면 None.

    * PORT 가 있으면 그 포트 (Render·Koyeb 등이 자동으로 넣어 줍니다)
    * ENABLE_HEALTH_SERVER=true 만 있으면 8080
    """
    raw = os.getenv("PORT", "").strip()
    if raw.isdigit():
        return int(raw)
    if os.getenv("ENABLE_HEALTH_SERVER", "").strip().lower() in {"1", "true", "yes", "on"}:
        return DEFAULT_HEALTH_PORT
    return None


class _HealthHandler(BaseHTTPRequestHandler):
    """GET / 또는 /health 에 200 을 돌려주는 최소 핸들러."""

    server_version = "TeamBotHealth/1.0"

    def _respond(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server 규약)
        if self.path.split("?", 1)[0] in HEALTH_PATHS:
            self._respond(json.dumps({"status": "ok", "bot": "team-splitter"}).encode("utf-8"))
        else:
            self._respond(b'{"status":"not found"}', status=404)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def log_message(self, fmt: str, *args) -> None:  # 기본 액세스 로그는 조용히
        LOG.debug("health: " + fmt, *args)


def start_health_server(port: int) -> Optional[ThreadingHTTPServer]:
    """데몬 스레드로 헬스체크 서버를 띄웁니다. 실패해도 봇은 계속 돌아갑니다."""
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    except OSError as exc:
        LOG.warning("헬스체크 서버를 열지 못했어요(포트 %d): %s", port, exc)
        return None

    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="health-server", daemon=True).start()
    LOG.info("헬스체크 서버 시작: http://0.0.0.0:%d/ (경로: / 또는 /health)", port)
    return server


# ---------------------------------------------------------------------------
# 결과 버튼
# ---------------------------------------------------------------------------


class SplitResultView(discord.ui.View):
    """팀 나누기 결과에 붙는 '다시 섞기' / '대기방으로 모으기' 버튼."""

    def __init__(
        self,
        *,
        author_id: int,
        guild: discord.Guild,
        lobby: discord.VoiceChannel,
        team1_channel: discord.VoiceChannel,
        team2_channel: discord.VoiceChannel,
        participants: Sequence[discord.Member],
        timeout: float = BUTTON_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.guild = guild
        self.lobby_id = lobby.id
        self.team1_id = team1_channel.id
        self.team2_id = team2_channel.id
        self.participant_ids: list[int] = [member.id for member in participants]
        self.message: Optional[discord.Message] = None
        self._lock = asyncio.Lock()

    # -- 권한 ---------------------------------------------------------------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions is not None and permissions.move_members:
            return True
        await interaction.response.send_message(
            "명령을 실행한 사람이나 **'멤버 이동'** 권한이 있는 사람만 사용할 수 있어요.",
            ephemeral=True,
        )
        return False

    # -- 도우미 -------------------------------------------------------------
    def _resolve(self, channel_id: int, label: str) -> discord.VoiceChannel:
        channel = self.guild.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise TeamBotError(f"{label} 음성 채널이 사라졌어요. 채널을 다시 확인해 주세요.")
        return channel

    def _team_channels(self) -> tuple[discord.VoiceChannel, discord.VoiceChannel]:
        return self._resolve(self.team1_id, "팀 1"), self._resolve(self.team2_id, "팀 2")

    def _current_members(self, channels: Sequence[discord.VoiceChannel]) -> list[discord.Member]:
        """지금 두 팀 채널에 있는 사람들(봇 제외). 부족하면 원래 참가자로 보완."""
        found: dict[int, discord.Member] = {}
        for channel in channels:
            for member in channel.members:
                if not member.bot:
                    found[member.id] = member
        if len(found) >= 2:
            return list(found.values())

        for member_id in self.participant_ids:
            member = self.guild.get_member(member_id)
            if member is not None and not member.bot and member.voice and member.voice.channel:
                found[member.id] = member
        return list(found.values())

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    # -- 버튼: 다시 섞기 ----------------------------------------------------
    @discord.ui.button(label="다시 섞기", emoji="🔀", style=discord.ButtonStyle.primary)
    async def reshuffle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._lock.locked():
            await interaction.response.send_message("지금 이동 중이에요. 잠시만 기다려 주세요.", ephemeral=True)
            return

        async with self._lock:
            await interaction.response.defer()
            try:
                team1_channel, team2_channel = self._team_channels()
                ensure_can_move(self.guild, team1_channel, team2_channel)

                members = self._current_members((team1_channel, team2_channel))
                if len(members) < 2:
                    raise TeamBotError(
                        "두 팀 채널에 모인 사람이 2명보다 적어 다시 섞을 수 없어요.\n"
                        "먼저 대기방에 모인 뒤 `/팀나누기` 를 다시 실행해 주세요."
                    )

                team1, team2 = split_two(members)
                moved1, failed1 = await move_members(team1, team1_channel, reason="팀 나누기 봇: 다시 섞기")
                moved2, failed2 = await move_members(team2, team2_channel, reason="팀 나누기 봇: 다시 섞기")

                self.participant_ids = [member.id for member in members]
                all_failed = failed1 + failed2
                embed = build_result_embed(
                    moved1,
                    moved2,
                    title="🔀 다시 섞은 결과",
                    description="같은 인원을 다시 무작위로 섞어 이동시켰어요.",
                    footer_extra=f"실패 {len(all_failed)}명" if all_failed else "",
                )
                await interaction.edit_original_response(embed=embed, view=self)
            except TeamBotError as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            except Exception:
                LOG.exception("다시 섞기 처리 중 오류")
                await interaction.followup.send(
                    "다시 섞는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.", ephemeral=True
                )
                return

        if all_failed:
            await interaction.followup.send(
                f"⚠️ {len(all_failed)}명은 이동하지 못했어요.\n{_failure_lines(all_failed)}",
                ephemeral=True,
            )

    # -- 버튼: 대기방으로 모으기 --------------------------------------------
    @discord.ui.button(label="대기방으로 모으기", emoji="📢", style=discord.ButtonStyle.secondary)
    async def gather_back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._lock.locked():
            await interaction.response.send_message("지금 이동 중이에요. 잠시만 기다려 주세요.", ephemeral=True)
            return

        async with self._lock:
            await interaction.response.defer()
            try:
                team1_channel, team2_channel = self._team_channels()
                lobby = self._resolve(self.lobby_id, "대기방")
                ensure_can_move(self.guild, lobby, team1_channel, team2_channel)

                members = self._current_members((team1_channel, team2_channel))
                if not members:
                    raise TeamBotError("두 팀 채널에 모인 사람이 없어요.")

                moved, failed = await move_members(members, lobby, reason="팀 나누기 봇: 대기방으로 복귀")

                embed = discord.Embed(
                    title="📢 모두 대기방으로 모았어요",
                    description=f"{len(moved)}명을 **{lobby.name}** 채널로 옮겼어요.",
                    colour=discord.Colour.green(),
                )
                if failed:
                    embed.add_field(
                        name=f"⚠️ 이동 실패 {len(failed)}명",
                        value=_failure_lines(failed)[:1024],
                        inline=False,
                    )
                embed.set_footer(text="다시 하려면 /팀나누기 를 실행해 주세요.")
                for child in self.children:
                    if isinstance(child, discord.ui.Button) and child is not button:
                        child.disabled = True
                button.disabled = True
                await interaction.edit_original_response(embed=embed, view=self)
                self.stop()
            except TeamBotError as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            except Exception:
                LOG.exception("대기방으로 모으기 처리 중 오류")
                await interaction.followup.send(
                    "대기방으로 모으는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.", ephemeral=True
                )
                return

        if failed:
            await interaction.followup.send(
                f"⚠️ {len(failed)}명은 이동하지 못했어요.\n{_failure_lines(failed)}", ephemeral=True
            )


# ---------------------------------------------------------------------------
# 봇 본체
# ---------------------------------------------------------------------------


class TeamBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True   # 누가 어느 음성 채널에 있는지 확인
        intents.members = True        # 멤버 목록·이동(권한이 켜져 있어야 동작)
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

    async def setup_hook(self) -> None:
        # 슬래시 명령어를 디스코드에 등록합니다.
        await self.tree.sync()
        LOG.info("슬래시 명령어 전역 동기화 완료")

        # GUILD_ID 를 넣었다면 그 서버에 즉시 등록해서 기다리지 않아도 되게 합니다.
        if GUILD_ID is not None:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            LOG.info("서버(%s)에 슬래시 명령어를 즉시 등록했어요", GUILD_ID)

    async def on_ready(self) -> None:
        LOG.info("로그인 완료: %s (서버 %d곳)", self.user, len(self.guilds))

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """슬래시 명령어에서 난 오류를 한국어 안내로 바꿔 조용히 알려 줍니다."""
        original: BaseException = error
        if isinstance(error, app_commands.CommandInvokeError):
            original = error.original

        if isinstance(original, TeamBotError):
            message = str(original)
        elif isinstance(error, app_commands.TransformerError):
            message = "채널을 찾지 못했어요. 목록에서 음성 채널을 다시 골라 주세요."
        elif isinstance(error, app_commands.MissingPermissions):
            message = "이 명령을 쓰려면 **'멤버 이동'** 권한이 필요해요."
        elif isinstance(error, app_commands.BotMissingPermissions):
            message = "봇에게 필요한 권한이 없어요. 봇 역할에 **'멤버 이동'** 권한을 켜 주세요."
        else:
            LOG.exception("슬래시 명령어 오류: %s", error, exc_info=original)
            message = "처리 중 예상치 못한 문제가 생겼어요. 잠시 후 다시 시도해 주세요."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            LOG.warning("오류 안내 메시지를 보내지 못했어요")


bot = TeamBot()


# ---------------------------------------------------------------------------
# /팀나누기
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="팀나누기",
    description="대기방에 모인 인원을 무작위로 두 팀으로 나눠 이동시켜요.",
)
@app_commands.guild_only()   # DM 목록에는 뜨지 않게
@app_commands.describe(
    팀1_채널="팀 1이 이동할 음성 채널",
    팀2_채널="팀 2가 이동할 음성 채널",
)
@app_commands.rename(팀1_채널="팀1_채널", 팀2_채널="팀2_채널")
async def split_teams(
    interaction: discord.Interaction,
    팀1_채널: discord.VoiceChannel,
    팀2_채널: discord.VoiceChannel,
) -> None:
    guild = interaction.guild
    if guild is None:
        raise TeamBotError("서버 안에서만 사용할 수 있는 명령어예요.")

    user = interaction.user
    voice_state = getattr(user, "voice", None)          # 서버 밖(DM)에서는 None
    lobby = voice_state.channel if voice_state is not None else None
    if lobby is None:
        raise TeamBotError(
            "먼저 **대기방 음성 채널**에 들어간 뒤 다시 실행해 주세요.\n"
            "지금 들어 있는 음성 채널의 인원을 두 팀으로 나눕니다."
        )

    if 팀1_채널.id == 팀2_채널.id:
        raise TeamBotError("팀 1 채널과 팀 2 채널이 같아요. 서로 다른 음성 채널을 골라 주세요.")
    if lobby.id in (팀1_채널.id, 팀2_채널.id):
        raise TeamBotError(
            f"지금 계신 **{lobby.name}** 채널이 팀 채널로 지정됐어요.\n"
            "대기방에 모인 뒤, 팀 채널은 대기방과 다른 채널로 골라 주세요."
        )

    participants = human_voice_members(lobby)
    if len(participants) < 2:
        raise TeamBotError(
            f"**{lobby.name}** 채널에 봇을 제외한 인원이 {len(participants)}명뿐이에요.\n"
            "2명 이상 모여야 팀을 나눌 수 있어요."
        )

    ensure_can_move(guild, lobby, 팀1_채널, 팀2_채널)
    await interaction.response.defer()

    team1, team2 = split_two(participants)
    moved1, failed1 = await move_members(team1, 팀1_채널)
    moved2, failed2 = await move_members(team2, 팀2_채널)
    all_failed = failed1 + failed2

    embed = build_result_embed(
        moved1,
        moved2,
        description=f"**{lobby.name}** 에 모인 {len(participants)}명을 무작위로 나눠 이동시켰어요.",
        footer_extra=f"실패 {len(all_failed)}명" if all_failed else "",
    )
    view = SplitResultView(
        author_id=user.id,
        guild=guild,
        lobby=lobby,
        team1_channel=팀1_채널,
        team2_channel=팀2_채널,
        participants=participants,
    )
    message = await interaction.followup.send(embed=embed, view=view)
    view.message = message

    if all_failed:
        await interaction.followup.send(
            f"⚠️ {len(all_failed)}명은 채널로 옮기지 못했어요.\n{_failure_lines(all_failed)}",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# /모이기
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="모이기",
    description="팀 채널에 흩어진 인원을 한 채널로 다시 모아요.",
)
@app_commands.guild_only()
@app_commands.describe(
    대상_채널1="인원을 모을 첫 번째 음성 채널",
    모일_채널="모일 목적지 채널 (비워 두면 지금 계신 음성 채널)",
    대상_채널2="인원을 모을 두 번째 음성 채널 (선택)",
)
@app_commands.rename(대상_채널1="대상_채널1", 대상_채널2="대상_채널2", 모일_채널="모일_채널")
async def gather_members(
    interaction: discord.Interaction,
    대상_채널1: discord.VoiceChannel,
    모일_채널: Optional[discord.VoiceChannel] = None,
    대상_채널2: Optional[discord.VoiceChannel] = None,
) -> None:
    guild = interaction.guild
    if guild is None:
        raise TeamBotError("서버 안에서만 사용할 수 있는 명령어예요.")

    user = interaction.user
    voice_state = getattr(user, "voice", None)
    user_channel = voice_state.channel if voice_state is not None else None
    destination = 모일_채널 or user_channel
    if destination is None:
        raise TeamBotError(
            "모일 채널을 지정하지 않았고, 지금 음성 채널에도 들어 있지 않아요.\n"
            "`모일_채널` 을 고르거나 음성 채널에 들어간 뒤 다시 실행해 주세요."
        )

    sources: list[discord.VoiceChannel] = []
    for channel in (대상_채널1, 대상_채널2):
        if channel is not None and channel.id != destination.id and channel.id not in {c.id for c in sources}:
            sources.append(channel)
    if not sources:
        raise TeamBotError(f"옮겨 올 채널이 **{destination.name}** 하나뿐이라 모을게 없어요.")

    members: dict[int, discord.Member] = {}
    for channel in sources:
        for member in channel.members:
            if not member.bot and (member.voice is None or member.voice.channel is None or member.voice.channel.id != destination.id):
                members[member.id] = member

    if not members:
        names = ", ".join(f"**{channel.name}**" for channel in sources)
        raise TeamBotError(f"{names} 에 봇을 제외한 인원이 없어요.")

    ensure_can_move(guild, destination, *sources)
    await interaction.response.defer()

    moved, failed = await move_members(members.values(), destination, reason="팀 나누기 봇: 모이기")

    embed = discord.Embed(
        title="📢 한 채널로 모았어요",
        description=f"{len(moved)}명을 **{destination.name}** 채널로 옮겼어요.",
        colour=discord.Colour.green(),
    )
    embed.add_field(name="🟦 이동한 사람", value=_field_value(moved), inline=False)
    if failed:
        embed.add_field(
            name=f"⚠️ 이동 실패 {len(failed)}명",
            value=_failure_lines(failed)[:1024],
            inline=False,
        )
    embed.set_footer(text="다시 나누려면 /팀나누기 를 실행해 주세요.")
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not TOKEN or TOKEN == PLACEHOLDER_TOKEN:
        print(
            "❌ 봇 토큰이 없어요.\n"
            "   1) 같은 폴더의 `.env.example` 을 `.env` 로 복사\n"
            "   2) `.env` 안의 DISCORD_BOT_TOKEN= 뒤에 봇 토큰을 붙여 넣기\n"
            "   3) 다시 `python bot.py` 실행"
        )
        raise SystemExit(1)

    port = health_port()
    if port is not None:
        start_health_server(port)

    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        print("❌ 봇 토큰이 올바르지 않아요. 디스코드 개발자 포털에서 토큰을 다시 복사해 주세요.")
        raise SystemExit(1)
    except discord.PrivilegedIntentsRequired:
        print(
            "❌ 'Server Members Intent' 가 꺼져 있어요.\n"
            "   디스코드 개발자 포털 → Bot → Privileged Gateway Intents 에서\n"
            "   SERVER MEMBERS INTENT 를 켠 뒤 다시 실행해 주세요."
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
