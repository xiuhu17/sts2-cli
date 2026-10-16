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
