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

    def test_revert_settlement_restores_score_and_counts(self):
        for user_id in (1, 2, 3, 4):
            self.store.ensure_player(9, user_id, f"u{user_id}")
        settlement = self.store.settle(9, [1, 2], [3, 4])
        self.assertEqual(self.store.score_of(9, 1), 1025)

        winners, losers = self.store.revert_settlement(9, settlement.winners, settlement.losers)

        self.assertEqual([change.user_id for change in winners], [1, 2])
        self.assertEqual([change.user_id for change in losers], [3, 4])
        self.assertEqual(self.store.score_of(9, 1), 1000)
        self.assertEqual(self.store.score_of(9, 3), 1000)
        rows = {row["user_id"]: row for row in self.store.ranking(9)}
        self.assertEqual((rows[1]["wins"], rows[1]["losses"]), (0, 0))
        self.assertEqual((rows[3]["wins"], rows[3]["losses"]), (0, 0))

    def test_revert_restores_clamped_loser_score(self):
        self.store.ensure_player(9, 1, "이기는사람")
        self.store.set_score(9, 2, 5, "0점직전")
        settlement = self.store.settle(9, [1], [2])
        self.assertEqual(self.store.score_of(9, 2), 0)          # 하한에 걸려 5점만 잃음
        self.store.revert_settlement(9, settlement.winners, settlement.losers)
        self.assertEqual(self.store.score_of(9, 2), 5)
        self.assertEqual(self.store.score_of(9, 1), 1000)

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


class RoundLogTests(unittest.TestCase):
    def _settlement(self, gain=25, loss=20):
        return team_bot.Settlement(
            gain=gain,
            loss=loss,
            winners=[team_bot.ScoreChange(1, "a", 1000, 1000 + gain, "🥇 골드", "🥇 골드")],
            losers=[team_bot.ScoreChange(2, "b", 1000, 1000 - loss, "🥇 골드", "🥈 실버")],
        )

    def test_rounds_accumulate(self):
        log = team_bot.RoundLog()
        self.assertEqual(log.count, 0)
        self.assertIsNone(log.latest)
        self.assertEqual(log.score_line(), "1팀 0승 vs 2팀 0승")

        first = log.record(1, self._settlement())
        second = log.record(2, self._settlement(28, 17))
        third = log.record(1, self._settlement())

        self.assertEqual([first.round_no, second.round_no, third.round_no], [1, 2, 3])
        self.assertEqual(log.count, 3)
        self.assertEqual(log.wins(1), 2)
        self.assertEqual(log.wins(2), 1)
        self.assertEqual(log.score_line(), "1팀 2승 vs 2팀 1승")
        self.assertIs(log.latest, third)

    def test_pop_returns_last_round_only(self):
        log = team_bot.RoundLog()
        log.record(1, self._settlement())
        log.record(2, self._settlement())

        self.assertEqual(log.pop().winner_number, 2)
        self.assertEqual(log.count, 1)
        self.assertEqual(log.latest.winner_number, 1)
        self.assertEqual(log.pop().round_no, 1)
        self.assertIsNone(log.pop())
        self.assertEqual(log.score_line(), "1팀 0승 vs 2팀 0승")

    def test_clear_resets_score(self):
        log = team_bot.RoundLog()
        log.record(1, self._settlement())
        log.record(1, self._settlement())
        log.clear()
        self.assertEqual(log.count, 0)
        self.assertIsNone(log.latest)
        self.assertEqual(log.score_line(), "1팀 0승 vs 2팀 0승")


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

    def test_round_score_field(self):
        players = _players([1000, 1000, 1000, 1000])
        team1, team2 = players[:2], players[2:]
        rounds = [
            team_bot.RoundRecord(1, 1, team_bot.Settlement(25, 20)),
            team_bot.RoundRecord(2, 2, team_bot.Settlement(28, 17)),
            team_bot.RoundRecord(3, 1, team_bot.Settlement(22, 23)),
        ]
        embed = team_bot.build_split_embed(
            team1, team2, settlement=rounds[-1].settlement, winner_number=1, rounds=rounds
        )
        score_field = embed.fields[3]
        self.assertIn("현재 스코어", score_field.name)
        self.assertIn("3라운드", score_field.name)
        self.assertIn("1팀 2승", score_field.value)
        self.assertIn("2팀 1승", score_field.value)
        self.assertIn("승리 +22 / 패배 -23", score_field.value)      # 이번(마지막) 라운드 변동
        self.assertIn("3라운드", embed.fields[2].name)              # 정산 필드에도 라운드 표시
        self.assertIn("3라운드 정산", embed.footer.text)

    def test_no_rounds_means_no_score_field(self):
        players = _players([1000, 1000, 1000, 1000])
        embed = team_bot.build_split_embed(players[:2], players[2:])
        self.assertEqual(len(embed.fields), 2)
        self.assertNotIn("라운드", embed.footer.text)

    def test_ranking_embed(self):
        rows = [{"user_id": 1, "name": "a", "score": 1200, "wins": 3, "losses": 1}]
        embed = team_bot.build_ranking_embed("테스트 서버", rows, total=1)
        self.assertIn("🥇", embed.description)
        self.assertIn("75%", embed.description)


