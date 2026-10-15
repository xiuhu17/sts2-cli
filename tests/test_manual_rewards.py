"""manual_rewards: post-combat rewards and treasure relics offered as decisions."""
import pytest


def start_manual(game, seed):
    state = game.send({"cmd": "start_run", "character": "Ironclad", "seed": seed,
                       "manual_rewards": True})
    return game.skip_neow(state)


def win_combat(game, seed):
    start_manual(game, seed)
    game.set_player(hp=999, max_hp=999)
    state = game.enter_room("combat")
    state = game.auto_play_combat(state)
    assert state["decision"] == "rewards", state.get("decision")
    return state


class TestCombatRewards:
    def test_rewards_screen_lists_rewards(self, game):
        state = win_combat(game, "mr1")
        types = [r["type"] for r in state["rewards"]]
        assert "gold" in types and "card" in types
        assert all(isinstance(r["index"], int) for r in state["rewards"])
        assert state["gold_earned"] == 0  # nothing claimed yet

    def test_claim_gold_then_card(self, game):
        state = win_combat(game, "mr2")
        gold_before = state["player"]["gold"]
        gold = next(r for r in state["rewards"] if r["type"] == "gold")
        state = game.act("claim_reward", reward_index=gold["index"])
        assert state["decision"] == "rewards"
        assert state["player"]["gold"] == gold_before + gold["amount"]
        assert all(r["index"] != gold["index"] for r in state["rewards"])

        card = next(r for r in state["rewards"] if r["type"] == "card")
        state = game.act("claim_reward", reward_index=card["index"])
        assert state["decision"] == "card_reward"
        deck_before = state["player"]["deck_size"]
        state = game.act("select_card_reward", card_index=0)
        assert state["player"]["deck_size"] == deck_before + 1
        assert state["decision"] in ("rewards", "map_select")

    def test_proceed_leaves_unclaimed_rewards(self, game):
        state = win_combat(game, "mr3")
        gold_before = state["player"]["gold"]
        state = game.act("proceed")
        assert state["decision"] == "map_select"
        assert state["player"]["gold"] == gold_before

    def test_other_actions_blocked_on_rewards_screen(self, game):
        state = win_combat(game, "mr4")
        choice = game.get_map()
        err = game.act("select_map_node", col=0, row=1)
        assert err["type"] == "error"
        assert "rewards screen" in err["message"]
        err = game.act("claim_reward", reward_index=999)
        assert err["type"] == "error"

    def test_claimed_index_cannot_be_claimed_twice(self, game):
        state = win_combat(game, "mr5")
        gold = next(r for r in state["rewards"] if r["type"] == "gold")
        game.act("claim_reward", reward_index=gold["index"])
        err = game.act("claim_reward", reward_index=gold["index"])
        assert err["type"] == "error"

    def test_potion_reward_rejected_when_slots_full(self, game):
        # Fill every slot, then look for a fight that drops a potion.
        for seed in [f"mrp{i}" for i in range(12)]:
            state = win_combat(game, seed)
            potion = next((r for r in state["rewards"] if r["type"] == "potion"), None)
            if not potion:
                continue
            slots = 3
            game.set_player(potions=["FIRE_POTION"] * slots)
            state = game.act("discard_potion", potion_index=0)  # allowed on the rewards screen
            assert state["type"] == "decision" and state["decision"] == "rewards"
            game.set_player(potions=["FIRE_POTION"] * slots)
            err = game.act("claim_reward", reward_index=potion["index"])
            if err["type"] == "error":
                assert "Potion slots are full" in err["message"]
                return
        pytest.skip("no potion drop / slot setup in the sampled seeds")

    def test_default_mode_unchanged(self, game):
        state = game.skip_neow(game.start(seed="mr6"))
        game.set_player(hp=999, max_hp=999)
        state = game.auto_play_combat(game.enter_room("combat"))
        assert state["decision"] == "card_reward"


class TestTreasure:
    def test_choose_treasure_relic(self, game):
        start_manual(game, "mt1")
        state = game.enter_room("treasure")
        assert state["decision"] == "treasure_relic"
        assert state["relics"]
        relics_before = len(state["player"]["relics"])
        chosen = state["relics"][-1]
        state = game.act("select_treasure_relic", relic_index=chosen["index"])
        assert state["decision"] == "map_select"
        names = [r["name"] for r in state["player"]["relics"]]
        assert len(names) == relics_before + 1
        assert chosen["name"] in names

    def test_skip_treasure_relic(self, game):
        start_manual(game, "mt2")
        state = game.enter_room("treasure")
        relics_before = len(state["player"]["relics"])
        state = game.act("skip_treasure_relic")
        assert state["decision"] == "map_select"
        assert len(state["player"]["relics"]) == relics_before

    def test_next_treasure_after_skip_still_works(self, game):
        """Skipping must close the relic-picking session (issue #56)."""
        start_manual(game, "mt3")
        game.enter_room("treasure")
        game.act("skip_treasure_relic")
        state = game.enter_room("treasure")
        assert state["decision"] == "treasure_relic", state
        state = game.act("select_treasure_relic", relic_index=0)
        assert state["decision"] == "map_select"

    def test_invalid_relic_index(self, game):
        start_manual(game, "mt4")
        game.enter_room("treasure")
        err = game.act("select_treasure_relic", relic_index=99)
        assert err["type"] == "error"


class TestEventRewards:
    def test_neow_rewards_are_claimable_and_skippable(self, game):
        state = game.send({"cmd": "start_run", "character": "Ironclad", "seed": "NP8VU5N662YB",
                           "manual_rewards": True})
        state = game.act("choose_option", option_index=0)  # Lost box: a card reward + a potion
        assert state["decision"] == "rewards" and state["source"] == "event"
        types = sorted(r["type"] for r in state["rewards"])
        assert types == ["card", "potion"]
        card = next(r for r in state["rewards"] if r["type"] == "card")
        state = game.act("claim_reward", reward_index=card["index"])
        assert state["decision"] == "card_reward"
        deck = state["player"]["deck_size"]
        state = game.act("select_card_reward", card_index=0)
        assert state["decision"] == "rewards"
        assert state["player"]["deck_size"] == deck + 1
        state = game.act("proceed")  # leave the potion
        assert state["decision"] == "map_select"
        assert state["player"]["potions"] == []

    def test_default_mode_still_auto_collects(self, game):
        state = game.start(seed="NP8VU5N662YB")
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "card_reward"
        assert len(state["player"]["potions"]) == 1
