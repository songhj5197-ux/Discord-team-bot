"""디스코드 내전 팀 나누기 봇.

대기방(로비) 음성 채널에 모인 멤버를 두 팀으로 나누고,
미리 만들어 둔 두 개의 음성 채널로 한 번에 이동시켜 줍니다.
팀은 점수(티어)를 고려해 최대한 균형 있게 나눌 수 있고,
경기 결과를 버튼으로 정산해 점수·승패를 기록합니다.

명령어
    /팀나누기  팀1_채널 팀2_채널 [방식]   → 대기방 인원을 두 팀으로 나눠 이동
                                          방식: 밸런스(기본) / 무작위
    /모이기    대상_채널1 [모일_채널] [대상_채널2] → 흩어진 인원을 한 채널로 모음
    /점수설정  멤버 점수                → 멤버 점수 수동 설정 (0~5000)
    /점수초기화 [멤버]                  → 멤버(또는 서버 전체) 점수를 1000점으로
    /랭킹                              → 서버 점수 순위표
    /기록채널  채널                     → 점수 백업용 텍스트 채널 지정
    /투표      제목 보기 [중복선택]      → 내전 날짜·시간 투표 (기본: 여러 보기 중복 선택)

결과 메시지의 버튼
    🔀 다시 섞기          같은 인원을 다시 섞어 이동 (새 판으로 리셋, 라운드 스코어 0:0)
    🏆 1팀 승리         1팀 승리로 그 라운드를 정산 (누를 때마다 라운드가 쌓임)
    🏆 2팀 승리         2팀 승리로 그 라운드를 정산 (누를 때마다 라운드가 쌓임)
    ↩️ 방금 정산 취소     마지막으로 정산한 라운드 1회분을 되돌림
    📢 대기방으로 모으기   두 팀 채널의 인원을 처음 대기방으로 복귀

투표
    /투표 로 만든 메시지에는 보기별 버튼이 붙고, 누를 때마다 표가 토글(추가/취소)
    됩니다. 버튼 라벨에 득표수(예: `9시 (3)`)가 함께 보이고, 임베드에는 보기별
    인원수와 투표한 사람 이름이 실시간으로 표시됩니다(멘션 알림 없이 이름만).
    기본은 중복 선택 허용이고 `중복선택:아니오` 로 1인 1표 투표도 만들 수 있습니다.
    만든 사람(또는 '멤버 이동' 권한자)이 🔒 투표 마감 을 누를 수 있고, 마감하면
    최다 득표 보기를 🏆 로 강조한 뒤 버튼을 잠깁니다. 버튼은 7일 동안 유효합니다.

점수
    새 멤버는 1000점에서 시작하고, 승리 시 +15~+35점, 패배 시 -15~-30점이
    반영됩니다(Elo 방식, 점수 차이가 클수록 변동 폭이 커짐). 점수는 0점 아래로
    내려가지 않습니다. 티어는 아이언 ~ 챌린저까지 8단계입니다.

저장
    data/scores.json 에 길드별 점수를 저장하고, /기록채널 로 지정한 채널
    (또는 이름이 '소현봇-기록' 인 채널)에 JSON 백업 메시지를 남깁니다.
    봇이 새로 켜졌을 때 그 채널에서 점수를 복구합니다.

실행 방법은 같은 폴더의 README.md 를 참고하세요.

헬스체크 서버 (무료 클라우드 호스팅용, 선택)
    PORT 환경변수가 있거나 ENABLE_HEALTH_SERVER 를 켜면
    http://0.0.0.0:<포트>/health 로 200 OK 를 돌려주는 아주 가벼운 서버가
    백그라운드 스레드로 함께 뜹니다. (Render·Koyeb·Cloudtype 같은
    웹 서비스 타입 호스팅이 '포트를 안 열었다'며 컨테이너를 죽이지 않게)
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import math
import os
import random
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable, Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()

# (선택) 서버 ID. 넣지 않아도 봇이 접속한 모든 서버에 슬래시 명령어를
# 곧바로(길드 명령어로) 등록합니다. 전역 명령어는 캐시 때문에 최대 1시간이
# 걸리고 같은 이름이 두 개씩 보일 수 있어, 길드 명령어만 사용합니다.
GUILD_ID_RAW = os.getenv("DISCORD_GUILD_ID", "").strip()
GUILD_ID: Optional[int] = int(GUILD_ID_RAW) if GUILD_ID_RAW.isdigit() else None

BUTTON_TIMEOUT = 60 * 60 * 3      # 결과 버튼을 누를 수 있는 시간(초) — 4맵 내전도 넉넉하게
POLL_BUTTON_TIMEOUT = 60 * 60 * 24 * 7   # 투표 버튼 유효시간(초) — 며칠 전부터 열어 둘 수 있게 7일
POLL_MIN_OPTIONS = 2              # 보기 최소 개수
POLL_MAX_OPTIONS = 20             # 보기 최대 개수 (5줄 × 5개에서 마감 버튼 자리 확보)
POLL_LABEL_LIMIT = 80             # 디스코드 버튼 라벨 최대 글자 수
POLL_TITLE_LIMIT = 200            # 투표 제목 최대 글자 수 (임베드 제목 256자 한도)
POLL_ID_PREFIX = "team_bot:poll:" # 버튼 custom_id 앞부분 (최대 100자)
MOVE_REASON = "팀 나누기 봇: 팀 이동"
PLACEHOLDER_TOKEN = "여기에_봇_토큰_입력"

DEFAULT_HEALTH_PORT = 8080        # PORT 없이 ENABLE_HEALTH_SERVER 만 켰을 때
HEALTH_PATHS = {"/", "/health", "/healthz"}

# -- 점수 / 티어 -------------------------------------------------------------
DEFAULT_SCORE = 1000              # 새 멤버 기본 점수
MIN_SCORE = 0                     # 점수 하한
MAX_SET_SCORE = 5000              # /점수설정 상한
BALANCE_TOLERANCE = 12.0          # 팀 평균 점수차 허용 폭 (후보를 고를 때)

# (최소 점수, 이모지, 티어 이름) — 위에서부터 확인
TIER_TABLE: tuple[tuple[int, str, str], ...] = (
    (1500, "👑", "챌린저"),
    (1350, "💎", "다이아"),
    (1200, "💠", "에메랄드"),
    (1100, "🔷", "플래티넘"),
    (1000, "🥇", "골드"),
    (900, "🥈", "실버"),
    (800, "🥉", "브론즈"),
    (0, "🪨", "아이언"),
)

MODE_BALANCE = "balance"
MODE_RANDOM = "random"
MODE_LABELS = {MODE_BALANCE: "밸런스", MODE_RANDOM: "무작위"}

# -- 저장 / 백업 -------------------------------------------------------------
SCORE_FILE = os.getenv("SCORE_FILE", "").strip() or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "scores.json"
)
BACKUP_CHANNEL_NAME = "소현봇-기록"
BACKUP_HEADER = "<!-- team-bot-scores:v1 p{index}/{total} -->"
BACKUP_MARKER_RE = re.compile(r"<!--\s*team-bot-scores:v1\s+p(\d+)/(\d+)\s*-->")
BACKUP_CODE_RE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", re.DOTALL)
BACKUP_MESSAGE_LIMIT = 2000       # 디스코드 메시지 최대 길이
BACKUP_CHUNK_SIZE = 1700          # 한 메시지에 담는 JSON 조각 길이
BACKUP_HISTORY_LIMIT = 100        # 채널 기록에서 백업 메시지를 찾을 때 훑는 개수
BACKUP_EXTRA_CHANNELS = 8         # 이름이 다른 채널까지 훑을 때 최대 개수

LOG = logging.getLogger("team_bot")


# ---------------------------------------------------------------------------
# 오류 타입
# ---------------------------------------------------------------------------


class TeamBotError(commands.CommandError):
    """사용자에게 그대로 보여 줄 한국어 안내가 담긴 오류."""


# ---------------------------------------------------------------------------
# 티어 · 점수 계산
# ---------------------------------------------------------------------------


def tier_for(score: int) -> tuple[str, str]:
    """점수에 해당하는 (이모지, 티어 이름)."""
    for threshold, emoji, name in TIER_TABLE:
        if score >= threshold:
            return emoji, name
    return TIER_TABLE[-1][1], TIER_TABLE[-1][2]


def tier_label(score: int) -> str:
    """'👑 챌린저' 처럼 이모지와 티어 이름을 붙인 문자열."""
    emoji, name = tier_for(score)
    return f"{emoji} {name}"


def elo_deltas(winner_avg: float, loser_avg: float) -> tuple[int, int]:
    """두 팀 평균 점수로 승리 팀 상승폭과 패배 팀 하락폭을 정합니다.

    * 승리: +15 ~ +35 (동점 +25)
    * 패배: -15 ~ -30 (동점 -20)
    * 점수가 낮은 팀이 이기면(업셋) 더 많이 오르고, 진 팀은 적게 잃습니다.
    """
    diff = loser_avg - winner_avg                    # +면 승리 팀이 약체
    ratio = max(-1.0, min(1.0, diff / 150.0))
    gain = int(round(25 + 10 * ratio))               # 15 ~ 35
    gain = max(15, min(35, gain))
    loss = int(round(20 - 10 * ratio))               # 10 ~ 30 → 15 ~ 30 로 보정
    loss = max(15, min(30, loss))
    return gain, loss


# ---------------------------------------------------------------------------
# 팀 나누기
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Player:
    """점수 계산·표시에 쓰는 참가자 정보."""

    member_id: int
    name: str
    score: int

    @property
    def tier(self) -> str:
        return tier_label(self.score)

    def line(self) -> str:
        return f"{tier_label(self.score)} · {self.score}점 · {self.name}"


def team_average(team: Sequence[Player]) -> float:
    return sum(player.score for player in team) / len(team) if team else 0.0


def _greedy_split(scores: Sequence[int], k: int, m: int, rng: random.Random) -> tuple[list[int], list[int], int]:
    """점수 큰 사람부터 낮은 쪽 팀에 넣는 휴리스틱 + 교환 개선."""
    order = list(range(len(scores)))
    rng.shuffle(order)
    order.sort(key=lambda index: scores[index], reverse=True)

    team1: list[int] = []
    team2: list[int] = []
    sum1 = sum2 = 0
    for index in order:
        if len(team1) < k and (len(team2) >= m or sum1 <= sum2):
            team1.append(index)
            sum1 += scores[index]
        else:
            team2.append(index)
            sum2 += scores[index]

    # 두 팀에서 한 명씩 바꿔 점수차를 줄일 수 있으면 바꿉니다.
    for _ in range(80):
        best_delta = 0
        best_pair: Optional[tuple[int, int]] = None
        current = abs(sum1 - sum2)
        for a in team1:
            for b in team2:
                delta = scores[b] - scores[a]                # team1 합 변화량
                improvement = current - abs(sum1 + delta - (sum2 - delta))
                if improvement > best_delta:
                    best_delta = improvement
                    best_pair = (a, b)
        if best_pair is None:
            break
        a, b = best_pair
        team1.remove(a)
        team2.remove(b)
        team1.append(b)
        team2.append(a)
        sum1 += scores[b] - scores[a]
        sum2 += scores[a] - scores[b]

    return team1, team2, sum1


def balanced_split(
    players: Sequence[Player],
    *,
    rng: Optional[random.Random] = None,
    tolerance: float = BALANCE_TOLERANCE,
) -> tuple[list[Player], list[Player]]:
    """인원수를 맞추면서 두 팀 평균 점수차가 최소가 되는 조합을 고릅니다.

    최소 점수차와 비슷한(같은) 후보들 중 하나를 무작위로 골라,
    같은 인원·같은 점수라도 매번 똑같은 팀이 나오지 않게 합니다.
    """
    rng = rng or random
    pool = list(players)
    n = len(pool)
    if n < 2:
        return pool, []
    k = math.ceil(n / 2)
    m = n - k
    scores = [player.score for player in pool]
    total = sum(scores)

    def avg_diff(sum1: int) -> float:
        avg1 = sum1 / k
        avg2 = (total - sum1) / m if m else 0.0
        return abs(avg1 - avg2)

    best = math.inf
    candidates: list[tuple[tuple[int, ...], float]] = []

    def remember(team1: Sequence[int], diff: float) -> None:
        nonlocal best
        candidates.append((tuple(sorted(team1)), diff))
        if diff < best:
            best = diff
        if len(candidates) > 4096:
            candidates.sort(key=lambda item: item[1])
            del candidates[1024:]

    if n <= 18:
        # 인원이 적을 때는 모든 조합을 확인합니다. (10명이면 252가지)
        for combo in itertools.combinations(range(n), k):
            remember(combo, avg_diff(sum(scores[i] for i in combo)))
    else:
        # 인원이 많을 때는 무작위 휴리스틱을 여러 번 돌립니다.
        for _ in range(240):
            team1, _team2, sum1 = _greedy_split(scores, k, m, rng)
            remember(team1, avg_diff(sum1))

    limit = best + tolerance + 1e-9
    pool_of_choices = [combo for combo, diff in candidates if diff <= limit]
    if not pool_of_choices:
        pool_of_choices = [min(candidates, key=lambda item: item[1])[0]]

    chosen = set(rng.choice(pool_of_choices))
    team1 = [pool[index] for index in range(n) if index in chosen]
    team2 = [pool[index] for index in range(n) if index not in chosen]
    rng.shuffle(team1)
    rng.shuffle(team2)
    return team1, team2


def random_split(players: Sequence[Player], *, rng: Optional[random.Random] = None) -> tuple[list[Player], list[Player]]:
    """인원만 반반으로 나누는 완전 무작위 분배 (홀수면 팀 1이 한 명 더)."""
    rng = rng or random
    pool = list(players)
    rng.shuffle(pool)
    half = math.ceil(len(pool) / 2)
    return pool[:half], pool[half:]


def split_players(players: Sequence[Player], mode: str) -> tuple[list[Player], list[Player]]:
    if mode == MODE_RANDOM:
        return random_split(players)
    return balanced_split(players)


# ---------------------------------------------------------------------------
# 공용 도우미
# ---------------------------------------------------------------------------


def human_voice_members(channel: Optional[discord.VoiceChannel]) -> list[discord.Member]:
    """음성 채널에 있는 사람(봇 제외) 목록. 채널이 없으면 빈 목록."""
    if channel is None:
        return []
    return [member for member in channel.members if not member.bot]


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


def ensure_score_manager(interaction: discord.Interaction) -> None:
    """점수를 바꾸는 명령은 '멤버 이동' 또는 '서버 관리' 권한이 필요합니다."""
    permissions = getattr(interaction.user, "guild_permissions", None)
    if permissions is not None and (permissions.move_members or permissions.manage_guild):
        return
    raise TeamBotError(
        "점수를 바꾸는 명령은 **'멤버 이동'** 또는 **'서버 관리'** 권한이 있는 사람만 쓸 수 있어요."
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


def _team_field(team: Sequence[Player], limit: int = 1000) -> str:
    """팀 명단(티어·점수·이름)을 임베드 필드 길이에 맞춰 만듭니다."""
    if not team:
        return "—"
    lines: list[str] = []
    used = 0
    for player in team:
        line = player.line()
        if used + len(line) + 1 > limit:
            break
        lines.append(line)
        used += len(line) + 1
    hidden = len(team) - len(lines)
    text = "\n".join(lines)
    if hidden > 0:
        text += f"\n… 외 {hidden}명"
    return text


def _settlement_lines(changes: Sequence["ScoreChange"], limit: int = 1000) -> str:
    if not changes:
        return "—"
    lines: list[str] = []
    used = 0
    for change in changes:
        line = f"{change.name} {change.before} → {change.after} ({change.delta:+d})"
        if used + len(line) + 1 > limit:
            break
        lines.append(line)
        used += len(line) + 1
    hidden = len(changes) - len(lines)
    text = "\n".join(lines)
    if hidden > 0:
        text += f"\n… 외 {hidden}명"
    return text


def _failure_lines(failed: Sequence[tuple[discord.Member, str]]) -> str:
    return "\n".join(f"• {member.display_name}: {reason}" for member, reason in failed)


def build_split_embed(
    team1: Sequence[Player],
    team2: Sequence[Player],
    *,
    mode: str = MODE_BALANCE,
    title: str = "⚖️ 팀 나누기 결과",
    description: str = "아래처럼 팀을 나눠 각 채널로 이동시켰어요.",
    failed: int = 0,
    settlement: Optional["Settlement"] = None,
    winner_number: int = 0,
    rounds: Sequence["RoundRecord"] = (),
) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, colour=discord.Colour.blurple())
    avg1 = team_average(team1)
    avg2 = team_average(team2)
    embed.add_field(
        name=f"🟦 팀 1 · {len(team1)}명 · 평균 {avg1:.0f}점",
        value=_team_field(team1),
        inline=True,
    )
    embed.add_field(
        name=f"🟥 팀 2 · {len(team2)}명 · 평균 {avg2:.0f}점",
        value=_team_field(team2),
        inline=True,
    )

    if settlement is not None:
        winner_label = f"{winner_number}팀"
        round_label = f" · {len(rounds)}라운드" if rounds else ""
        embed.add_field(
            name=f"🏆 {winner_label} 승리{round_label} · 정산 완료 (+{settlement.gain} / -{settlement.loss})",
            value=(
                f"__승리 팀 (+{settlement.gain})__\n{_settlement_lines(settlement.winners)}\n"
                f"__패배 팀 (-{settlement.loss})__\n{_settlement_lines(settlement.losers)}"
            )[:1024],
            inline=False,
        )

    if rounds:
        wins1 = sum(1 for entry in rounds if entry.winner_number == 1)
        wins2 = len(rounds) - wins1
        last = rounds[-1]
        embed.add_field(
            name=f"📊 현재 스코어 · {len(rounds)}라운드",
            value=(
                f"**1팀 {wins1}승** vs **2팀 {wins2}승**\n"
                f"최근 라운드 변동: 승리 +{last.settlement.gain} / 패배 -{last.settlement.loss}"
            ),
            inline=False,
        )

    footer = f"{MODE_LABELS.get(mode, mode)} 배정 · 총 {len(team1) + len(team2)}명 · 평균차 {abs(avg1 - avg2):.0f}점"
    if failed:
        footer += f" · 이동 실패 {failed}명"
    if rounds:
        footer += f" · {len(rounds)}라운드 정산 (1팀 {sum(1 for entry in rounds if entry.winner_number == 1)}승 vs 2팀 {sum(1 for entry in rounds if entry.winner_number == 2)}승)"
    elif settlement is not None:
        footer += " · 정산 완료"
    embed.set_footer(text=footer)
    return embed


def build_ranking_embed(guild_name: str, rows: Sequence[dict[str, Any]], *, total: int, limit: int = 25) -> discord.Embed:
    embed = discord.Embed(
        title=f"🏆 {guild_name} 랭킹",
        colour=discord.Colour.gold(),
    )
    if not rows:
        embed.description = "아직 기록된 점수가 없어요. `/팀나누기` 로 한 판 해보세요!"
        embed.set_footer(text=f"새 멤버는 {DEFAULT_SCORE}점부터 시작해요.")
        return embed

    lines: list[str] = []
    for index, row in enumerate(rows[:limit], start=1):
        played = row["wins"] + row["losses"]
        rate = f"{row['wins'] / played * 100:.0f}%" if played else "-"
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"{index}.")
        lines.append(
            f"{medal} **{row['name'] or '알 수 없음'}** · {tier_label(row['score'])} {row['score']}점 · "
            f"{row['wins']}승 {row['losses']}패 ({rate})"
        )
    embed.description = "\n".join(lines)
    footer = f"참가자 {total}명"
    if len(rows) > limit:
        footer += f" · 상위 {limit}명만 표시"
    embed.set_footer(text=footer)
    return embed


# ---------------------------------------------------------------------------
# 점수 저장소
# ---------------------------------------------------------------------------


def _clean_name(name: str) -> str:
    """백업 JSON·임베드를 깨뜨리지 않도록 이름을 정리합니다."""
    text = (name or "").replace("`", "'").replace("\n", " ").replace("\r", " ")
    return text.strip()[:40]


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class ScoreChange:
    user_id: int
    name: str
    before: int
    after: int
    tier_before: str
    tier_after: str

    @property
    def delta(self) -> int:
        return self.after - self.before


@dataclass
class Settlement:
    gain: int
    loss: int
    winners: list[ScoreChange] = field(default_factory=list)
    losers: list[ScoreChange] = field(default_factory=list)


@dataclass
class RoundRecord:
    """한 판에서 정산한 라운드 하나의 기록 (취소할 때 이 기록 그대로 되돌립니다)."""

    round_no: int
    winner_number: int
    settlement: Settlement


class RoundLog:
    """한 판의 라운드별 정산 기록.

    같은 팀 조합 그대로 라운드를 계속 쌓을 수 있고, 마지막 라운드 하나만
    되돌릴 수 있습니다. 🔀 다시 섞기 로 새 판을 시작하면 0:0 으로 비워집니다.
    """

    def __init__(self) -> None:
        self.rounds: list[RoundRecord] = []

    def __len__(self) -> int:
        return len(self.rounds)

    @property
    def count(self) -> int:
        return len(self.rounds)

    @property
    def latest(self) -> Optional[RoundRecord]:
        return self.rounds[-1] if self.rounds else None

    def record(self, winner_number: int, settlement: Settlement) -> RoundRecord:
        entry = RoundRecord(len(self.rounds) + 1, winner_number, settlement)
        self.rounds.append(entry)
        return entry

    def pop(self) -> Optional[RoundRecord]:
        return self.rounds.pop() if self.rounds else None

    def clear(self) -> None:
        self.rounds.clear()

    def wins(self, team_number: int) -> int:
        return sum(1 for entry in self.rounds if entry.winner_number == team_number)

    def score_line(self) -> str:
        """'1팀 2승 vs 2팀 1승' 형태의 현재 라운드 스코어."""
        return f"1팀 {self.wins(1)}승 vs 2팀 {self.wins(2)}승"


class ScoreStore:
    """길드별 멤버 점수/승/패를 JSON 파일 하나에 보관합니다."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": 1, "guilds": {}}
        self.load()

    # -- 파일 ---------------------------------------------------------------
    def load(self) -> bool:
        try:
            with open(self.path, encoding="utf-8") as fp:
                raw = json.load(fp)
        except FileNotFoundError:
            return False
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("점수 파일을 읽지 못했어요(%s): %s", self.path, exc)
            return False
        if isinstance(raw, dict) and isinstance(raw.get("guilds"), dict):
            with self._lock:
                self._data = {"version": 1, "guilds": raw["guilds"]}
            return True
        LOG.warning("점수 파일 형식이 올바르지 않아 무시했어요: %s", self.path)
        return False

    def save(self) -> bool:
        with self._lock:
            payload = json.dumps(self._data, ensure_ascii=False, separators=(",", ":"))
        try:
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            temporary = f"{self.path}.tmp"
            with open(temporary, "w", encoding="utf-8") as fp:
                fp.write(payload)
            os.replace(temporary, self.path)
        except OSError as exc:
            LOG.warning("점수 파일을 저장하지 못했어요(%s): %s", self.path, exc)
            return False
        return True

    # -- 길드/멤버 -----------------------------------------------------------
    def _guild(self, guild_id: int, *, create: bool = False) -> Optional[dict[str, Any]]:
        key = str(guild_id)
        guild = self._data["guilds"].get(key)
        if guild is None and create:
            guild = {"members": {}, "record_channel_id": None, "backup_message_ids": []}
            self._data["guilds"][key] = guild
        return guild

    def has_scores(self, guild_id: int) -> bool:
        with self._lock:
            guild = self._guild(guild_id)
            return bool(guild and guild.get("members"))

    def member_count(self, guild_id: int) -> int:
        with self._lock:
            guild = self._guild(guild_id)
            return len(guild.get("members", {})) if guild else 0

    def record_channel_id(self, guild_id: int) -> Optional[int]:
        with self._lock:
            guild = self._guild(guild_id)
            value = guild.get("record_channel_id") if guild else None
            return value if isinstance(value, int) else None

    def set_record_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        with self._lock:
            guild = self._guild(guild_id, create=True)
            guild["record_channel_id"] = channel_id

    def backup_message_ids(self, guild_id: int) -> list[int]:
        with self._lock:
            guild = self._guild(guild_id)
            ids = guild.get("backup_message_ids") if guild else []
            return [value for value in ids if isinstance(value, int)]

    def set_backup_message_ids(self, guild_id: int, message_ids: Sequence[int]) -> None:
        with self._lock:
            guild = self._guild(guild_id, create=True)
            guild["backup_message_ids"] = [int(value) for value in message_ids]

    def _ensure_member(self, guild: dict[str, Any], user_id: int, name: Optional[str] = None) -> dict[str, Any]:
        record = guild["members"].get(str(user_id))
        if record is None:
            record = {"score": DEFAULT_SCORE, "wins": 0, "losses": 0, "name": _clean_name(name or "")}
            guild["members"][str(user_id)] = record
        elif name:
            record["name"] = _clean_name(name)
        return record

    def ensure_player(self, guild_id: int, user_id: int, name: str) -> Player:
        """멤버를 등록(없으면 기본 점수)하고 Player 로 돌려줍니다."""
        with self._lock:
            guild = self._guild(guild_id, create=True)
            record = self._ensure_member(guild, user_id, name)
            return Player(member_id=user_id, name=_clean_name(name) or record.get("name", ""), score=int(record["score"]))

    def score_of(self, guild_id: int, user_id: int) -> int:
        with self._lock:
            guild = self._guild(guild_id)
            if not guild:
                return DEFAULT_SCORE
            record = guild["members"].get(str(user_id))
            return int(record["score"]) if record else DEFAULT_SCORE

    def set_score(self, guild_id: int, user_id: int, score: int, name: str = "") -> tuple[int, int]:
        with self._lock:
            guild = self._guild(guild_id, create=True)
            record = self._ensure_member(guild, user_id, name or None)
            before = int(record["score"])
            record["score"] = max(MIN_SCORE, min(MAX_SET_SCORE, int(score)))
            return before, int(record["score"])

    def reset_member(self, guild_id: int, user_id: int, name: str = "") -> Optional[int]:
        with self._lock:
            guild = self._guild(guild_id, create=True)
            record = self._ensure_member(guild, user_id, name or None)
            before = int(record["score"])
            record["score"] = DEFAULT_SCORE
            record["wins"] = 0
            record["losses"] = 0
            return before

    def reset_guild(self, guild_id: int) -> int:
        with self._lock:
            guild = self._guild(guild_id)
            if not guild:
                return 0
            count = len(guild.get("members", {}))
            guild["members"] = {}
            return count

    def settle(
        self,
        guild_id: int,
        winner_ids: Sequence[int],
        loser_ids: Sequence[int],
        *,
        names: Optional[dict[int, str]] = None,
    ) -> Settlement:
        """한 판의 승패를 점수에 반영합니다. (점수 하한 0점)"""
        names = names or {}
        with self._lock:
            guild = self._guild(guild_id, create=True)
            if not winner_ids or not loser_ids:
                raise TeamBotError("정산할 팀 인원이 없어요. 다시 팀을 나눠 주세요.")

            winner_records = [self._ensure_member(guild, int(uid), names.get(int(uid))) for uid in winner_ids]
            loser_records = [self._ensure_member(guild, int(uid), names.get(int(uid))) for uid in loser_ids]

            winner_avg = sum(int(record["score"]) for record in winner_records) / len(winner_records)
            loser_avg = sum(int(record["score"]) for record in loser_records) / len(loser_records)
            gain, loss = elo_deltas(winner_avg, loser_avg)

            winners: list[ScoreChange] = []
            for user_id, record in zip(winner_ids, winner_records):
                before = int(record["score"])
                after = max(MIN_SCORE, before + gain)
                record["score"] = after
                record["wins"] = int(record.get("wins", 0)) + 1
                winners.append(
                    ScoreChange(int(user_id), record.get("name", ""), before, after, tier_label(before), tier_label(after))
                )

            losers: list[ScoreChange] = []
            for user_id, record in zip(loser_ids, loser_records):
                before = int(record["score"])
                after = max(MIN_SCORE, before - loss)
                record["score"] = after
                record["losses"] = int(record.get("losses", 0)) + 1
                losers.append(
                    ScoreChange(int(user_id), record.get("name", ""), before, after, tier_label(before), tier_label(after))
                )

            return Settlement(gain=gain, loss=loss, winners=winners, losers=losers)

    def revert_settlement(
        self,
        guild_id: int,
        winners: Sequence[ScoreChange],
        losers: Sequence[ScoreChange],
    ) -> tuple[list[ScoreChange], list[ScoreChange]]:
        """마지막 라운드 정산을 정확히 되돌립니다 (점수 역적용 + 승/패 -1).

        정산 때 기록한 변동량(delta)을 그대로 빼므로, 0점 하한에 걸려 실제로는
        오르지 못한 점수도 원래 자리로 돌아갑니다.
        """
        with self._lock:
            guild = self._guild(guild_id, create=True)

            def rollback(changes: Sequence[ScoreChange], counter: str) -> list[ScoreChange]:
                restored: list[ScoreChange] = []
                for change in changes:
                    record = guild["members"].get(str(change.user_id))
                    if record is None:
                        continue
                    before = int(record["score"])
                    after = max(MIN_SCORE, before - change.delta)
                    record["score"] = after
                    record[counter] = max(0, int(record.get(counter, 0)) - 1)
                    restored.append(
                        ScoreChange(
                            change.user_id,
                            record.get("name", ""),
                            before,
                            after,
                            tier_label(before),
                            tier_label(after),
                        )
                    )
                return restored

            return rollback(winners, "wins"), rollback(losers, "losses")

    def ranking(self, guild_id: int) -> list[dict[str, Any]]:
        with self._lock:
            guild = self._guild(guild_id)
            if not guild:
                return []
            rows = [
                {
                    "user_id": int(user_id),
                    "name": record.get("name", ""),
                    "score": int(record.get("score", DEFAULT_SCORE)),
                    "wins": int(record.get("wins", 0)),
                    "losses": int(record.get("losses", 0)),
                }
                for user_id, record in guild.get("members", {}).items()
                if str(user_id).isdigit()
            ]
        rows.sort(key=lambda row: (-row["score"], -row["wins"], row["name"]))
        return rows

    # -- 백업 ---------------------------------------------------------------
    def backup_payload(self, guild_id: int) -> dict[str, Any]:
        with self._lock:
            guild = self._guild(guild_id) or {"members": {}, "record_channel_id": None}
            return {
                "version": 1,
                "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "guild": {
                    "id": str(guild_id),
                    "record_channel_id": guild.get("record_channel_id"),
                    "members": guild.get("members", {}),
                },
            }

    def restore_from_payload(
        self,
        guild_id: int,
        payload: dict[str, Any],
        *,
        record_channel_id: Optional[int] = None,
        message_ids: Sequence[int] = (),
    ) -> int:
        """백업 JSON 으로 길드 점수를 되살립니다. 복구한 멤버 수를 돌려줍니다."""
        guild_part = payload.get("guild") if isinstance(payload, dict) else None
        raw_members = guild_part.get("members") if isinstance(guild_part, dict) else None
        if not isinstance(raw_members, dict):
            return 0

        members: dict[str, dict[str, Any]] = {}
        for user_id, record in raw_members.items():
            if not str(user_id).isdigit() or not isinstance(record, dict):
                continue
            score = max(MIN_SCORE, _to_int(record.get("score"), DEFAULT_SCORE))
            members[str(user_id)] = {
                "score": score,
                "wins": max(0, _to_int(record.get("wins"), 0)),
                "losses": max(0, _to_int(record.get("losses"), 0)),
                "name": _clean_name(str(record.get("name", ""))),
            }
        if not members:
            return 0

        with self._lock:
            channel_id = record_channel_id
            if channel_id is None:
                channel_id = _to_int(guild_part.get("record_channel_id"), 0) or None
            self._data["guilds"][str(guild_id)] = {
                "members": members,
                "record_channel_id": channel_id,
                "backup_message_ids": [int(value) for value in message_ids],
            }
        return len(members)