class _StubChannel:
    def __init__(self, channel_id, name=None):
        self.id = channel_id
        self.name = name or f"channel-{channel_id}"
        self.members = []

    def permissions_for(self, me):
        return FakeVoicePermissions()


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.deferred = False
        self.edits = []

    def is_done(self):
        return self.deferred

    async def send_message(self, content=None, **kwargs):
        self.messages.append(content)

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def defer(self, **kwargs):
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append(content)


class FakeInteraction:
    def __init__(self, user=None):
        self.user = user
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
    def __init__(self, guild_id, channels=()):
        self.id = guild_id
        self.channels = {channel.id: channel for channel in channels}
        self.me = FakeMe()

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_member(self, member_id):
        return None


class FakeVoicePermissions:
    def __init__(self, move_members=True):
        self.move_members = move_members


class FakeMe:
    def __init__(self):
        self.guild_permissions = FakeVoicePermissions()


class FakeVoiceState:
    def __init__(self, channel):
        self.channel = channel


class FakeMember:
    """음성 채널 사이를 옮겨 다니는 가짜 멤버."""

    def __init__(self, member_id, name, channel):
        self.id = member_id
        self.display_name = name
        self.bot = False
        self.voice = FakeVoiceState(channel)
        self.moved = []

    async def move_to(self, channel, reason=None):
        self.voice.channel.members.remove(self)
        channel.members.append(self)
        self.voice.channel = channel
        self.moved.append(channel.id)


def _view(store):
    """음성 채널·멤버까지 흔들 수 있는 가짜 서버로 결과 화면을 만듭니다."""
    lobby = _StubChannel(1, "대기방")
    team1_channel = _StubChannel(2, "팀1방")
    team2_channel = _StubChannel(3, "팀2방")
    fake_guild = _StubGuild(7, (lobby, team1_channel, team2_channel))

    team1 = [team_bot.Player(101, "가", 1000), team_bot.Player(102, "나", 1000)]
    team2 = [team_bot.Player(201, "다", 1000), team_bot.Player(202, "라", 1000)]
    for player, channel in ((team1[0], team1_channel), (team1[1], team1_channel),
                            (team2[0], team2_channel), (team2[1], team2_channel)):
        store.ensure_player(7, player.member_id, player.name)
        channel.members.append(FakeMember(player.member_id, player.name, channel))

    view = team_bot.SplitResultView(
        bot_=FakeBot(store),
        author_id=1,
        guild=fake_guild,
        lobby=lobby,
        team1_channel=team1_channel,
        team2_channel=team2_channel,
        mode=team_bot.MODE_BALANCE,
        team1=team1,
        team2=team2,
    )
    # 진짜 discord.VoiceChannel 이 아니라 가짜 채널이라 isinstance 검사를 건너뜁니다.
    view._resolve = lambda channel_id, label: fake_guild.get_channel(channel_id)
    return view


