"""Tests for card rewards."""
import pytest


class TestCardReward:
    def test_card_reward_after_combat(self, game):
        state = game.start(seed="cr1")
        game.neow(state, 0, rewards="proceed")
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        state = game.fight(state)
        # After combat the rewards screen lists the card reward; claiming it opens the cards.
        assert state["decision"] == "rewards"
        assert any(r["type"] == "card" for r in state["rewards"])
        state = game.open_card_reward(state)
        assert state["decision"] == "card_reward"

    def test_card_reward_structure(self, game):
        state = game.start(seed="cr2")
        game.neow(state, 0, rewards="proceed")
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        state = game.open_card_reward(game.fight(state))
        if state["decision"] != "card_reward":
            pytest.skip("No card_reward after this fight")
        assert len(state["cards"]) > 0
        for card in state["cards"]:
            assert isinstance(card["name"], str)
            assert "cost" in card
            assert "type" in card

    def test_select_card_adds_to_deck(self, game):
        state = game.start(seed="cr3")
        game.neow(state, 0, rewards="proceed")
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        state = game.open_card_reward(game.fight(state))
        if state["decision"] != "card_reward":
            pytest.skip("No card_reward")
        deck_before = state["player"]["deck_size"]
        state = game.act("select_card_reward", card_index=state["cards"][0]["index"])
        deck_after = state.get("player", {}).get("deck_size", deck_before)
        assert deck_after == deck_before + 1

    def test_skip_card(self, game):
        state = game.start(seed="cr4")
        game.neow(state, 0)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        state = game.open_card_reward(game.fight(state))
        if state["decision"] != "card_reward":
            pytest.skip("No card_reward")
        deck_before = state["player"]["deck_size"]
        state = game.act("skip_card_reward")
        deck_after = state.get("player", {}).get("deck_size", deck_before)
        assert deck_after == deck_before
        # Skip goes back to the rewards screen with the card reward still claimable.
        assert state["decision"] == "rewards"
        assert any(r["type"] == "card" for r in state["rewards"])


def _actions(state):
    return [a["action"] for a in state.get("legal_actions", [])]


def _win(game, state, max_steps=400):
    """Play the first affordable card (or end the turn) until the fight is over."""
    for _ in range(max_steps):
        if state.get("decision") != "combat_play":
            return state
        hand = [c for c in state["hand"] if c.get("can_play") and c["cost"] <= state["energy"]]
        if hand:
            args = {"card_index": hand[0]["index"]}
            if hand[0].get("target_type") == "AnyEnemy":
                args["target_index"] = state["enemies"][0]["index"]
            state = game.act("play_card", **args)
        else:
            state = game.act("end_turn")
    raise RuntimeError("combat did not end")


def _card_reward_after_fight(game, seed, relics):
    game.skip_neow(game.start(seed=seed))
    game.set_player(deck=["BLUDGEON"] * 10, relics=relics, hp=999, max_hp=999)
    state = _win(game, game.enter_room("Monster", encounter="SHRINKER_BEETLE_WEAK"))
    assert state["decision"] == "rewards"
    return game.open_card_reward(state)


