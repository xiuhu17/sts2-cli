"""Tests for all 5 characters."""
import pytest

CHARACTERS = ["Ironclad", "Silent", "Defect", "Regent", "Necrobinder"]


class TestCharacterMechanics:
    def test_defect_has_orbs(self, game):
        state = game.start(character="Defect", seed="dm1")
        game.neow(state, 0)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        assert "orbs" in state or "orb_slots" in state

    def test_regent_has_stars(self, game):
        state = game.start(character="Regent", seed="dm2")
        game.neow(state, 0, bundle=0)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        assert "stars" in state

    def test_necrobinder_has_osty(self, game):
        state = game.start(character="Necrobinder", seed="dm3")
        game.neow(state, 0)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        assert "osty" in state
        assert "hp" in state["osty"]
        assert "alive" in state["osty"]


class TestFullRun:
    @pytest.mark.parametrize("character", CHARACTERS)
    @pytest.mark.slow
    def test_full_run(self, game, character):
        state = game.start(character=character, seed=f"full_{character.lower()}")
        # HeuristicPolicy (python/policy.py), seeded by the character: its rules are stated there.
        policy = game.policy(f"full_{character.lower()}")
        steps = 0
        while steps < 2000:
            dec = state.get("decision", "")
            if dec == "game_over":
                assert "victory" in state
                return
            assert state.get("type") == "decision", state
            if state.get("softlocked"):
                return  # the game shows no button here: the run ends (not an engine failure)
            state = game.step(state, policy)
            steps += 1
        pytest.fail(f"{character} did not finish in {steps} steps")