def _buttons(view):
    return {child.custom_id: child for child in view.children if child.custom_id}


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = team_bot.ScoreStore(os.path.join(self.directory.name, "scores.json"))

    def tearDown(self):
        self.directory.cleanup()

    def test_button_layout_and_undo_initially_disabled(self):
        view = _view(self.store)
        layout = [(child.custom_id, child.row) for child in view.children]
        self.assertEqual(
            layout,
            [
                ("team_bot:reshuffle", 0),
                ("team_bot:win1", 0),
                ("team_bot:win2", 0),
                ("team_bot:gather", 0),
                ("team_bot:undo", 1),
            ],
        )
        buttons = _buttons(view)
        self.assertEqual(buttons["team_bot:undo"].label, "방금 정산 취소")
        self.assertTrue(buttons["team_bot:undo"].disabled, "정산 0회에는 취소 버튼이 꺼져 있어야 해요")
        for custom_id in ("team_bot:reshuffle", "team_bot:win1", "team_bot:win2", "team_bot:gather"):
            self.assertFalse(buttons[custom_id].disabled)

    def test_button_timeout_is_three_hours(self):
        self.assertEqual(team_bot.BUTTON_TIMEOUT, 60 * 60 * 3)
        self.assertEqual(_view(self.store).timeout, 60 * 60 * 3)

    def test_four_rounds_accumulate_on_same_teams(self):
        view = _view(self.store)
        for winner_number in (1, 1, 2, 1):
            interaction = FakeInteraction()
            asyncio_run(view._settle(interaction, winner_number=winner_number))
            self.assertTrue(interaction.response.deferred)
            self.assertTrue(interaction.edits, "라운드마다 결과 임베드가 갱신돼야 해요")

        self.assertEqual(view.rounds.count, 4)
        self.assertEqual(view.rounds.score_line(), "1팀 3승 vs 2팀 1승")
        self.assertEqual(view.bot.backups, 4, "라운드마다 기록 채널 백업이 갱신돼야 해요")
        # 라운드마다 Elo 가 다시 계산돼 누적됩니다.
        self.assertEqual(self.store.score_of(7, 101), 1054)
        self.assertEqual(self.store.score_of(7, 201), 965)
        rows = {row["user_id"]: row for row in self.store.ranking(7)}
        self.assertEqual((rows[101]["wins"], rows[101]["losses"]), (3, 1))
        self.assertEqual((rows[201]["wins"], rows[201]["losses"]), (1, 3))

        embed = interaction.edits[0]["embed"]
        self.assertIn("4라운드", embed.title)
        score_field = embed.fields[3]
        self.assertIn("4라운드", score_field.name)
        self.assertIn("1팀 3승", score_field.value)
        self.assertIn("2팀 1승", score_field.value)
        self.assertIn("승리 +22 / 패배 -23", score_field.value)

        buttons = _buttons(view)
        self.assertFalse(buttons["team_bot:win1"].disabled, "연속 라운드를 위해 승리 버튼은 계속 열려 있어야 해요")
        self.assertFalse(buttons["team_bot:win2"].disabled)
        self.assertFalse(buttons["team_bot:undo"].disabled)

    def test_undo_reverts_only_last_round(self):
        view = _view(self.store)
        asyncio_run(view._settle(FakeInteraction(), winner_number=1))
        after_first = (self.store.score_of(7, 101), self.store.score_of(7, 201))
        asyncio_run(view._settle(FakeInteraction(), winner_number=2))
        self.assertNotEqual((self.store.score_of(7, 101), self.store.score_of(7, 201)), after_first)

        interaction = FakeInteraction()
        asyncio_run(view.undo_settlement.callback(interaction))

        self.assertEqual((self.store.score_of(7, 101), self.store.score_of(7, 201)), after_first)
        self.assertEqual(self.store.score_of(7, 102), after_first[0])
        self.assertEqual(self.store.score_of(7, 202), after_first[1])
        self.assertEqual(view.rounds.count, 1)
        self.assertEqual(view.rounds.score_line(), "1팀 1승 vs 2팀 0승")
        self.assertEqual(view.bot.backups, 3, "취소 뒤 기록 채널 백업도 갱신돼야 해요")
        rows = {row["user_id"]: row for row in self.store.ranking(7)}
        self.assertEqual((rows[101]["wins"], rows[101]["losses"]), (1, 0))
        self.assertEqual((rows[201]["wins"], rows[201]["losses"]), (0, 1))

        embed = interaction.edits[0]["embed"]
        self.assertIn("취소", embed.title)
        self.assertIn("2라운드", embed.description)
        self.assertIn("1팀 1승 vs 2팀 0승", embed.description)
        self.assertIn("1라운드", embed.fields[3].name)

    def test_undo_twice_returns_to_start(self):
        view = _view(self.store)
        asyncio_run(view._settle(FakeInteraction(), winner_number=2))
        asyncio_run(view.undo_settlement.callback(FakeInteraction()))
        self.assertEqual(self.store.score_of(7, 101), 1000)
        self.assertEqual(self.store.score_of(7, 201), 1000)
        self.assertEqual(view.rounds.count, 0)
        self.assertTrue(_buttons(view)["team_bot:undo"].disabled)

        empty = FakeInteraction()
        asyncio_run(view.undo_settlement.callback(empty))
        self.assertIn("되돌릴 정산이 없어요", empty.response.messages[0])
        self.assertEqual(self.store.score_of(7, 101), 1000)
        self.assertEqual(view.bot.backups, 2)

    def test_reshuffle_clears_round_score(self):
        view = _view(self.store)
        asyncio_run(view._settle(FakeInteraction(), winner_number=1))
        asyncio_run(view._settle(FakeInteraction(), winner_number=2))
        self.assertEqual(view.rounds.count, 2)
        scores_before = {user_id: self.store.score_of(7, user_id) for user_id in (101, 102, 201, 202)}

        interaction = FakeInteraction()
        asyncio_run(view.reshuffle.callback(interaction))

        self.assertEqual(view.rounds.count, 0)
        self.assertEqual(view.rounds.score_line(), "1팀 0승 vs 2팀 0승")
        embed = interaction.edits[0]["embed"]
        self.assertEqual(len(embed.fields), 2, "새 판에서는 정산·스코어 필드가 사라져야 해요")
        self.assertIn("0 : 0", embed.description)
        buttons = _buttons(view)
        self.assertTrue(buttons["team_bot:undo"].disabled)
        self.assertFalse(buttons["team_bot:win1"].disabled)
        # 팀 조합은 새로 짜이지만, 이미 쌓인 점수 자체는 그대로 남습니다.
        self.assertEqual(len(view.team1) + len(view.team2), 4)
        self.assertEqual({user_id: self.store.score_of(7, user_id) for user_id in (101, 102, 201, 202)}, scores_before)

    def test_commands_registered(self):
        names = {command.name for command in team_bot.bot.tree.get_commands()}
        for expected in {"팀나누기", "모이기", "점수설정", "점수초기화", "랭킹", "기록채널", "투표"}:
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
        for message_id in list(self.pinned_ids):
            if message_id in self.messages:
                yield self.messages[message_id]

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