def backup_message_bodies(json_text: str) -> list[str]:
    """JSON 문자열을 디스코드 메시지 길이에 맞춰 여러 조각으로 나눕니다."""
    chunks = [json_text[index:index + BACKUP_CHUNK_SIZE] for index in range(0, len(json_text), BACKUP_CHUNK_SIZE)]
    if not chunks:
        chunks = [""]
    total = len(chunks)
    bodies = []
    for index, chunk in enumerate(chunks, start=1):
        header = BACKUP_HEADER.format(index=index, total=total)
        body = f"{header}\n```json\n{chunk}\n```"
        if len(body) > BACKUP_MESSAGE_LIMIT:          # 안전장치 (여기까지 오면 안 됨)
            chunk = chunk[: BACKUP_MESSAGE_LIMIT - len(header) - 12]
            body = f"{header}\n```json\n{chunk}\n```"
        bodies.append(body)
    return bodies


def parse_backup_message(content: str) -> Optional[tuple[int, int, str]]:
    """백업 메시지 하나에서 (조각 번호, 전체 조각 수, JSON 조각)을 꺼냅니다."""
    match = BACKUP_MARKER_RE.search(content or "")
    if not match:
        return None
    index, total = int(match.group(1)), int(match.group(2))
    if index < 1 or total < 1 or index > total:
        return None
    code = BACKUP_CODE_RE.search(content)
    if not code:
        return None
    return index, total, code.group(1)