class TestCardRewardAlternatives:
    """CardRewardAlternative.Generate: Skip (CanSkip), REROLL (Driftwood, CanReroll) and
    SACRIFICE (Pael's Wing). The screen shows the latest Generate (RefreshOptions after a reroll)."""

    def test_driftwood_rerolls_once(self, game):
        state = _card_reward_after_fight(game, "t1", ["BURNING_BLOOD", "DRIFTWOOD"])
        assert [a["id"] for a in state["alternatives"]] == ["Skip", "REROLL"]
        before = [c["id"] for c in state["cards"]]
        state = game.act("select_card_alternative", alternative_index=1)
        assert state["decision"] == "card_reward"
        assert [c["id"] for c in state["cards"]] != before
        # CanReroll is false now: only Skip is left, as on the game's refreshed screen.
        assert [a["id"] for a in state["alternatives"]] == ["Skip"]
        assert state["can_skip"] is True
        assert "select_card_alternative" not in _actions(state)
        rerolled = [c["id"] for c in state["cards"]]
        err = game.act("select_card_alternative", alternative_index=1)
        assert err["type"] == "error"
        # Skip still works (resolves to OnSelect's own Skip) and the reward stays claimable;
        # reopening shows the rerolled cards without REROLL.
        state = game.act("skip_card_reward")
        assert state["decision"] == "rewards"
        state = game.open_card_reward(state)
        assert state["decision"] == "card_reward"
        assert [c["id"] for c in state["cards"]] == rerolled
        assert [a["id"] for a in state["alternatives"]] == ["Skip"]

    def test_draft_reward_has_no_alternatives(self, game):
        """CanSkip=false (Draft's Neow card rewards): no Skip. Driftwood only marks rewards
        of a RewardsSet (TryModifyRewardsLate), so it adds no REROLL here either."""
        game.send({"cmd": "start_run", "character": "Silent", "seed": "m1", "modifiers": ["DRAFT"]})
        game.set_player(relics=["DRIFTWOOD"])
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "card_reward"
        assert state["alternatives"] == [] and state["can_skip"] is False
        assert set(_actions(state)) == {"select_card_reward"}
        assert game.act("skip_card_reward")["type"] == "error"
        assert game.act("select_card_alternative", alternative_index=0)["type"] == "error"

    def test_paels_wing_second_sacrifice_obtains_relic(self, game):
        """PaelsWing.OnSacrifice: every 2nd sacrifice obtains a relic; the counter
        (DisplayAmount = RewardsSacrificed % 2) goes 1, 0."""
        def sacrifice(state):
            state = game.open_card_reward(_win(game, state))
            assert [a["id"] for a in state["alternatives"]] == ["Skip", "SACRIFICE"]
            return game.act("select_card_alternative", alternative_index=1)

        game.skip_neow(game.start(seed="PAEL1"))
        game.set_player(deck=["BLUDGEON"] * 10, relics=["BURNING_BLOOD", "PAELS_WING"],
                        hp=999, max_hp=999)
        state = sacrifice(game.enter_room("Monster", encounter="SHRINKER_BEETLE_WEAK"))
        assert state["decision"] == "rewards"
        wing = next(r for r in state["player"]["relics"] if r["id"] == "PAELS_WING")
        assert wing["counter"] == 1
        relics_before = len(state["player"]["relics"])
        game.act("proceed")
        state = sacrifice(game.enter_room("Monster", encounter="SHRINKER_BEETLE_WEAK"))
        assert state["decision"] == "rewards"
        wing = next(r for r in state["player"]["relics"] if r["id"] == "PAELS_WING")
        assert wing["counter"] == 0
        assert len(state["player"]["relics"]) == relics_before + 1
        assert any(e.get("type") == "relic_obtained" for e in state.get("events", []))


class TestCardRewardAcrossRuns:
    def test_new_run_cancels_open_card_reward(self, game):
        """A card reward left open when a new run starts is cancelled with its run: it never
        shows up in the next run and nothing is picked for it."""
        state = _card_reward_after_fight(game, "rr1", ["BURNING_BLOOD"])
        assert state["decision"] == "card_reward"
        state = game.start(seed="rr2")
        assert state["type"] == "decision" and state["decision"] != "card_reward"
        state = game.skip_neow(state)
        assert state["decision"] == "map_select"
        assert game.act("select_card_reward", card_index=0)["type"] == "error"
        # The new run's own rewards work as usual.
        game.set_player(hp=999, max_hp=999)
        state = game.open_card_reward(_win(game, game.enter_room("combat")))
        assert state["decision"] == "card_reward"
        state = game.act("select_card_reward", card_index=0)
        assert state["decision"] == "rewards"