class _StubHttp:
    """길드/전역 명령어 등록 호출을 기록만 하는 가짜 HTTP 클라이언트."""

    def __init__(self):
        self.calls = []          # ("guild"|"global", guild_id, [이름], payload)

    def _record(self, kind, guild_id, payload):
        self.calls.append((kind, guild_id, [item["name"] for item in payload], payload))
        return []

    async def bulk_upsert_guild_commands(self, application_id, guild_id, payload):
        return self._record("guild", guild_id, payload)

    async def bulk_upsert_global_commands(self, application_id, payload):
        return self._record("global", None, payload)


class _FailingHttp(_StubHttp):
    """길드 등록이 항상 실패하는 가짜 HTTP 클라이언트."""

    async def bulk_upsert_guild_commands(self, application_id, guild_id, payload):
        super().bulk_upsert_guild_commands(application_id, guild_id, payload)
        raise team_bot.discord.HTTPException(_FakeResponse(500), "boom")


class _StubConnection:
    def __init__(self):
        self._command_tree = None
        self._translator = None


class _StubClient:
    application_id = 987654321

    def __init__(self, http):
        self.http = http
        self._connection = _StubConnection()


class _SyncGuild:
    def __init__(self, guild_id):
        self.id = guild_id
        self.name = f"서버{guild_id}"


EXPECTED_COMMANDS = {"팀나누기", "모이기", "점수설정", "점수초기화", "랭킹", "기록채널", "투표"}


class CommandSyncBot:
    """TeamBot 의 슬래시 명령어 동기화 메서드만 빌려 쓰는 가짜 봇."""

    register_commands_for_guild = team_bot.TeamBot.register_commands_for_guild
    _restore_global_snapshot = team_bot.TeamBot._restore_global_snapshot
    sync_commands_to_guilds = team_bot.TeamBot.sync_commands_to_guilds
    clear_global_commands = team_bot.TeamBot.clear_global_commands
    on_guild_join = team_bot.TeamBot.on_guild_join

    def __init__(self, http, guilds):
        self.tree = team_bot.app_commands.CommandTree(_StubClient(http))
        for command in team_bot.bot.tree.get_commands(guild=None):
            self.tree.add_command(command)
        self._guilds = list(guilds)
        self._synced_guild_ids = set()
        self._global_commands_cleared = False
        self._command_snapshot = []

    @property
    def guilds(self):
        return list(self._guilds)