def assemble_backup(contents: Iterable[str]) -> Optional[tuple[dict[str, Any], list[str]]]:
    """여러 백업 메시지에서 완전한 조각 묶음을 찾아 JSON 으로 되돌립니다."""
    groups: dict[int, dict[int, tuple[str, str]]] = {}
    for content in contents:
        parsed = parse_backup_message(content)
        if parsed is None:
            continue
        index, total, json_text = parsed
        groups.setdefault(total, {})[index] = (content, json_text)

    for total in sorted(groups, reverse=True):
        parts = groups[total]
        if all(index in parts for index in range(1, total + 1)):
            ordered = [parts[index] for index in range(1, total + 1)]
            text = "".join(chunk for _, chunk in ordered)
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("guild"), dict):
                return payload, [content for content, _ in ordered]
    return None


async def read_backup_channel(
    channel: discord.TextChannel, *, deep: bool = True
) -> tuple[Optional[dict[str, Any]], list[int]]:
    """텍스트 채널의 고정 메시지(와 필요하면 최근 기록)에서 점수 백업을 찾습니다."""
    contents: dict[int, str] = {}

    def remember(message: discord.Message) -> None:
        if message.content and BACKUP_MARKER_RE.search(message.content):
            contents[message.id] = message.content

    try:
        async for message in channel.pins():
            remember(message)
    except (discord.Forbidden, discord.HTTPException):
        pass

    if not contents and deep:
        try:
            async for message in channel.history(limit=BACKUP_HISTORY_LIMIT):
                remember(message)
        except (discord.Forbidden, discord.HTTPException):
            pass

    if not contents:
        return None, []

    assembled = assemble_backup(contents.values())
    if assembled is None:
        return None, []
    payload, used_contents = assembled
    content_to_ids: dict[str, list[int]] = {}
    for message_id, content in contents.items():
        content_to_ids.setdefault(content, []).append(message_id)
    message_ids: list[int] = []
    for content in used_contents:
        message_ids.extend(content_to_ids.get(content, []))
    return payload, message_ids


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
# 봇 본체
# ---------------------------------------------------------------------------


