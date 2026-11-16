"""bot.py 의 점수·티어·밸런스 분배·저장/복구 로직 단위 테스트.

실행:
    cd discord_team_bot
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as team_bot  # noqa: E402

asyncio_run = asyncio.run


class TierTests(unittest.TestCase):
    def test_boundaries(self):
        cases = {
            1500: "챌린저", 1499: "다이아", 1350: "다이아", 1349: "에메랄드",
            1200: "에메랄드", 1199: "플래티넘", 1100: "플래티넘", 1099: "골드",
            1000: "골드", 999: "실버", 900: "실버", 899: "브론즈",
            800: "브론즈", 799: "아이언", 0: "아이언",
        }
        for score, expected in cases.items():
            with self.subTest(score=score):
                _emoji, name = team_bot.tier_for(score)
                self.assertEqual(name, expected)

    def test_label_has_emoji(self):
        self.assertEqual(team_bot.tier_label(1000), "🥇 골드")
        self.assertEqual(team_bot.tier_label(1600), "👑 챌린저")
        self.assertEqual(team_bot.tier_label(10), "🪨 아이언")


class EloTests(unittest.TestCase):
    def test_even_match(self):
        self.assertEqual(team_bot.elo_deltas(1000, 1000), (25, 20))

    def test_bounds_over_range(self):
        for winner_avg in range(0, 3001, 50):
            for gap in (-1500, -600, -150, 0, 150, 600, 1500):
                gain, loss = team_bot.elo_deltas(winner_avg, winner_avg + gap)
                with self.subTest(winner=winner_avg, gap=gap):
                    self.assertGreaterEqual(gain, 15)
                    self.assertLessEqual(gain, 35)
                    self.assertGreaterEqual(loss, 15)
                    self.assertLessEqual(loss, 30)

    def test_underdog_gains_more(self):
        favourite_gain, favourite_loss = team_bot.elo_deltas(1200, 1000)   # 강팀이 이김
        underdog_gain, underdog_loss = team_bot.elo_deltas(1000, 1200)     # 약팀이 이김
        self.assertGreater(underdog_gain, favourite_gain)
        self.assertGreater(favourite_loss, underdog_loss)
        self.assertEqual(underdog_gain, 35)
        self.assertEqual(favourite_loss, 30)


def _players(scores):
    return [team_bot.Player(member_id=1000 + index, name=f"p{index}", score=score)
            for index, score in enumerate(scores)]


def _avg_diff(team1, team2):
    return abs(team_bot.team_average(team1) - team_bot.team_average(team2))


class BalanceSplitTests(unittest.TestCase):
    def test_equal_scores_split_evenly(self):
        team1, team2 = team_bot.balanced_split(_players([1000] * 10), rng=random.Random(1))
        self.assertEqual((len(team1), len(team2)), (5, 5))
        self.assertEqual(_avg_diff(team1, team2), 0)
        ids = {player.member_id for player in team1 + team2}
        self.assertEqual(len(ids), 10)

    def test_odd_headcount_differs_by_one(self):
        for count in (3, 5, 7, 11, 13):
            team1, team2 = team_bot.balanced_split(_players([1000 + i * 7 for i in range(count)]), rng=random.Random(2))
            with self.subTest(count=count):
                self.assertEqual(abs(len(team1) - len(team2)), 1)
                self.assertEqual(len(team1) + len(team2), count)

    def test_close_to_optimal_difference(self):
        rng = random.Random(3)
        for _ in range(20):
            scores = [rng.randint(800, 1500) for _ in range(10)]
            players = _players(scores)
            team1, team2 = team_bot.balanced_split(players, rng=random.Random(4))
            total = sum(scores)
            best = min(
                abs(sum(scores[i] for i in combo) / 5 - (total - sum(scores[i] for i in combo)) / 5)
                for combo in itertools.combinations(range(10), 5)
            )
            self.assertLessEqual(_avg_diff(team1, team2), best + team_bot.BALANCE_TOLERANCE + 1e-9)

    def test_varies_between_calls(self):
        players = _players([1000] * 10)
        seen = set()
        for _ in range(30):
            team1, _team2 = team_bot.balanced_split(players)
            seen.add(frozenset(player.member_id for player in team1))
        self.assertGreater(len(seen), 1, "같은 인원인데 매번 같은 팀이 나왔어요")

    def test_large_group_heuristic(self):
        rng = random.Random(5)
        scores = [rng.randint(800, 1500) for _ in range(24)]
        team1, team2 = team_bot.balanced_split(_players(scores), rng=random.Random(6))
        self.assertEqual((len(team1), len(team2)), (12, 12))
        self.assertLess(_avg_diff(team1, team2), 40)

    def test_random_mode_is_random(self):
        players = _players(list(range(1000, 1010)))
        team1, team2 = team_bot.split_players(players, team_bot.MODE_RANDOM)
        self.assertEqual((len(team1), len(team2)), (5, 5))

    def test_too_few_players(self):
        self.assertEqual(team_bot.balanced_split(_players([1000]))[1], [])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "data", "scores.json")
        self.store = team_bot.ScoreStore(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_new_member_defaults(self):
        player = self.store.ensure_player(1, 42, "테스터")
        self.assertEqual(player.score, team_bot.DEFAULT_SCORE)
        rows = self.store.ranking(1)
        self.assertEqual(rows[0]["wins"], 0)
        self.assertEqual(rows[0]["losses"], 0)

    def test_settlement_updates_score_and_counts(self):
        for user_id in (1, 2, 3, 4):
            self.store.ensure_player(9, user_id, f"u{user_id}")
        settlement = self.store.settle(9, [1, 2], [3, 4])
        self.assertEqual((settlement.gain, settlement.loss), (25, 20))
        self.assertEqual(self.store.score_of(9, 1), 1025)
        self.assertEqual(self.store.score_of(9, 3), 980)
        rows = {row["user_id"]: row for row in self.store.ranking(9)}
        self.assertEqual(rows[1]["wins"], 1)
        self.assertEqual(rows[3]["losses"], 1)
        self.assertEqual(len(settlement.winners), 2)
        self.assertEqual(settlement.winners[0].delta, 25)
        self.assertEqual(settlement.losers[0].delta, -20)

    def test_score_floor_is_zero(self):
        self.store.set_score(9, 1, 10, "낮은사람")
        self.store.ensure_player(9, 2, "상대")
        self.store.settle(9, [2], [1])
        self.assertEqual(self.store.score_of(9, 1), 0)

    def test_reset_member_and_guild(self):
        self.store.set_score(9, 1, 1500, "a")
        self.store.set_score(9, 2, 1200, "b")
        self.store.reset_member(9, 1, "a")
        self.assertEqual(self.store.score_of(9, 1), team_bot.DEFAULT_SCORE)
        self.assertEqual(self.store.reset_guild(9), 2)
        self.assertEqual(self.store.member_count(9), 0)

    def test_file_roundtrip(self):
        self.store.set_score(9, 1, 1234, "저장")
        self.store.set_record_channel(9, 777)
        self.assertTrue(self.store.save())
        self.assertTrue(os.path.exists(self.path))

        reloaded = team_bot.ScoreStore(self.path)
        self.assertEqual(reloaded.score_of(9, 1), 1234)
        self.assertEqual(reloaded.record_channel_id(9), 777)
        self.assertTrue(reloaded.has_scores(9))

    def test_ranking_order(self):
        self.store.set_score(9, 1, 1100, "a")
        self.store.set_score(9, 2, 1400, "b")
        self.store.set_score(9, 3, 1100, "c")
        rows = self.store.ranking(9)
        self.assertEqual([row["score"] for row in rows], [1400, 1100, 1100])
        self.assertEqual(rows[0]["name"], "b")


class BackupTests(unittest.TestCase):
    def test_single_message_roundtrip(self):
        payload = {"version": 1, "guild": {"members": {"1": {"score": 1000, "wins": 0, "losses": 0, "name": "a"}}}}
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        bodies = team_bot.backup_message_bodies(text)
        self.assertEqual(len(bodies), 1)
        self.assertLessEqual(len(bodies[0]), team_bot.BACKUP_MESSAGE_LIMIT)
        assembled = team_bot.assemble_backup(bodies)
        self.assertIsNotNone(assembled)
        restored, contents = assembled
        self.assertEqual(restored, payload)
        self.assertEqual(contents, bodies)

    def test_multi_part_roundtrip(self):
        members = {
            str(1000 + index): {"score": 1000 + index, "wins": index, "losses": index, "name": f"플레이어{index}"}
            for index in range(150)
        }
        payload = {"version": 1, "guild": {"members": members}}
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        bodies = team_bot.backup_message_bodies(text)
        self.assertGreater(len(bodies), 1)
        for body in bodies:
            self.assertLessEqual(len(body), team_bot.BACKUP_MESSAGE_LIMIT)
        assembled = team_bot.assemble_backup(bodies)
        self.assertIsNotNone(assembled)
        self.assertEqual(assembled[0], payload)

    def test_incomplete_parts_rejected(self):
        payload = {"version": 1, "guild": {"members": {str(i): {"score": 1000} for i in range(200)}}}
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        bodies = team_bot.backup_message_bodies(text)
        self.assertGreater(len(bodies), 1)
        self.assertIsNone(team_bot.assemble_backup(bodies[1:]))

    def test_unrelated_messages_ignored(self):
        self.assertIsNone(team_bot.assemble_backup(["그냥 잡담", "```json\n{}\n```"]))

    def test_restore_from_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            store = team_bot.ScoreStore(os.path.join(directory, "scores.json"))
            payload = {
                "version": 1,
                "guild": {
                    "record_channel_id": 55,
                    "members": {
                        "11": {"score": 1450, "wins": 7, "losses": 2, "name": "소현"},
                        "12": {"score": 990, "wins": 1, "losses": 4, "name": "친구"},
                    },
                },
            }
            count = store.restore_from_payload(9, payload, record_channel_id=99, message_ids=[1, 2])
            self.assertEqual(count, 2)
            self.assertEqual(store.score_of(9, 11), 1450)
            self.assertEqual(store.record_channel_id(9), 99)
            self.assertEqual(store.backup_message_ids(9), [1, 2])
            rows = store.ranking(9)
            self.assertEqual(rows[0]["name"], "소현")
            self.assertEqual(rows[0]["wins"], 7)

    def test_restore_rejects_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            store = team_bot.ScoreStore(os.path.join(directory, "scores.json"))
            self.assertEqual(store.restore_from_payload(9, {"version": 1}), 0)
            self.assertEqual(store.restore_from_payload(9, {"guild": {"members": []}}), 0)


class SettlementGuardTests(unittest.TestCase):
    def test_only_first_click_settles(self):
        guard = team_bot.SettlementGuard()
        self.assertTrue(guard.try_begin())
        self.assertFalse(guard.try_begin())      # 중복 클릭 차단
        self.assertFalse(guard.try_begin())
        self.assertTrue(guard.settled)

    def test_reshuffle_resets(self):
        guard = team_bot.SettlementGuard()
        self.assertTrue(guard.try_begin())
        guard.reset()
        self.assertFalse(guard.settled)
        self.assertTrue(guard.try_begin())


class EmbedTests(unittest.TestCase):
    def test_split_embed_has_teams_and_average(self):
        team1, team2 = team_bot.balanced_split(_players([1000, 1100, 1200, 1000]), rng=random.Random(7))
        embed = team_bot.build_split_embed(team1, team2, mode=team_bot.MODE_BALANCE)
        self.assertEqual(embed.fields[0].name.startswith("🟦 팀 1 · 2명"), True)
        self.assertIn("평균", embed.fields[0].name)
        self.assertEqual(len(embed.fields), 2)
        self.assertIn("밸런스", embed.footer.text)

    def test_settlement_embed_field(self):
        players = _players([1000, 1000, 1000, 1000])
        team1, team2 = players[:2], players[2:]
        settlement = team_bot.Settlement(
            gain=25,
            loss=20,
            winners=[team_bot.ScoreChange(1, "a", 1000, 1025, "🥇 골드", "🥇 골드")],
            losers=[team_bot.ScoreChange(2, "b", 1000, 980, "🥇 골드", "🥈 실버")],
        )
        embed = team_bot.build_split_embed(team1, team2, settlement=settlement, winner_number=1)
        self.assertIn("1팀 승리", embed.fields[2].name)
        self.assertIn("+25", embed.fields[2].value)
        self.assertIn("-20", embed.fields[2].value)

    def test_ranking_embed(self):
        rows = [{"user_id": 1, "name": "a", "score": 1200, "wins": 3, "losses": 1}]
        embed = team_bot.build_ranking_embed("테스트 서버", rows, total=1)
        self.assertIn("🥇", embed.description)
        self.assertIn("75%", embed.description)


class _StubChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.name = f"channel-{channel_id}"


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.deferred = False

    def is_done(self):
        return self.deferred

    async def send_message(self, content=None, **kwargs):
        self.messages.append(content)

    async def defer(self, **kwargs):
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append(content)


class FakeInteraction:
    def __init__(self):
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.edits = []

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


class FakeBot:
    def __init__(self, store):
        self.store = store
        self.backups = 0

    async def sync_backup(self, guild_id):
        self.backups += 1


class _StubGuild:
    def __init__(self, guild_id):
        self.id = guild_id


def _view(store):
    fake_guild = _StubGuild(7)
    team1 = [team_bot.Player(101, "가", 1000), team_bot.Player(102, "나", 1000)]
    team2 = [team_bot.Player(201, "다", 1000), team_bot.Player(202, "라", 1000)]
    for player in team1 + team2:
        store.ensure_player(7, player.member_id, player.name)
    view = team_bot.SplitResultView(
        bot_=FakeBot(store),
        author_id=1,
        guild=fake_guild,
        lobby=_StubChannel(1),
        team1_channel=_StubChannel(2),
        team2_channel=_StubChannel(3),
        mode=team_bot.MODE_BALANCE,
        team1=team1,
        team2=team2,
    )
    return view


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = team_bot.ScoreStore(os.path.join(self.directory.name, "scores.json"))

    def tearDown(self):
        self.directory.cleanup()

    def test_four_buttons_in_one_row(self):
        view = _view(self.store)
        custom_ids = [child.custom_id for child in view.children]
        self.assertEqual(
            custom_ids,
            ["team_bot:reshuffle", "team_bot:win1", "team_bot:win2", "team_bot:gather"],
        )
        for child in view.children:
            self.assertEqual(child.row, 0)
            self.assertFalse(child.disabled)

    def test_settlement_applies_once(self):
        view = _view(self.store)
        first = FakeInteraction()
        asyncio_run(view._settle(first, winner_number=1))

        self.assertEqual(self.store.score_of(7, 101), 1025)
        self.assertEqual(self.store.score_of(7, 102), 1025)
        self.assertEqual(self.store.score_of(7, 201), 980)
        self.assertEqual(self.store.score_of(7, 202), 980)
        self.assertEqual(view.bot.backups, 1)
        self.assertIn("1팀 승리", first.edits[0]["embed"].title)
        self.assertTrue(all(child.disabled for child in view.children if child.custom_id in {"team_bot:win1", "team_bot:win2"}))

        second = FakeInteraction()
        asyncio_run(view._settle(second, winner_number=2))
        self.assertEqual(self.store.score_of(7, 101), 1025, "중복 클릭으로 점수가 두 번 바뀌었어요")
        self.assertEqual(self.store.score_of(7, 202), 980)
        self.assertEqual(self.store.score_of(7, 201), 980)
        self.assertEqual(view.bot.backups, 1)
        self.assertIn("이미 승패가 정산된 판", second.response.messages[0])

    def test_reshuffle_resets_settlement(self):
        view = _view(self.store)
        asyncio_run(view._settle(FakeInteraction(), winner_number=2))
        self.assertTrue(view.guard.settled)
        view.guard.reset()
        view._sync_win_buttons()
        self.assertFalse(any(child.disabled for child in view.children))
        third = FakeInteraction()
        asyncio_run(view._settle(third, winner_number=1))
        # 980점 팀이 1025점 팀을 이겼으니 업셋 가산(+28)이 붙어요
        self.assertEqual(self.store.score_of(7, 101), 1008)

    def test_commands_registered(self):
        names = {command.name for command in team_bot.bot.tree.get_commands()}
        for expected in {"팀나누기", "모이기", "점수설정", "점수초기화", "랭킹", "기록채널"}:
            self.assertIn(expected, names)


class FakePermissions:
    def __init__(self, *, send_messages=True, read_message_history=True):
        self.send_messages = send_messages
        self.read_message_history = read_message_history


class FakeMessage:
    def __init__(self, message_id, content, channel):
        self.id = message_id
        self.content = content
        self.channel = channel
        self.pinned = False

    async def edit(self, content=None, **kwargs):
        self.content = content

    async def delete(self):
        self.channel.messages.pop(self.id, None)

    async def pin(self, reason=None):
        self.pinned = True
        if self.id not in self.channel.pinned_ids:
            self.channel.pinned_ids.append(self.id)


class FakeTextChannel:
    """백업 메시지를 저장하는 가짜 텍스트 채널."""

    def __init__(self, channel_id=500, name=team_bot.BACKUP_CHANNEL_NAME, *, can_write=True):
        self.id = channel_id
        self.name = name
        self.messages = {}
        self.pinned_ids = []
        self._next_id = 9000
        self.can_write = can_write
        self.denied = False

    def permissions_for(self, me):
        return FakePermissions(send_messages=self.can_write, read_message_history=self.can_write)

    async def send(self, content):
        if not self.can_write:
            self.denied = True
            raise team_bot.discord.Forbidden(_FakeResponse(403), "forbidden")
        self._next_id += 1
        message = FakeMessage(self._next_id, content, self)
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id):
        message = self.messages.get(message_id)
        if message is None:
            raise team_bot.discord.NotFound(_FakeResponse(404), "unknown message")
        return message

    async def pins(self):
        return [self.messages[i] for i in self.pinned_ids if i in self.messages]

    async def history(self, limit=None):
        items = sorted(self.messages.values(), key=lambda message: message.id, reverse=True)[:limit]
        for message in items:
            yield message


class _FakeResponse:
    def __init__(self, status):
        self.status = status
        self.reason = "fake"


class FakeGuildForBackup:
    def __init__(self, channel, guild_id=7):
        self.id = guild_id
        self.name = "테스트 서버"
        self.channel = channel
        self.me = None

    @property
    def text_channels(self):
        return [self.channel]

    def get_channel(self, channel_id):
        return self.channel if channel_id == self.channel.id else None


class FakeTeamBot(FakeBot):
    """TeamBot 의 백업/복구 메서드만 빌려 쓰는 가짜 봇."""

    def __init__(self, store, guild):
        super().__init__(store)
        self.guild = guild

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None

    resolve_backup_channel = team_bot.TeamBot.resolve_backup_channel
    _backup_channels = team_bot.TeamBot._backup_channels
    restore_guild = team_bot.TeamBot.restore_guild

    async def sync_backup(self, guild_id):
        await team_bot.TeamBot.sync_backup(self, guild_id)


class BackupChannelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.channel = FakeTextChannel()
        self.guild = FakeGuildForBackup(self.channel)

    def tearDown(self):
        self.directory.cleanup()

    def _store(self, name):
        return team_bot.ScoreStore(os.path.join(self.directory.name, name))

    def test_backup_then_restore_after_restart(self):
        original = self._store("a.json")
        original.set_score(7, 11, 1450, "소현")
        original.set_score(7, 12, 990, "친구")
        original.settle(7, [11], [12])
        original.save()

        writer = FakeTeamBot(original, self.guild)
        asyncio_run(writer.sync_backup(7))
        self.assertEqual(len(self.channel.messages), 1)
        self.assertTrue(self.channel.pinned_ids, "백업 메시지가 고정되지 않았어요")

        restarted = self._store("b.json")          # 파일이 사라진 새 프로세스
        self.assertFalse(restarted.has_scores(7))
        reader = FakeTeamBot(restarted, self.guild)
        restored = asyncio_run(reader.restore_guild(self.guild))

        self.assertEqual(restored, 2)
        self.assertEqual(restarted.score_of(7, 11), 1465)
        self.assertEqual(restarted.score_of(7, 12), 960)
        self.assertEqual(restarted.record_channel_id(7), self.channel.id)
        self.assertTrue(restarted.has_scores(7))

    def test_repeated_backup_edits_same_message(self):
        store = self._store("a.json")
        store.set_score(7, 11, 1000, "소현")
        store.save()
        writer = FakeTeamBot(store, self.guild)
        asyncio_run(writer.sync_backup(7))
        first_ids = set(self.channel.messages)

        store.set_score(7, 11, 1200, "소현")
        store.save()
        asyncio_run(writer.sync_backup(7))
        self.assertEqual(set(self.channel.messages), first_ids)
        self.assertIn("1200", self.channel.messages[max(first_ids)].content)

    def test_large_backup_splits_and_restores(self):
        store = self._store("a.json")
        for index in range(120):
            store.set_score(7, 1000 + index, 1000 + index, f"플레이어{index}")
        store.save()
        writer = FakeTeamBot(store, self.guild)
        asyncio_run(writer.sync_backup(7))
        self.assertGreater(len(self.channel.messages), 1)

        restarted = self._store("b.json")
        reader = FakeTeamBot(restarted, self.guild)
        self.assertEqual(asyncio_run(reader.restore_guild(self.guild)), 120)
        self.assertEqual(restarted.score_of(7, 1000), 1000)
        self.assertEqual(restarted.score_of(7, 1119), 1119)

    def test_read_only_channel_does_not_crash(self):
        channel = FakeTextChannel(can_write=False)
        guild = FakeGuildForBackup(channel)
        store = self._store("a.json")
        store.set_score(7, 11, 1300, "소현")
        store.save()
        writer = FakeTeamBot(store, guild)
        asyncio_run(writer.sync_backup(7))       # 예외 없이 끝나야 함
        self.assertEqual(store.score_of(7, 11), 1300)
        self.assertTrue(store.has_scores(7))
        self.assertEqual(asyncio_run(FakeTeamBot(self._store("b.json"), guild).restore_guild(guild)), 0)


if __name__ == "__main__":
    unittest.main()