class CommandSyncTests(unittest.TestCase):
    """서버별(길드) 슬래시 명령어 즉시 동기화 테스트."""

    def setUp(self):
        self.http = _StubHttp()
        self.bot = CommandSyncBot(self.http, [_SyncGuild(11), _SyncGuild(22)])

    def _guild_calls(self):
        return [call for call in self.http.calls if call[0] == "guild"]

    def _global_calls(self):
        return [call for call in self.http.calls if call[0] == "global"]

    def test_every_guild_gets_every_command_before_global_is_cleared(self):
        asyncio_run(self.bot.sync_commands_to_guilds())

        guild_calls = self._guild_calls()
        self.assertEqual([call[1] for call in guild_calls], [11, 22])
        for _kind, _guild_id, names, _payload in guild_calls:
            self.assertEqual(set(names), EXPECTED_COMMANDS)

        # 전역은 모든 길드 등록이 끝난 뒤에, 빈 목록으로 한 번만 비웁니다.
        self.assertEqual(self.http.calls[-1][0], "global")
        self.assertEqual(self.http.calls[-1][2], [])
        self.assertEqual(len(self._global_calls()), 1)

    def test_new_options_are_in_guild_payload(self):
        asyncio_run(self.bot.sync_commands_to_guilds())
        _kind, _guild_id, _names, payload = self._guild_calls()[0]
        payload_by_name = {item["name"]: item for item in payload}
        split_options = {option["name"]: option for option in payload_by_name["팀나누기"]["options"]}
        self.assertIn("방식", split_options)
        self.assertEqual({choice["name"] for choice in split_options["방식"]["choices"]}, {"밸런스", "무작위"})
        self.assertFalse(split_options["방식"]["required"])
        self.assertIn("채널", {option["name"] for option in payload_by_name["기록채널"]["options"]})
        self.assertIn("멤버", {option["name"] for option in payload_by_name["점수설정"]["options"]})
        poll_options = {option["name"]: option for option in payload_by_name["투표"]["options"]}
        self.assertEqual(set(poll_options), {"제목", "보기", "중복선택"})
        self.assertTrue(poll_options["제목"]["required"])
        self.assertTrue(poll_options["보기"]["required"])
        self.assertFalse(poll_options["중복선택"]["required"], "중복선택은 기본값이 있는 선택 항목이어야 해요")
        self.assertTrue(poll_options["중복선택"]["description"])

    def test_globals_cleared_once_and_memory_kept_for_later_copy(self):
        asyncio_run(self.bot.sync_commands_to_guilds())
        self.assertTrue(self.bot._global_commands_cleared)
        # 메모리에는 원본이 남아 있어야 새 서버에 다시 복사할 수 있습니다.
        self.assertEqual(len(self.bot.tree.get_commands(guild=None)), len(EXPECTED_COMMANDS))

        # 재접속으로 on_ready 가 다시 불려도 전역을 또 건드리거나 중복 등록하지 않습니다.
        asyncio_run(self.bot.sync_commands_to_guilds())
        self.assertEqual(len(self._global_calls()), 1)
        self.assertEqual(len(self._guild_calls()), 2)

    def test_new_guild_gets_commands_on_join(self):
        asyncio_run(self.bot.sync_commands_to_guilds())
        self.http.calls.clear()

        asyncio_run(self.bot.on_guild_join(_SyncGuild(33)))

        self.assertEqual(len(self.http.calls), 1)
        kind, guild_id, names, _payload = self.http.calls[0]
        self.assertEqual((kind, guild_id), ("guild", 33))
        self.assertEqual(set(names), EXPECTED_COMMANDS)
        self.assertIn(33, self.bot._synced_guild_ids)

    def test_join_before_first_sync_is_left_to_on_ready(self):
        asyncio_run(self.bot.on_guild_join(_SyncGuild(44)))
        self.assertEqual(self.http.calls, [])

    def test_failed_guild_sync_keeps_global_commands(self):
        http = _FailingHttp()
        bot = CommandSyncBot(http, [_SyncGuild(55)])
        asyncio_run(bot.sync_commands_to_guilds())      # 예외 없이 끝나야 함

        self.assertFalse(bot._global_commands_cleared)
        self.assertFalse([call for call in http.calls if call[0] == "global"])
        # 전역 명령어는 그대로 남아 최소한 예전 명령어는 보입니다.
        self.assertEqual(len(bot.tree.get_commands(guild=None)), len(EXPECTED_COMMANDS))

    def test_guild_scope_holds_copied_commands(self):
        guild = _SyncGuild(11)
        asyncio_run(self.bot.register_commands_for_guild(guild))
        names = {command.name for command in self.bot.tree.get_commands(guild=guild)}
        self.assertEqual(names, EXPECTED_COMMANDS)
        # 전역에도 그대로 남아 있어 두 곳에서 중복 등록되지 않습니다.
        self.assertEqual(len(self.bot.tree.get_commands(guild=None)), len(EXPECTED_COMMANDS))