class TeamBot(commands.Bot):
    def __init__(self, store: Optional[ScoreStore] = None) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True   # 누가 어느 음성 채널에 있는지 확인
        intents.members = True        # 멤버 목록·이동(권한이 켜져 있어야 동작)
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.store = store or ScoreStore(SCORE_FILE)
        self._restore_tried: set[int] = set()
        # -- 슬래시 명령어 동기화 상태 --------------------------------------
        self._synced_guild_ids: set[int] = set()      # 명령어를 등록해 둔 서버
        self._global_commands_cleared = False          # 전역 목록을 비웠는지
        self._command_snapshot: list[Any] = []         # 전역 명령어 원본 (복사용)

    # -- 슬래시 명령어 -------------------------------------------------------
    async def setup_hook(self) -> None:
        """로그인 전 준비. 전역 목록을 기억해 두고, 서버 ID가 있으면 먼저 등록합니다.

        디스코드는 전역 명령어를 최대 1시간까지 캐시하기 때문에, 전역으로만
        등록하면 새 명령어가 늦게 보입니다. 그래서 봇이 접속한 모든 서버에
        '길드 명령어'로 복사해 즉시 보이게 하고, 전역 목록은 비워서 같은
        명령어가 두 개씩 뜨지 않게 합니다. 전역 목록을 비우면 메모리에서도
        사라지므로, 나중에 새로 초대된 서버에 다시 복사할 수 있도록 원본을
        따로 남겨 둡니다.
        """
        self._command_snapshot = list(self.tree.get_commands(guild=None))
        LOG.info(
            "슬래시 명령어 %d개 확인: %s",
            len(self._command_snapshot),
            ", ".join(command.name for command in self._command_snapshot) or "(없음)",
        )

        # DISCORD_GUILD_ID 를 넣었다면 로그인 전에도 그 서버에 먼저 등록해 둡니다.
        if GUILD_ID is not None:
            await self.register_commands_for_guild(discord.Object(id=GUILD_ID))

    async def register_commands_for_guild(self, guild: discord.abc.Snowflake) -> int:
        """한 서버에 슬래시 명령어를 곧바로 등록합니다. 등록된 개수를 돌려줍니다."""
        # 전역 목록을 비우는 중에 들어온 요청이면 복사할 원본이 없다고
        # 빈 목록을 등록(기존 명령어 삭제)하지 않도록 먼저 되돌려 둡니다.
        if not self.tree.get_commands(guild=None):
            self._restore_global_snapshot()

        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        self._synced_guild_ids.add(guild.id)
        LOG.info("서버(%s) 슬래시 명령어 %d개 등록 완료", guild.id, len(synced))
        return len(synced)

    def _restore_global_snapshot(self) -> None:
        """비워 둔 전역 명령어 원본을 메모리에 되돌립니다 (길드 복사용)."""
        for command in self._command_snapshot:
            try:
                self.tree.add_command(command)
            except app_commands.CommandAlreadyRegistered:
                pass

    async def sync_commands_to_guilds(self) -> int:
        """봇이 접속한 모든 서버에 슬래시 명령어를 즉시 등록합니다.

        모든 서버 등록이 끝난 뒤에야 전역 목록을 비웁니다(순서 중요).
        전역을 먼저 비우면 복사할 원본이 사라져 길드 등록이 불가능해집니다.
        """
        guilds = list(self.guilds)
        if not guilds and GUILD_ID is None:
            LOG.info("접속한 서버가 없어 길드 명령어 동기화를 건너뛰었어요")
            return 0

        synced_total = 0
        for guild in guilds:
            if guild.id in self._synced_guild_ids:
                continue
            try:
                await self.register_commands_for_guild(guild)
            except (discord.Forbidden, discord.HTTPException) as exc:
                LOG.warning("서버(%s) 명령어 등록에 실패했어요: %s", guild.id, exc)
            except Exception:
                LOG.exception("서버(%s) 명령어 등록 중 오류", guild.id)
            else:
                synced_total += 1

        # 등록에 성공한 서버가 하나라도 있을 때만 전역 목록을 비웁니다.
        # (전부 실패했다면 전역 명령어를 남겨 두어 최소한 예전 명령어는 보이게)
        if self._synced_guild_ids:
            await self.clear_global_commands()
            LOG.info(
                "길드 명령어 동기화 완료: 서버 %d곳 (%s)",
                len(self._synced_guild_ids),
                ", ".join(str(guild_id) for guild_id in sorted(self._synced_guild_ids)),
            )
        return synced_total

    async def clear_global_commands(self) -> None:
        """길드 명령어와 겹쳐 2개씩 보이지 않도록 전역 명령어를 비웁니다."""
        if self._global_commands_cleared:
            return
        if not self._command_snapshot:
            self._command_snapshot = list(self.tree.get_commands(guild=None))

        self.tree.clear_commands(guild=None)
        try:
            removed = await self.tree.sync()
        finally:
            # 메모리에는 원본을 다시 채워 둡니다. 이래야 새로 초대된 서버에
            # copy_global_to 로 같은 명령어를 복사할 수 있습니다.
            # (디스코드에 다시 전역 등록하지는 않으므로 중복은 생기지 않습니다.)
            self._restore_global_snapshot()
        self._global_commands_cleared = True
        LOG.info("전역 슬래시 명령어를 비웠어요 (%d개 → 서버별 명령어만 남김)", len(removed))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """새 서버에 초대되면 그 서버에도 곧바로 명령어를 등록합니다."""
        LOG.info("새 서버에 초대됐어요: %s(%s)", guild.name, guild.id)
        if not self._global_commands_cleared:
            # 아직 첫 로그인 전이면 on_ready 의 일괄 동기화가 처리합니다.
            return
        try:
            await self.register_commands_for_guild(guild)
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOG.warning("새 서버(%s) 명령어 등록에 실패했어요: %s", guild.id, exc)
        except Exception:
            LOG.exception("새 서버(%s) 명령어 등록 중 오류", guild.id)

    async def on_ready(self) -> None:
        LOG.info("로그인 완료: %s (서버 %d곳)", self.user, len(self.guilds))
        try:
            await self.sync_commands_to_guilds()
        except Exception:
            LOG.exception("슬래시 명령어 동기화 중 오류 (봇은 계속 동작해요)")
        await self.restore_missing_guilds()

    # -- 백업 채널 -----------------------------------------------------------
    def resolve_backup_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """/기록채널 로 정한 채널, 없으면 이름이 '소현봇-기록' 인 채널."""
        record_id = self.store.record_channel_id(guild.id)
        if record_id:
            channel = guild.get_channel(record_id)
            if isinstance(channel, discord.TextChannel):
                return channel
        for channel in guild.text_channels:
            if channel.name == BACKUP_CHANNEL_NAME:
                return channel
        return None

    def _backup_channels(self, guild: discord.Guild) -> list[discord.TextChannel]:
        """복구용으로 살펴볼 후보 채널 (지정 채널 → 이름 일치 → 나머지 일부)."""
        ordered: list[discord.TextChannel] = []
        primary = self.resolve_backup_channel(guild)
        if primary is not None:
            ordered.append(primary)

        me = guild.me
        for channel in guild.text_channels:
            if channel in ordered:
                continue
            if me is None or not channel.permissions_for(me).read_message_history:
                continue
            ordered.append(channel)
            if len(ordered) > 1 + BACKUP_EXTRA_CHANNELS:
                break
        return ordered

    async def restore_missing_guilds(self) -> None:
        """메모리에 점수가 없는 길드만 기록 채널에서 복구합니다."""
        for guild in self.guilds:
            if self.store.has_scores(guild.id) or guild.id in self._restore_tried:
                continue
            self._restore_tried.add(guild.id)
            try:
                restored = await self.restore_guild(guild)
            except Exception:
                LOG.exception("점수 복구 중 오류: 서버 %s", guild.id)
                continue
            if restored:
                LOG.info("'%s' 서버 점수 %d명을 기록 채널에서 복구했어요", guild.name, restored)

    async def restore_guild(self, guild: discord.Guild) -> int:
        primary = self.resolve_backup_channel(guild)
        for channel in self._backup_channels(guild):
            try:
                payload, message_ids = await read_backup_channel(channel, deep=(channel == primary))
            except (discord.Forbidden, discord.HTTPException):
                continue
            if not payload:
                continue
            restored = self.store.restore_from_payload(
                guild.id, payload, record_channel_id=channel.id, message_ids=message_ids
            )
            if restored:
                self.store.save()
                return restored
        return 0

    async def sync_backup(self, guild_id: int) -> None:
        """점수 JSON 을 기록 채널 메시지에 저장(수정)합니다. 실패해도 봇은 계속 동작합니다."""
        guild = self.get_guild(guild_id)
        if guild is None or not self.store.has_scores(guild_id):
            return

        channel = self.resolve_backup_channel(guild)
        if channel is None:
            LOG.info("점수 기록 채널이 없어 파일에만 저장했어요 (서버 %s)", guild_id)
            return

        me = guild.me
        permissions = channel.permissions_for(me) if me is not None else None
        if permissions is not None and not (permissions.send_messages and permissions.read_message_history):
            LOG.info("'%s' 채널에 백업할 권한이 없어 파일에만 저장했어요", channel.name)
            return

        payload = self.store.backup_payload(guild_id)
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        bodies = backup_message_bodies(text)
        existing = list(self.store.backup_message_ids(guild_id))
        new_ids: list[int] = []

        try:
            for index, body in enumerate(bodies):
                message: Optional[discord.Message] = None
                if index < len(existing):
                    try:
                        message = await channel.fetch_message(existing[index])
                    except discord.NotFound:
                        message = None
                    except (discord.Forbidden, discord.HTTPException):
                        message = None
                if message is not None:
                    if message.content != body:
                        await message.edit(content=body)
                    new_ids.append(message.id)
                    continue
                sent = await channel.send(body)
                new_ids.append(sent.id)
                try:
                    await sent.pin(reason="소현봇 점수 백업")
                except (discord.Forbidden, discord.HTTPException):
                    LOG.debug("백업 메시지를 고정하지 못했어요 (%s)", channel.name)

            for stale_id in existing[len(bodies):]:
                try:
                    stale = await channel.fetch_message(stale_id)
                    await stale.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOG.warning("점수 백업 메시지를 갱신하지 못했어요(%s): %s", channel.name, exc)

        if new_ids:
            self.store.set_record_channel(guild_id, channel.id)
            self.store.set_backup_message_ids(guild_id, new_ids)
            self.store.save()


