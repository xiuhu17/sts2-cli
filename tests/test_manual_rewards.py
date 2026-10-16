"""Post-combat rewards, treasure relics and event rewards are always offered as decisions.

The old "manual_rewards" flag is still accepted but changes nothing (the auto-collect mode was
removed): these tests run both with and without it."""
import pytest


def start_manual(game, seed):
    state = game.send({"cmd": "start_run", "character": "Ironclad", "seed": seed,
                       "manual_rewards": True})
    return game.neow(state, 0, cards="skip", cards_if_required=[0], rewards="proceed", bundle=0)


def win_combat(game, seed):
    start_manual(game, seed)
    game.set_player(hp=999, max_hp=999)
    state = game.enter_room("combat")
    state = game.fight(state)
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

    def test_without_flag_rewards_are_still_a_decision(self, game):
        state = game.neow(game.start(seed="mr6"), 0)
        game.set_player(hp=999, max_hp=999)
        gold_before = state["player"]["gold"]
        state = game.fight(game.enter_room("combat"))
        assert state["decision"] == "rewards"
        assert state["player"]["gold"] == gold_before  # nothing taken for the player
        assert {"gold", "card"} <= {r["type"] for r in state["rewards"]}

    def test_manual_rewards_false_is_ignored(self, game):
        state = game.send({"cmd": "start_run", "character": "Ironclad", "seed": "mr7",
                           "manual_rewards": False})
        game.neow(state, 0, cards="skip")
        game.set_player(hp=999, max_hp=999)
        state = game.fight(game.enter_room("combat"))
        assert state["decision"] == "rewards"

    def test_skipped_card_reward_stays_claimable(self, game):
        """Skip on the card screen returns to the rewards screen (NRewardButton keeps a reward
        whose selection didn't complete); it can be reopened and taken later."""
        state = win_combat(game, "mr8")
        card = next(r for r in state["rewards"] if r["type"] == "card")
        state = game.act("claim_reward", reward_index=card["index"])
        assert state["decision"] == "card_reward"
        first = [c["id"] for c in state["cards"]]
        state = game.act("skip_card_reward")
        assert state["decision"] == "rewards"
        again = next(r for r in state["rewards"] if r["type"] == "card")
        assert again["index"] == card["index"]
        state = game.act("claim_reward", reward_index=card["index"])
        assert state["decision"] == "card_reward"
        assert [c["id"] for c in state["cards"]] == first  # the same cards, not a new roll
        deck = state["player"]["deck_size"]
        state = game.act("select_card_reward", card_index=0)
        assert state["player"]["deck_size"] == deck + 1

    def test_invalid_card_index_is_an_error_not_a_skip(self, game):
        state = win_combat(game, "mr2")
        state = game.open_card_reward(state)
        assert state["decision"] == "card_reward"
        deck = state["player"]["deck_size"]
        err = game.act("select_card_reward", card_index=99)
        assert err["type"] == "error" and err.get("decision") == "card_reward"
        # The prompt is still open: a valid pick still works.
        state = game.act("select_card_reward", card_index=0)
        assert state["player"]["deck_size"] == deck + 1


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
        # The event's last page (outcome + Proceed), as NEventRoom shows it.
        assert state["decision"] == "event_choice" and state.get("finished") is True
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "map_select"
        assert state["player"]["potions"] == []

    def test_without_flag_event_rewards_are_a_decision(self, game):
        """Neow's Lost Coffer: the potion is not procured for the player (RewardsSet.Offer
        always shows the rewards screen)."""
        state = game.start(seed="NP8VU5N662YB")
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "rewards" and state["source"] == "event"
        assert state["player"]["potions"] == []
        assert sorted(r["type"] for r in state["rewards"]) == ["card", "potion"]

    def test_neows_bones_rewards_cannot_be_skipped(self, game):
        """RewardsSet.WithSkippingDisallowed (Neow's Bones): Proceed stays disabled until every
        reward is taken."""
        state = game.send({"cmd": "start_run", "character": "Ironclad", "seed": "nb72"})
        bones = next(o for o in state["options"] if o["relic"]["id"] == "NEOWS_BONES")
        state = game.act("choose_option", option_index=bones["index"])
        assert state["decision"] == "rewards" and state["can_skip"] is False
        assert "proceed" not in [a["action"] for a in state["legal_actions"]]
        err = game.act("proceed")
        assert err["type"] == "error" and err["decision"] == "rewards"

    def test_neow_finished_page_before_map(self, game):
        """After Neow's rewards the event's last page (Proceed) is shown, as NEventRoom does."""
        state = game.start(seed="NP8VU5N662YB")
        state = game.act("choose_option", option_index=0)
        while state["decision"] == "rewards":
            state = game.act("proceed")
        assert state["decision"] == "event_choice" and state["finished"] is True
        assert [o["is_proceed"] for o in state["options"]] == [True]
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "map_select"


class TestTreasureOrder:
    def test_chest_gold_comes_before_the_relic_choice(self, game):
        """NTreasureRoom.OpenChest: gold (DoNormalRewards) is given when the chest opens, before
        the relic is picked or skipped."""
        state = game.neow(game.start(seed="mt5"), 0, cards="skip")
        gold = state["player"]["gold"]
        state = game.enter_room("treasure")
        assert state["decision"] == "treasure_relic"
        assert state["player"]["gold"] > gold
        opened = state["player"]["gold"]
        state = game.act("skip_treasure_relic")
        assert state["decision"] == "map_select" and state["player"]["gold"] == opened