class FakePollUser:
    """투표를 누르는 가짜 사용자."""

    def __init__(self, user_id, name, *, move_members=False, bot=False):
        self.id = user_id
        self.display_name = name
        self.bot = bot
        self.guild_permissions = FakeVoicePermissions(move_members)


def _poll_view(*, multi=True, options=None, author_id=1):
    state = team_bot.PollState(
        title="오늘 내전 몇 시?",
        options=list(options or ["9시", "10시", "11시", "불참"]),
        multi=multi,
        author_id=author_id,
    )
    return team_bot.PollView(state)


def _poll_buttons(view):
    return {child.custom_id.rsplit(":", 1)[-1]: child for child in view.children}


class PollParseTests(unittest.TestCase):
    def test_trims_spaces_and_drops_empties(self):
        self.assertEqual(
            team_bot.parse_poll_options(" 9시 , 10시 ,, 11시 , 불참, "),
            ["9시", "10시", "11시", "불참"],
        )

    def test_fullwidth_and_korean_commas(self):
        self.assertEqual(team_bot.parse_poll_options("9시，10시、11시"), ["9시", "10시", "11시"])

    def test_duplicates_removed_in_order(self):
        self.assertEqual(team_bot.parse_poll_options("9시, 10시, 9시"), ["9시", "10시"])

    def test_requires_two_options(self):
        for raw in ("", "   ", "9시", "9시, , ", "9시, 9시"):
            with self.subTest(raw=raw):
                with self.assertRaises(team_bot.TeamBotError) as caught:
                    team_bot.parse_poll_options(raw)
                self.assertIn("2개 이상", str(caught.exception))

    def test_max_twenty_options(self):
        self.assertEqual(len(team_bot.parse_poll_options(", ".join(f"{i}시" for i in range(20)))), 20)
        with self.assertRaises(team_bot.TeamBotError) as caught:
            team_bot.parse_poll_options(", ".join(f"{i}시" for i in range(21)))
        self.assertIn("20개", str(caught.exception))


class PollStateTests(unittest.TestCase):
    def _state(self, multi=True):
        return team_bot.PollState(title="오늘 내전 몇 시?", options=["9시", "10시", "11시"], multi=multi, author_id=1)

    def test_multi_choice_toggle(self):
        state = self._state()
        self.assertTrue(state.toggle(10, "가", 0))
        self.assertTrue(state.toggle(10, "가", 2), "중복 선택 모드에서는 여러 보기를 동시에 고를 수 있어요")
        self.assertEqual((state.count(0), state.count(2)), (1, 1))
        self.assertEqual(state.total_voters(), 1)

        self.assertFalse(state.toggle(10, "가", 0), "이미 누른 보기를 다시 누르면 취소돼야 해요")
        self.assertEqual((state.count(0), state.count(2)), (0, 1))
        self.assertEqual(state.total_voters(), 1)
        self.assertFalse(state.toggle(10, "가", 2))
        self.assertEqual(state.total_voters(), 0)

    def test_single_choice_moves_vote(self):
        state = self._state(multi=False)
        state.toggle(10, "가", 0)
        state.toggle(10, "가", 1)
        self.assertEqual([state.count(i) for i in range(3)], [0, 1, 0])
        self.assertEqual(state.voter_ids(1), [10])
        self.assertEqual(state.total_voters(), 1)
        state.toggle(10, "가", 1)
        self.assertEqual(state.total_voters(), 0)

    def test_voter_names_keep_vote_order(self):
        state = self._state()
        state.toggle(10, "가", 0)
        state.toggle(11, "나", 0)
        state.toggle(12, "다", 0)
        self.assertEqual(state.voter_names(0), ["가", "나", "다"])

    def test_winners_and_tie(self):
        state = self._state()
        self.assertEqual(state.winners(), [], "0표만 있으면 최다 득표가 없어요")
        state.toggle(10, "가", 0)
        state.toggle(11, "나", 1)
        self.assertEqual(sorted(state.winners()), [0, 1])
        state.toggle(12, "다", 1)
        self.assertEqual(state.winners(), [1])

    def test_closed_poll_rejects_votes(self):
        state = self._state()
        self.assertTrue(state.close())
        self.assertFalse(state.close(), "이미 마감한 투표는 다시 마감되지 않아요")
        with self.assertRaises(team_bot.TeamBotError):
            state.toggle(10, "가", 0)

    def test_bad_option_index(self):
        state = self._state()
        with self.assertRaises(team_bot.TeamBotError):
            state.toggle(10, "가", 3)