bot = TeamBot()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    """슬래시 명령어에서 난 오류를 한국어 안내로 바꿔 조용히 알려 줍니다."""
    original: BaseException = error
    if isinstance(error, app_commands.CommandInvokeError):
        original = error.original

    if isinstance(original, TeamBotError):
        message = str(original)
    elif isinstance(error, app_commands.TransformerError):
        message = "채널을 찾지 못했어요. 목록에서 다시 골라 주세요."
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


# ---------------------------------------------------------------------------
# 결과 버튼
# ---------------------------------------------------------------------------


def _button_custom_ids(view: "SplitResultView") -> dict[str, discord.ui.Button]:
    buttons: dict[str, discord.ui.Button] = {}
    for child in view.children:
        if isinstance(child, discord.ui.Button) and child.custom_id:
            buttons[child.custom_id] = child
    return buttons


class SplitResultView(discord.ui.View):
    """팀 나누기 결과에 붙는 버튼들.

    1줄: 🔀 다시 섞기 · 🏆 1팀 승리 · 🏆 2팀 승리 · 📢 대기방으로 모으기
    2줄: ↩️ 방금 정산 취소
    """

    def __init__(
        self,
        *,
        bot_: "TeamBot",
        author_id: int,
        guild: discord.Guild,
        lobby: discord.VoiceChannel,
        team1_channel: discord.VoiceChannel,
        team2_channel: discord.VoiceChannel,
        mode: str,
        team1: Sequence[Player],
        team2: Sequence[Player],
        timeout: float = BUTTON_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot_
        self.author_id = author_id
        self.guild = guild
        self.lobby_id = lobby.id
        self.team1_id = team1_channel.id
        self.team2_id = team2_channel.id
        self.mode = mode
        self.team1: list[Player] = list(team1)
        self.team2: list[Player] = list(team2)
        self.rounds = RoundLog()
        self.message: Optional[discord.Message] = None
        self._lock = asyncio.Lock()
        self._sync_buttons()

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
    def _sync_buttons(self) -> None:
        """승리 버튼은 항상 열어 두고(라운드마다 계속 누름), 취소 버튼만
        정산된 라운드가 있을 때 활성화합니다."""
        for custom_id, button in _button_custom_ids(self).items():
            if custom_id == "team_bot:undo":
                button.disabled = self.rounds.count == 0

    def _refresh_players(self) -> None:
        """정산·취소 뒤 임베드에 보이는 점수를 저장소의 현재 점수로 맞춥니다."""
        def refresh(team: Sequence[Player]) -> list[Player]:
            return [
                Player(
                    member_id=player.member_id,
                    name=player.name,
                    score=self.bot.store.score_of(self.guild.id, player.member_id),
                )
                for player in team
            ]

        self.team1 = refresh(self.team1)
        self.team2 = refresh(self.team2)

    def _resolve(self, channel_id: int, label: str) -> discord.VoiceChannel:
        channel = self.guild.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise TeamBotError(f"{label} 음성 채널이 사라졌어요. 채널을 다시 확인해 주세요.")
        return channel

    def _team_channels(self) -> tuple[discord.VoiceChannel, discord.VoiceChannel]:
        return self._resolve(self.team1_id, "팀 1"), self._resolve(self.team2_id, "팀 2")

    def _current_members(self, channels: Sequence[discord.VoiceChannel]) -> list[discord.Member]:
        """지금 두 팀 채널에 있는 사람들(봇 제외). 부족하면 기록된 참가자로 보완."""
        found: dict[int, discord.Member] = {}
        for channel in channels:
            for member in channel.members:
                if not member.bot:
                    found[member.id] = member
        if len(found) >= 2:
            return list(found.values())

        participant_ids = [player.member_id for player in self.team1 + self.team2]
        for member_id in participant_ids:
            member = self.guild.get_member(member_id)
            if member is not None and not member.bot and member.voice and member.voice.channel:
                found.setdefault(member.id, member)
        return list(found.values())

    def _build_embed(self, **kwargs: Any) -> discord.Embed:
        self._refresh_players()
        return build_split_embed(
            self.team1,
            self.team2,
            mode=self.mode,
            rounds=self.rounds.rounds,
            **kwargs,
        )

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
    @discord.ui.button(
        label="다시 섞기", emoji="🔀", style=discord.ButtonStyle.primary,
        custom_id="team_bot:reshuffle", row=0,
    )
    async def reshuffle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._lock.locked():
            await interaction.response.send_message("지금 이동 중이에요. 잠시만 기다려 주세요.", ephemeral=True)
            return

        failed: list[tuple[discord.Member, str]] = []
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

                players = [self.bot.store.ensure_player(self.guild.id, member.id, member.display_name) for member in members]
                self.bot.store.save()
                team1, team2 = split_players(players, self.mode)
                member_map = {member.id: member for member in members}

                moved1, failed1 = await move_members(
                    [member_map[player.member_id] for player in team1], team1_channel, reason="팀 나누기 봇: 다시 섞기"
                )
                moved2, failed2 = await move_members(
                    [member_map[player.member_id] for player in team2], team2_channel, reason="팀 나누기 봇: 다시 섞기"
                )
                failed = failed1 + failed2

                self.team1, self.team2 = team1, team2
                self.rounds.clear()          # 새 판 → 라운드 스코어 0:0 으로 초기화
                self._sync_buttons()

                embed = self._build_embed(
                    title="🔀 다시 섞은 결과",
                    description=(
                        f"같은 인원을 **{MODE_LABELS.get(self.mode, self.mode)}** 방식으로 다시 나눠 이동시켰어요.\n"
                        "라운드 스코어는 **0 : 0** 으로 초기화됐어요."
                    ),
                    failed=len(failed),
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

        if failed:
            await interaction.followup.send(
                f"⚠️ {len(failed)}명은 이동하지 못했어요.\n{_failure_lines(failed)}",
                ephemeral=True,
            )

    # -- 버튼: 승리 정산 ----------------------------------------------------
    @discord.ui.button(
        label="1팀 승리", emoji="🏆", style=discord.ButtonStyle.success,
        custom_id="team_bot:win1", row=0,
    )
    async def win_team1(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._settle(interaction, winner_number=1)

    @discord.ui.button(
        label="2팀 승리", emoji="🏆", style=discord.ButtonStyle.success,
        custom_id="team_bot:win2", row=0,
    )
    async def win_team2(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._settle(interaction, winner_number=2)

    async def _settle(self, interaction: discord.Interaction, *, winner_number: int) -> None:
        """누를 때마다 한 라운드씩 정산합니다 (팀·버튼 그대로 여러 라운드 가능)."""
        if self._lock.locked():
            await interaction.response.send_message("지금 처리 중이에요. 잠시만 기다려 주세요.", ephemeral=True)
            return

        async with self._lock:
            await interaction.response.defer()
            try:
                winners = self.team1 if winner_number == 1 else self.team2
                losers = self.team2 if winner_number == 1 else self.team1
                if not winners or not losers:
                    raise TeamBotError("정산할 팀 인원이 없어요. 다시 팀을 나눠 주세요.")

                names = {player.member_id: player.name for player in self.team1 + self.team2}
                settlement = self.bot.store.settle(
                    self.guild.id,
                    [player.member_id for player in winners],
                    [player.member_id for player in losers],
                    names=names,
                )
                entry = self.rounds.record(winner_number, settlement)
                self.bot.store.save()
                await self.bot.sync_backup(self.guild.id)

                self._sync_buttons()
                embed = self._build_embed(
                    title=f"🏆 {winner_number}팀 승리! · {entry.round_no}라운드",
                    description=(
                        f"**{winner_number}팀** 승리로 **{entry.round_no}라운드**를 정산했어요. "
                        f"(승리 +{settlement.gain} / 패배 -{settlement.loss})\n"
                        f"같은 팀 그대로 다음 라운드도 **🏆 버튼**으로 이어서 정산할 수 있어요. "
                        f"잘못 눌렀다면 **↩️ 방금 정산 취소** 를 눌러 주세요."
                    ),
                    settlement=settlement,
                    winner_number=winner_number,
                )
                await interaction.edit_original_response(embed=embed, view=self)
            except TeamBotError as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            except Exception:
                LOG.exception("승리 정산 처리 중 오류")
                await interaction.followup.send(
                    "점수를 정산하는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.", ephemeral=True
                )
                return

    # -- 버튼: 대기방으로 모으기 --------------------------------------------
    @discord.ui.button(
        label="대기방으로 모으기", emoji="📢", style=discord.ButtonStyle.secondary,
        custom_id="team_bot:gather", row=0,
    )
    async def gather_back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._lock.locked():
            await interaction.response.send_message("지금 이동 중이에요. 잠시만 기다려 주세요.", ephemeral=True)
            return

        failed: list[tuple[discord.Member, str]] = []
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
                    if isinstance(child, discord.ui.Button):
                        child.disabled = True
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

    # -- 버튼: 방금 정산 취소 ------------------------------------------------
    @discord.ui.button(
        label="방금 정산 취소", emoji="↩️", style=discord.ButtonStyle.danger,
        custom_id="team_bot:undo", row=1,
    )
    async def undo_settlement(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """마지막으로 정산한 라운드 1회분의 점수 변동과 승/패를 정확히 되돌립니다."""
        if self._lock.locked():
            await interaction.response.send_message("지금 처리 중이에요. 잠시만 기다려 주세요.", ephemeral=True)
            return

        async with self._lock:
            entry = self.rounds.latest
            if entry is None:
                await interaction.response.send_message(
                    "되돌릴 정산이 없어요. **🏆 1팀 승리 / 🏆 2팀 승리** 로 한 라운드를 먼저 정산해 주세요.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer()
            try:
                self.bot.store.revert_settlement(
                    self.guild.id,
                    entry.settlement.winners,
                    entry.settlement.losers,
                )
                self.rounds.pop()
                self.bot.store.save()
                await self.bot.sync_backup(self.guild.id)

                self._sync_buttons()
                latest = self.rounds.latest
                description = (
                    f"**{entry.round_no}라운드 ({entry.winner_number}팀 승리)** 정산을 되돌렸어요. "
                    "점수와 승/패가 한 라운드 전으로 돌아갔어요.\n"
                    f"현재 스코어: **{self.rounds.score_line()}**"
                )
                kwargs: dict[str, Any] = {
                    "title": "↩️ 방금 정산을 취소했어요",
                    "description": description,
                }
                if latest is not None:
                    kwargs["settlement"] = latest.settlement
                    kwargs["winner_number"] = latest.winner_number
                embed = self._build_embed(**kwargs)
                await interaction.edit_original_response(embed=embed, view=self)
            except TeamBotError as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            except Exception:
                LOG.exception("방금 정산 취소 처리 중 오류")
                await interaction.followup.send(
                    "정산을 되돌리는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.", ephemeral=True
                )
                return


# ---------------------------------------------------------------------------
# 투표 (내전 날짜·시간 정하기)
# ---------------------------------------------------------------------------

POLL_SEPARATOR_RE = re.compile(r"[,，、]")


def parse_poll_options(raw: str) -> list[str]:
    """쉼표로 구분한 보기 문자열을 정리된 목록으로 바꿉니다.

    '9시, 10시, 11시, 불참' → ['9시', '10시', '11시', '불참']
    앞뒤 공백과 빈 항목을 없애고, 같은 보기가 두 번 나오면 뒤엣것은 버립니다.
    보기가 2개보다 적거나 20개보다 많으면 TeamBotError 로 안내합니다.
    """
    options: list[str] = []
    for part in POLL_SEPARATOR_RE.split(raw or ""):
        text = part.strip()
        if not text or text in options:
            continue
        options.append(text)

    if len(options) < POLL_MIN_OPTIONS:
        raise TeamBotError(
            "보기를 **2개 이상** 쉼표(,)로 구분해 주세요.\n"
            "예: `/투표 제목: 오늘 내전 몇 시? 보기: 9시, 10시, 11시, 불참`"
        )
    if len(options) > POLL_MAX_OPTIONS:
        raise TeamBotError(
            f"보기는 최대 **{POLL_MAX_OPTIONS}개** 까지만 만들 수 있어요. (지금 {len(options)}개)\n"
            "보기를 줄여서 다시 만들어 주세요."
        )
    return options


@dataclass
class PollState:
    """투표 한 건의 상태.

    디스코드 없이도 그대로 돌려 볼 수 있도록 버튼·임베드와 분리해 둡니다.
    표는 보기별로 `{유저ID: 표시이름}` 순서 있는 사전에 담아, 먼저 누른 사람이
    먼저 보이게 합니다(멘션 알림이 가지 않도록 이름만 저장).
    """

    title: str
    options: list[str]
    multi: bool = True              # True: 중복 선택 가능, False: 1인 1표
    author_id: int = 0              # 투표를 만든 사람 (마감 권한)
    poll_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    closed: bool = False
    votes: list[dict[int, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.votes) != len(self.options):
            self.votes = [dict() for _ in self.options]

    # -- 투표 -----------------------------------------------------------------
    def toggle(self, user_id: int, name: str, index: int) -> bool:
        """한 사람의 표를 토글합니다. 방금 눌러서 담겼으면 True, 취소됐으면 False."""
        if self.closed:
            raise TeamBotError("이미 마감된 투표예요. 새 투표를 만들어 주세요.")
        if not 0 <= index < len(self.options):
            raise TeamBotError("없는 보기예요. 화면을 새로 고친 뒤 다시 눌러 주세요.")

        voters = self.votes[index]
        if user_id in voters:
            del voters[user_id]
            return False

        if not self.multi:
            # 1인 1표: 다른 보기에 눌러 둔 표를 먼저 지웁니다.
            for other in self.votes:
                other.pop(user_id, None)
        voters[user_id] = name
        return True

    def count(self, index: int) -> int:
        return len(self.votes[index])

    def voter_names(self, index: int) -> list[str]:
        return list(self.votes[index].values())

    def voter_ids(self, index: int) -> list[int]:
        return list(self.votes[index].keys())

    def total_voters(self) -> int:
        """서로 다른 사람이 몇 명 투표했는지."""
        return len({user_id for voters in self.votes for user_id in voters})

    def winners(self) -> list[int]:
        """가장 많은 표를 받은 보기들 (0표면 빈 목록, 동률이면 여러 개)."""
        best = max((self.count(index) for index in range(len(self.options))), default=0)
        if best == 0:
            return []
        return [index for index in range(len(self.options)) if self.count(index) == best]

    def close(self) -> bool:
        """투표를 마감합니다. 이미 마감됐으면 False."""
        if self.closed:
            return False
        self.closed = True
        return True


def poll_mode_text(state: PollState) -> str:
    if state.multi:
        return "여러 보기를 **동시에** 고를 수 있어요. 누른 보기를 다시 누르면 취소돼요."
    return "**1인 1표**예요. 다른 보기를 누르면 표가 그쪽으로 옮겨가요."


def poll_button_label(state: PollState, index: int) -> str:
    """'9시 (3)' 처럼 득표수를 붙인 버튼 라벨 (디스코드 80자 제한)."""
    suffix = f" ({state.count(index)})"
    text = state.options[index]
    room = POLL_LABEL_LIMIT - len(suffix) - 1      # 1은 말줄임표 자리
    if len(text) > room:
        text = text[:max(0, room)] + "…"
    return f"{text}{suffix}"


def _poll_field_value(names: Sequence[str], limit: int = 1024) -> str:
    """투표한 사람 이름을 한 줄로 (임베드 필드 1024자 한도 안에서).

    이름이 아주 많아도 잘리지 않고 '… 외 N명' 으로 남깁니다.
    """
    if not names:
        return "아직 없음"
    value = ""
    for position, name in enumerate(names):
        candidate = f"{value}, {name}" if value else name
        if len(candidate) > limit - 20:
            return f"{value}, … 외 {len(names) - position}명"
        value = candidate
    return value


def build_poll_embed(state: PollState) -> discord.Embed:
    """투표 현황 임베드 (보기별 인원수 + 투표한 사람 이름, 마감 시 최다 득표 강조)."""
    title = state.title if len(state.title) <= POLL_TITLE_LIMIT else state.title[:POLL_TITLE_LIMIT - 1] + "…"
    winners = set(state.winners()) if state.closed else set()

    if state.closed:
        if winners:
            if len(winners) == len(state.options):
                result = "모든 보기가 같은 표를 받았어요."
            else:
                best = max(state.count(index) for index in winners)
                names = ", ".join(f"**{state.options[index]}**" for index in sorted(winners))
                result = f"🏆 최다 득표: {names} ({best}표)"
        else:
            result = "아무도 투표하지 않았어요."
        description = f"🔒 **투표가 마감됐어요.**\n{result}"
    else:
        description = (
            f"{poll_mode_text(state)}\n"
            "보기 버튼을 누르면 바로 반영돼요. 마감하려면 만든 사람이 **🔒 투표 마감** 을 눌러 주세요."
        )

    embed = discord.Embed(
        title=f"🗳️ {title}",
        description=description,
        colour=discord.Colour.gold() if state.closed else discord.Colour.blurple(),
    )

    for index, option in enumerate(state.options):
        name = option if len(option) <= 200 else option[:200] + "…"
        if index in winners:
            name = f"🏆 {name}"
        embed.add_field(
            name=f"{name} — {state.count(index)}표",
            value=_poll_field_value(state.voter_names(index)),
            inline=True,
        )

    footer = f"투표한 사람 {state.total_voters()}명"
    if state.closed:
        footer += " · 마감됨"
    else:
        footer += " · " + ("중복 선택 가능" if state.multi else "1인 1표")
    embed.set_footer(text=footer)
    return embed


class PollView(discord.ui.View):
    """실시간 투표 버튼들.

    보기 버튼은 5개씩 한 줄(최대 20개 → 4줄)에 놓고, 마지막 줄에 **🔒 투표 마감** 을
    둡니다. 디스코드 뷰 최대 수명(15분)보다 훨씬 긴 7일(`POLL_BUTTON_TIMEOUT`) 동안
    열어 두어, 며칠 전부터 날짜·시간을 모을 수 있게 했습니다.
    """

    def __init__(
        self,
        state: PollState,
        *,
        author_id: Optional[int] = None,
        timeout: float = POLL_BUTTON_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.state = state
        self.author_id = state.author_id if author_id is None else author_id
        self.message: Optional[discord.Message] = None
        self._vote_buttons: dict[int, discord.ui.Button] = {}
        self._close_button: Optional[discord.ui.Button] = None
        self._build_children()

    # -- 버튼 구성 -----------------------------------------------------------
    def _build_children(self) -> None:
        self.clear_items()
        self._vote_buttons = {}

        for index in range(len(self.state.options)):
            button = discord.ui.Button(
                label=poll_button_label(self.state, index),
                style=discord.ButtonStyle.secondary,
                row=index // 5,
                custom_id=f"{POLL_ID_PREFIX}{self.state.poll_id}:{index}",
            )
            button.callback = self._vote_callback(index)
            self._vote_buttons[index] = button
            self.add_item(button)

        close_row = min(4, (len(self.state.options) - 1) // 5 + 1)
        close = discord.ui.Button(
            label="투표 마감",
            emoji="🔒",
            style=discord.ButtonStyle.danger,
            row=close_row,
            custom_id=f"{POLL_ID_PREFIX}{self.state.poll_id}:close",
        )
        close.callback = self._close_callback
        self._close_button = close
        self.add_item(close)
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        """득표수·색·마감 상태를 지금 투표 상태에 맞춥니다."""
        for index, button in self._vote_buttons.items():
            button.label = poll_button_label(self.state, index)
            button.style = (
                discord.ButtonStyle.success if self.state.count(index) else discord.ButtonStyle.secondary
            )
            button.disabled = self.state.closed
        if self._close_button is not None:
            self._close_button.disabled = self.state.closed
            self._close_button.label = "마감됨" if self.state.closed else "투표 마감"

    # -- 도우미 ---------------------------------------------------------------
    @staticmethod
    async def _notice(interaction: discord.Interaction, message: str) -> None:
        """나만 보이는 안내 (이미 응답했으면 followup 으로)."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            LOG.warning("투표 안내 메시지를 보내지 못했어요")

    async def _refresh_message(self, interaction: discord.Interaction) -> None:
        self.refresh_buttons()
        try:
            await interaction.response.edit_message(embed=build_poll_embed(self.state), view=self)
        except discord.HTTPException:
            LOG.warning("투표 메시지를 갱신하지 못했어요")

    def can_close(self, user: Any) -> bool:
        """투표를 만든 사람이거나 '멤버 이동' 권한이 있으면 마감할 수 있습니다."""
        if user is None:
            return False
        if getattr(user, "id", None) == self.author_id:
            return True
        permissions = getattr(user, "guild_permissions", None)
        return bool(permissions is not None and getattr(permissions, "move_members", False))

    # -- 버튼: 보기 투표 ------------------------------------------------------
    def _vote_callback(self, index: int):
        async def callback(interaction: discord.Interaction) -> None:
            await self.handle_vote(interaction, index)

        return callback

    async def handle_vote(self, interaction: discord.Interaction, index: int) -> None:
        """누를 때마다 그 보기에 표를 더하거나(없던 표면) 뺍니다."""
        user = getattr(interaction, "user", None)
        if user is None or getattr(user, "bot", False):
            await self._notice(interaction, "봇은 투표할 수 없어요.")
            return
        try:
            self.state.toggle(
                int(user.id), str(getattr(user, "display_name", None) or user.id), index
            )
        except TeamBotError as error:
            await self._notice(interaction, str(error))
            return
        except Exception:
            LOG.exception("투표 처리 중 오류")
            await self._notice(interaction, "투표를 반영하지 못했어요. 잠시 후 다시 눌러 주세요.")
            return
        await self._refresh_message(interaction)

    # -- 버튼: 투표 마감 ------------------------------------------------------
    async def _close_callback(self, interaction: discord.Interaction) -> None:
        await self.handle_close(interaction)

    async def handle_close(self, interaction: discord.Interaction) -> None:
        if self.state.closed:
            await self._notice(interaction, "이미 마감된 투표예요. 새로 `/투표` 를 만들어 주세요.")
            return
        if not self.can_close(getattr(interaction, "user", None)):
            await self._notice(
                interaction,
                "투표를 만든 사람이나 **'멤버 이동'** 권한이 있는 사람만 마감할 수 있어요.",
            )
            return
        self.state.close()
        await self._refresh_message(interaction)

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# /투표
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="투표",
    description="내전 날짜·시간을 보기 버튼으로 투표해요. (기본: 여러 보기 중복 선택)",
)
@app_commands.guild_only()
@app_commands.describe(
    제목="투표 제목 (예: 오늘 내전 몇 시?)",
    보기="쉼표(,)로 구분한 보기 2~20개 (예: 9시, 10시, 11시, 불참)",
    중복선택="여러 보기를 동시에 고를 수 있게 할지 (기본: 예)",
)
@app_commands.rename(제목="제목", 보기="보기", 중복선택="중복선택")
async def poll_command(
    interaction: discord.Interaction,
    제목: str,
    보기: str,
    중복선택: Optional[bool] = None,
) -> None:
    guild = interaction.guild
    if guild is None:
        raise TeamBotError("서버 안에서만 사용할 수 있는 명령어예요.")

    title = (제목 or "").strip()
    if not title:
        raise TeamBotError("투표 제목을 적어 주세요. (예: `오늘 내전 몇 시?`)")
    if len(title) > POLL_TITLE_LIMIT:
        raise TeamBotError(f"제목은 **{POLL_TITLE_LIMIT}자** 까지 적을 수 있어요. (지금 {len(title)}자)")

    options = parse_poll_options(보기)
    state = PollState(
        title=title,
        options=options,
        multi=True if 중복선택 is None else bool(중복선택),
        author_id=interaction.user.id,
    )
    view = PollView(state, author_id=interaction.user.id)
    await interaction.response.send_message(embed=build_poll_embed(state), view=view)
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:      # 메시지 참조를 못 얻어도 투표는 계속 동작합니다
        LOG.debug("투표 메시지 참조를 가져오지 못했어요")


# ---------------------------------------------------------------------------
# /팀나누기
# ---------------------------------------------------------------------------

SPLIT_MODE_CHOICES = [
    app_commands.Choice(name="밸런스", value=MODE_BALANCE),
    app_commands.Choice(name="무작위", value=MODE_RANDOM),
]


@bot.tree.command(
    name="팀나누기",
    description="대기방에 모인 인원을 점수 밸런스(또는 무작위)로 두 팀으로 나눠 이동시켜요.",
)
@app_commands.guild_only()   # DM 목록에는 뜨지 않게
@app_commands.describe(
    팀1_채널="팀 1이 이동할 음성 채널",
    팀2_채널="팀 2가 이동할 음성 채널",
    방식="팀 나누는 방식 (기본: 밸런스)",
)
@app_commands.rename(팀1_채널="팀1_채널", 팀2_채널="팀2_채널", 방식="방식")
@app_commands.choices(방식=SPLIT_MODE_CHOICES)
async def split_teams(
    interaction: discord.Interaction,
    팀1_채널: discord.VoiceChannel,
    팀2_채널: discord.VoiceChannel,
    방식: Optional[app_commands.Choice[str]] = None,
) -> None:
    guild = interaction.guild
    if guild is None:
        raise TeamBotError("서버 안에서만 사용할 수 있는 명령어예요.")

    mode = 방식.value if 방식 is not None else MODE_BALANCE

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

    players = [bot.store.ensure_player(guild.id, member.id, member.display_name) for member in participants]
    bot.store.save()
    team1, team2 = split_players(players, mode)
    member_map = {member.id: member for member in participants}

    moved1, failed1 = await move_members([member_map[player.member_id] for player in team1], 팀1_채널)
    moved2, failed2 = await move_members([member_map[player.member_id] for player in team2], 팀2_채널)
    all_failed = failed1 + failed2

    description = (
        f"**{lobby.name}** 에 모인 {len(participants)}명을 **{MODE_LABELS[mode]}** 방식으로 나눴어요."
    )
    if mode == MODE_BALANCE:
        gap = abs(team_average(team1) - team_average(team2))
        description += f"\n팀 평균 점수 차이는 **{gap:.0f}점** 이에요."

    embed = build_split_embed(
        team1,
        team2,
        mode=mode,
        description=description,
        failed=len(all_failed),
    )
    view = SplitResultView(
        bot_=bot,
        author_id=user.id,
        guild=guild,
        lobby=lobby,
        team1_channel=팀1_채널,
        team2_channel=팀2_채널,
        mode=mode,
        team1=team1,
        team2=team2,
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
# /점수설정
# ---------------------------------------------------------------------------


@bot.tree.command(name="점수설정", description="특정 멤버의 내전 점수를 직접 정해요. (0~5000)")
@app_commands.guild_only()
@app_commands.describe(멤버="점수를 바꿀 멤버", 점수="새 점수 (0~5000)")
@app_commands.rename(멤버="멤버", 점수="점수")
async def set_score_command(
    interaction: discord.Interaction,
    멤버: discord.Member,
    점수: app_commands.Range[int, MIN_SCORE, MAX_SET_SCORE],
) -> None:
    guild = interaction.guild
    if guild is None:
        raise TeamBotError("서버 안에서만 사용할 수 있는 명령어예요.")
    ensure_score_manager(interaction)
    if 멤버.bot:
        raise TeamBotError("봇에게는 점수를 줄 수 없어요.")

    await interaction.response.defer(ephemeral=True)
    before, after = bot.store.set_score(guild.id, 멤버.id, int(점수), 멤버.display_name)
    bot.store.save()
    await bot.sync_backup(guild.id)

    change = ""
    if before != after:
        change = f" ({before}점 → **{after}점**, {after - before:+d})"
    else:
        change = f" ({after}점 그대로)"
    await interaction.followup.send(
        f"✅ {멤버.display_name} 님의 점수를 **{tier_label(after)} {after}점** 으로 설정했어요.{change}",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /점수초기화
# ---------------------------------------------------------------------------


@bot.tree.command(name="점수초기화", description="멤버(또는 서버 전체) 점수를 1000점·0승 0패로 되돌려요.")
@app_commands.guild_only()
@app_commands.describe(멤버="비워 두면 서버 전체를 초기화해요")
@app_commands.rename(멤버="멤버")
async def reset_score_command(
    interaction: discord.Interaction,
    멤버: Optional[discord.Member] = None,
) -> None:
    guild = interaction.guild
    if guild is None:
        raise TeamBotError("서버 안에서만 사용할 수 있는 명령어예요.")
    ensure_score_manager(interaction)

    await interaction.response.defer(ephemeral=True)
    if 멤버 is not None:
        if 멤버.bot:
            raise TeamBotError("봇의 점수는 초기화할 필요가 없어요.")
        bot.store.reset_member(guild.id, 멤버.id, 멤버.display_name)
        bot.store.save()
        await bot.sync_backup(guild.id)
        await interaction.followup.send(
            f"♻️ {멤버.display_name} 님을 **{DEFAULT_SCORE}점 (0승 0패)** 으로 되돌렸어요.",
            ephemeral=True,
        )
        return

    count = bot.store.reset_guild(guild.id)
    bot.store.save()
    await bot.sync_backup(guild.id)
    await interaction.followup.send(
        f"♻️ 서버 전체 점수를 초기화했어요. ({count}명 → {DEFAULT_SCORE}점)",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /랭킹
# ---------------------------------------------------------------------------


@bot.tree.command(name="랭킹", description="서버 내전 순위표를 보여줘요.")
@app_commands.guild_only()
async def ranking_command(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        raise TeamBotError("서버 안에서만 사용할 수 있는 명령어예요.")

    rows = bot.store.ranking(guild.id)
    embed = build_ranking_embed(guild.name, rows, total=len(rows))
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# /기록채널
# ---------------------------------------------------------------------------


@bot.tree.command(name="기록채널", description="점수 백업용 텍스트 채널을 지정해요. (새 채널을 만들 필요는 없어요)")
@app_commands.guild_only()
@app_commands.describe(채널="점수 JSON 백업 메시지를 남길 텍스트 채널")
@app_commands.rename(채널="채널")
async def set_record_channel_command(
    interaction: discord.Interaction,
    채널: discord.TextChannel,
) -> None:
    guild = interaction.guild
    if guild is None:
        raise TeamBotError("서버 안에서만 사용할 수 있는 명령어예요.")
    ensure_score_manager(interaction)

    me = guild.me
    permissions = 채널.permissions_for(me) if me is not None else None
    if permissions is not None and not (permissions.send_messages and permissions.read_message_history):
        raise TeamBotError(
            f"**{채널.name}** 채널에서 봇이 메시지를 보내거나 읽을 권한이 없어요.\n"
            "채널 설정 → 권한에서 봇 역할의 **'메시지 보내기'**, **'채널 기록 읽기'** 를 허용해 주세요."
        )

    await interaction.response.defer(ephemeral=True)
    bot.store.set_record_channel(guild.id, 채널.id)
    bot.store.save()
    await bot.sync_backup(guild.id)
    await interaction.followup.send(
        f"✅ 점수 기록 채널을 **{채널.name}** 로 정했어요.\n"
        "이 채널의 JSON 백업 메시지로 봇이 재시작해도 점수가 복구돼요.",
        ephemeral=True,
    )


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