class PollEmbedTests(unittest.TestCase):
    def _state(self):
        state = team_bot.PollState(title="오늘 내전 몇 시?", options=["9시", "10시", "불참"], author_id=1)
        state.toggle(10, "가", 0)
        state.toggle(11, "나", 0)
        state.toggle(12, "다", 2)
        return state

    def test_live_embed_shows_counts_and_names(self):
        embed = team_bot.build_poll_embed(self._state())
        self.assertIn("오늘 내전 몇 시?", embed.title)
        self.assertIn("동시에", embed.description)
        self.assertEqual([field.name for field in embed.fields], ["9시 — 2표", "10시 — 0표", "불참 — 1표"])
        self.assertEqual(embed.fields[0].value, "가, 나")
        self.assertEqual(embed.fields[1].value, "아직 없음")
        self.assertIn("투표한 사람 3명", embed.footer.text)
        self.assertIn("중복 선택 가능", embed.footer.text)
        self.assertFalse(any("🏆" in field.name for field in embed.fields), "마감 전에는 강조하지 않아요")

    def test_single_choice_embed_notice(self):
        state = self._state()
        state.multi = False
        embed = team_bot.build_poll_embed(state)
        self.assertIn("1인 1표", embed.description)
        self.assertIn("1인 1표", embed.footer.text)

    def test_closed_embed_highlights_winner(self):
        state = self._state()
        state.close()
        embed = team_bot.build_poll_embed(state)
        self.assertIn("마감", embed.description)
        self.assertIn("🏆 최다 득표: **9시** (2표)", embed.description)
        self.assertTrue(embed.fields[0].name.startswith("🏆 9시"))
        self.assertFalse(embed.fields[2].name.startswith("🏆"))
        self.assertIn("마감됨", embed.footer.text)

    def test_closed_embed_without_votes(self):
        state = team_bot.PollState(title="제목", options=["a", "b"], author_id=1)
        state.close()
        embed = team_bot.build_poll_embed(state)
        self.assertIn("아무도 투표하지 않았어요", embed.description)

    def test_many_voters_are_not_truncated_by_embed_limit(self):
        state = team_bot.PollState(title="제목", options=["a", "b"], author_id=1)
        for index in range(300):
            state.toggle(1000 + index, f"참가자{index}", 0)
        embed = team_bot.build_poll_embed(state)
        self.assertLessEqual(len(embed.fields[0].value), 1024)
        self.assertIn("외", embed.fields[0].value)


class PollViewTests(unittest.TestCase):
    def test_timeout_is_seven_days(self):
        self.assertEqual(team_bot.POLL_BUTTON_TIMEOUT, 60 * 60 * 24 * 7)
        self.assertEqual(_poll_view().timeout, 60 * 60 * 24 * 7)

    def test_button_layout_and_labels(self):
        view = _poll_view()
        self.assertEqual([child.row for child in view.children], [0, 0, 0, 0, 1])
        labels = [child.label for child in view.children]
        self.assertEqual(labels[:4], ["9시 (0)", "10시 (0)", "11시 (0)", "불참 (0)"])
        self.assertEqual(labels[4], "투표 마감")
        close = _poll_buttons(view)["close"]
        self.assertTrue(close.disabled is False)
        self.assertEqual(close.custom_id, f"team_bot:poll:{view.state.poll_id}:close")

    def test_twenty_options_fit_in_five_rows(self):
        view = _poll_view(options=[f"{i}시" for i in range(20)])
        self.assertEqual(len(view.children), 21)
        self.assertEqual([child.row for child in view.children], [0] * 5 + [1] * 5 + [2] * 5 + [3] * 5 + [4])
        for child in view.children:
            self.assertLessEqual(len(child.label), team_bot.POLL_LABEL_LIMIT)
            self.assertLessEqual(len(child.custom_id), 100)

    def test_long_option_label_is_truncated(self):
        view = _poll_view(options=["가" * 200, "짧은 보기"])
        self.assertLessEqual(len(view.children[0].label), team_bot.POLL_LABEL_LIMIT)
        self.assertTrue(view.children[0].label.endswith("… (0)"))

    def test_vote_updates_message_and_label(self):
        view = _poll_view()
        user = FakePollUser(10, "가")
        interaction = FakeInteraction(user)

        asyncio_run(view.handle_vote(interaction, 0))
        self.assertEqual(view.state.count(0), 1)
        self.assertTrue(interaction.response.edits, "투표하면 메시지가 바로 갱신돼야 해요")
        embed = interaction.response.edits[0]["embed"]
        self.assertEqual(embed.fields[0].value, "가")
        self.assertEqual(view.children[0].label, "9시 (1)")

        # 같은 보기를 다시 누르면 취소 (토글)
        asyncio_run(view.handle_vote(FakeInteraction(user), 0))
        self.assertEqual(view.state.count(0), 0)
        self.assertEqual(view.children[0].label, "9시 (0)")

    def test_multi_choice_two_buttons_at_once(self):
        view = _poll_view()
        user = FakePollUser(10, "가")
        asyncio_run(view.handle_vote(FakeInteraction(user), 0))
        asyncio_run(view.handle_vote(FakeInteraction(user), 2))
        self.assertEqual([view.state.count(i) for i in range(4)], [1, 0, 1, 0])
        self.assertEqual(view.children[0].style, team_bot.discord.ButtonStyle.success)
        self.assertEqual(view.children[1].style, team_bot.discord.ButtonStyle.secondary)

    def test_single_choice_mode_replaces_vote(self):
        view = _poll_view(multi=False)
        user = FakePollUser(10, "가")
        asyncio_run(view.handle_vote(FakeInteraction(user), 0))
        asyncio_run(view.handle_vote(FakeInteraction(user), 1))
        self.assertEqual([view.state.count(i) for i in range(4)], [0, 1, 0, 0])
        self.assertEqual(view.children[0].label, "9시 (0)")
        self.assertEqual(view.children[1].label, "10시 (1)")

    def test_bot_cannot_vote(self):
        view = _poll_view()
        interaction = FakeInteraction(FakePollUser(99, "봇", bot=True))
        asyncio_run(view.handle_vote(interaction, 0))
        self.assertEqual(view.state.count(0), 0)
        self.assertIn("봇은 투표할 수 없어요", interaction.response.messages[0])

    def test_close_permissions(self):
        view = _poll_view(author_id=1)
        other = FakeInteraction(FakePollUser(2, "나"))
        asyncio_run(view.handle_close(other))
        self.assertFalse(view.state.closed)
        self.assertIn("만든 사람", other.response.messages[0])

        manager = FakeInteraction(FakePollUser(3, "관리자", move_members=True))
        asyncio_run(view.handle_close(manager))
        self.assertTrue(view.state.closed, "'멤버 이동' 권한자는 마감할 수 있어요")

    def test_creator_can_close_and_buttons_lock(self):
        view = _poll_view(author_id=1)
        author = FakePollUser(1, "만든이")
        asyncio_run(view.handle_vote(FakeInteraction(author), 1))

        interaction = FakeInteraction(author)
        asyncio_run(view.handle_close(interaction))

        self.assertTrue(view.state.closed)
        embed = interaction.response.edits[0]["embed"]
        self.assertIn("마감", embed.description)
        self.assertTrue(embed.fields[1].name.startswith("🏆 10시"))
        for child in view.children:
            self.assertTrue(child.disabled, "마감하면 모든 버튼이 잠겨야 해요")
        self.assertEqual(view.children[-1].label, "마감됨")

        # 마감 뒤 투표는 막힙니다.
        late = FakeInteraction(FakePollUser(5, "늦은이"))
        asyncio_run(view.handle_vote(late, 0))
        self.assertEqual(view.state.count(0), 0)
        self.assertIn("마감된 투표", late.response.messages[0])

    def test_vote_after_timeout_disables_buttons(self):
        view = _poll_view()
        asyncio_run(view.on_timeout())
        for child in view.children:
            self.assertTrue(child.disabled)

    def test_poll_command_registered_with_options(self):
        commands = {command.name: command for command in team_bot.bot.tree.get_commands()}
        self.assertIn("투표", commands)
        parameters = {parameter.name: parameter for parameter in commands["투표"].parameters}
        self.assertEqual(set(parameters), {"제목", "보기", "중복선택"})
        self.assertTrue(parameters["제목"].required)
        self.assertTrue(parameters["보기"].required)
        self.assertFalse(parameters["중복선택"].required)
        self.assertIsNone(parameters["중복선택"].default)


if __name__ == "__main__":
    unittest.main()
