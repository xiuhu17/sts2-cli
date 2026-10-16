"""Tests for combat scenarios."""
import pytest


class TestCombatStructure:
    def test_combat_play_fields(self, game):
        state = game.start(seed="cs1")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        assert state["decision"] == "combat_play"
        for key in ("round", "energy", "max_energy", "hand", "enemies",
                    "player", "draw_pile_count", "discard_pile_count", "player_powers"):
            assert key in state, f"Missing: {key}"

    def test_card_fields(self, game):
        state = game.start(seed="cs2")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        for card in state["hand"]:
            assert isinstance(card["name"], str)
            assert "cost" in card
            assert "can_play" in card
            assert card["type"] in ("Attack", "Skill", "Power", "Status", "Curse")

    def test_enemy_fields(self, game):
        state = game.start(seed="cs3")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        for e in state["enemies"]:
            assert isinstance(e["name"], str)
            assert e["hp"] > 0
            assert e["max_hp"] > 0
            assert "block" in e


class TestPlayCards:
    def test_play_card_costs_energy(self, game):
        state = game.start(seed="cp1")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        energy_before = state["energy"]
        playable = [c for c in state["hand"] if c.get("can_play") and c["cost"] <= energy_before]
        assert playable
        card = playable[0]
        args = {"card_index": card["index"]}
        if card.get("target_type") == "AnyEnemy":
            args["target_index"] = state["enemies"][0]["index"]
        state = game.act("play_card", **args)
        if state["decision"] == "combat_play":
            assert state["energy"] == energy_before - card["cost"]

    def test_play_attack_reduces_enemy_hp(self, game):
        state = game.start(seed="cp2")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        target = state["enemies"][0]
        hp_before = target["hp"]
        attacks = [c for c in state["hand"] if c.get("can_play") and c["type"] == "Attack"
                   and c["cost"] <= state["energy"]]
        if not attacks:
            pytest.skip("No attacks in hand")
        card = attacks[0]
        args = {"card_index": card["index"]}
        if card.get("target_type") == "AnyEnemy":
            args["target_index"] = target["index"]
        state = game.act("play_card", **args)
        if state["decision"] == "combat_play":
            new_target = next((e for e in state["enemies"] if e["index"] == target["index"]), None)
            if new_target and target.get("block", 0) == 0:
                assert new_target["hp"] < hp_before

    def test_play_defend_adds_block(self, game):
        state = game.start(seed="cp3")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        block_before = state["player"].get("block", 0)
        defends = [c for c in state["hand"] if c.get("can_play") and c["type"] == "Skill"
                   and c["cost"] <= state["energy"]]
        if not defends:
            pytest.skip("No skill cards")
        state = game.act("play_card", card_index=defends[0]["index"])
        if state["decision"] == "combat_play":
            assert state["player"].get("block", 0) >= block_before


class TestTurnFlow:
    def test_end_turn_advances_round(self, game):
        state = game.start(seed="tf1")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        rnd = state["round"]
        state = game.act("end_turn")
        if state["decision"] == "combat_play":
            assert state["round"] == rnd + 1

    def test_end_turn_resets_energy(self, game):
        state = game.start(seed="tf2")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        max_e = state["max_energy"]
        state = game.act("end_turn")
        if state["decision"] == "combat_play":
            assert state["energy"] == max_e

    def test_end_turn_draws_new_hand(self, game):
        state = game.start(seed="tf3")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        state = game.act("end_turn")
        if state["decision"] == "combat_play":
            assert len(state["hand"]) > 0


class TestCombatEnd:
    def test_win_combat_leads_to_reward(self, game):
        state = game.start(seed="cw1")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        state = game.auto_play_combat(state)
        assert state["decision"] in ("card_reward", "map_select", "card_select", "bundle_select")

    def test_player_powers_after_enemy_debuff(self, game):
        """Shrinker Beetle applies Shrink debuff to player after its turn."""
        state = game.start(seed="ep1")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        # End turn so beetle acts (applies Shrink to player)
        state = game.act("end_turn")
        if state["decision"] == "combat_play":
            pp = state.get("player_powers") or []
            assert len(pp) > 0, "Expected player debuff after Shrinker Beetle turn"
            for pw in pp:
                assert "name" in pw
                assert "amount" in pw
                assert "description" in pw


class TestCombatEdgeCases:
    def test_exhaust_all_and_end_turn(self, game):
        state = game.start(seed="ce1")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        for _ in range(20):
            if state.get("decision") != "combat_play":
                break
            playable = [c for c in state["hand"] if c.get("can_play") and c["cost"] <= state["energy"]]
            if not playable:
                break
            card = playable[0]
            args = {"card_index": card["index"]}
            if card.get("target_type") == "AnyEnemy" and state["enemies"]:
                args["target_index"] = state["enemies"][0]["index"]
            state = game.act("play_card", **args)
        if state.get("decision") == "combat_play":
            state = game.act("end_turn")
            assert state.get("type") != "error"

    def test_many_cards_per_turn(self, game):
        """Play all playable cards in a single turn without errors."""
        state = game.start(seed="inf1")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        plays = 0
        for _ in range(20):
            if state.get("decision") != "combat_play":
                break
            playable = [c for c in state["hand"] if c.get("can_play")
                        and c["cost"] <= state["energy"] and c["type"] not in ("Status", "Curse")]
            if not playable:
                break
            card = playable[0]
            args = {"card_index": card["index"]}
            if card.get("target_type") == "AnyEnemy" and state["enemies"]:
                args["target_index"] = state["enemies"][0]["index"]
            state = game.act("play_card", **args)
            plays += 1
            assert state.get("type") != "error", f"Error after {plays} plays: {state.get('message')}"
        assert plays >= 2

    def test_infinite_card_loop(self, game):
        """Pommel Strike + Bloodletting infinite loop doesn't crash.

        Pommel Strike (1e): damage + draw 1
        Bloodletting (0e): lose HP + gain 2 energy
        Each cycle: net +1 energy, draws next card. Truly infinite.
        """
        state = game.start(seed="inf2")
        game.skip_neow(state)
        game.set_player(hp=80, max_hp=80, deck=["POMMEL_STRIKE"] * 5 + ["BLOODLETTING"] * 5)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")

        plays = 0
        for _ in range(60):
            if state.get("decision") != "combat_play":
                break
            hand = state.get("hand", [])
            energy = state.get("energy", 0)
            playable = [c for c in hand if c.get("can_play") and c["cost"] <= energy
                        and c["type"] not in ("Status", "Curse")]
            if not playable:
                break
            card = playable[0]
            args = {"card_index": card["index"]}
            if card.get("target_type") == "AnyEnemy" and state["enemies"]:
                args["target_index"] = state["enemies"][0]["index"]
            state = game.act("play_card", **args)
            plays += 1
            assert state.get("type") != "error", f"Error after {plays} plays: {state.get('message')}"

        # With Pommel Strike + Bloodletting, should play many cards before enemy dies
        assert plays >= 5, f"Expected infinite loop plays >= 5, got {plays}"

    def test_low_hp_death(self, game):
        """Player with 1 HP should die to any attack."""
        state = game.start(seed="ce2")
        game.skip_neow(state)
        game.set_player(hp=1)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        # Just end turn, beetle will kill us
        state = game.act("end_turn")
        # Might need another turn
        for _ in range(10):
            if state.get("decision") == "game_over":
                break
            if state.get("decision") == "combat_play":
                state = game.act("end_turn")
            else:
                break
        assert state["decision"] == "game_over"
        assert state["victory"] is False


class TestPileContents:
    def test_piles_listed_and_consistent_with_counts(self, game):
        state = game.start(seed="piles1")
        state = game.enter_room("combat")
        assert state["decision"] == "combat_play"
        for key in ("draw_pile", "discard_pile", "exhaust_pile"):
            assert isinstance(state[key], list)
        assert sum(c["count"] for c in state["draw_pile"]) == state["draw_pile_count"]
        assert sum(c["count"] for c in state["discard_pile"]) == state["discard_pile_count"]
        assert state["exhaust_pile_count"] == sum(c["count"] for c in state["exhaust_pile"])
        # Hand + draw pile = whole deck at the start of combat.
        deck = state["player"]["deck_size"]
        assert len(state["hand"]) + state["draw_pile_count"] == deck

    def test_draw_pile_is_sorted_not_in_draw_order(self, game):
        state = game.start(seed="piles2")
        state = game.enter_room("combat")
        # Sorted like the game's draw pile screen: by rarity, then id.
        order = {"Basic": 0, "Common": 1, "Uncommon": 2, "Rare": 3}
        keys = [(order.get(c["rarity"], 9), c["id"]) for c in state["draw_pile"]]
        assert keys == sorted(keys)

    def test_discard_pile_after_playing_a_card(self, game):
        state = game.start(seed="piles3")
        state = game.enter_room("combat")
        card = next(c for c in state["hand"] if c["can_play"] and c["cost"] <= state["energy"])
        args = {"card_index": card["index"]}
        if card["target_type"] == "AnyEnemy":
            args["target_index"] = 0
        state = game.act("play_card", **args)
        if card["type"] != "Power":
            assert state["discard_pile_count"] + state["exhaust_pile_count"] >= 1


class TestCardFixes:
    def test_particle_wall_returning_to_hand_is_not_an_error(self, game):
        """BUG-006: Particle Wall returns itself to the same hand slot after being played."""
        game.skip_neow(game.start(character="Regent", seed="pw1"))
        game.set_player(deck=["PARTICLE_WALL"])
        state = game.enter_room("combat")
        card = next(c for c in state["hand"] if "Particle" in c["name"])
        state = game.act("play_card", card_index=card["index"])
        assert state["type"] == "decision", state
        assert state["player"]["block"] > 0

    def test_neutralize_uses_real_card_logic(self, game):
        game.skip_neow(game.start(character="Silent", seed="nz1"))
        game.set_player(deck=["NEUTRALIZE"] * 5)
        state = game.enter_room("combat")
        enemy = state["enemies"][0]
        state = game.act("play_card", card_index=0, target_index=0)
        assert state["type"] == "decision", state
        after = state["enemies"][0]
        assert after["hp"] < enemy["hp"]
        assert any("Weak" in p["name"] for p in after.get("powers") or [])


class TestPotionUsage:
    def test_automatic_potion_not_usable(self, game):
        game.skip_neow(game.start(seed="fairy1"))
        game.set_player(potions=["FAIRY_IN_A_BOTTLE"])
        state = game.enter_room("combat")
        fairy = next(p for p in state["player"]["potions"] if "Fairy" in p["name"])
        assert fairy["usage"] == "Automatic"
        uses = [a for a in state["legal_actions"] if a["action"] == "use_potion"
                and a["args"]["potion_index"] == fairy["index"]]
        assert uses == []
        err = game.act("use_potion", potion_index=fairy["index"])
        assert err["type"] == "error"


class TestDescriptionText:
    def test_card_descriptions_have_numbers_and_follow_strength(self, game):
        game.skip_neow(game.start(seed="txt1"))
        game.set_player(deck=["STRIKE_IRONCLAD", "BASH", "INFLAME", "TRUE_GRIT", "BLUDGEON"])
        state = game.enter_room("combat")
        for c in state["hand"]:
            assert "{" not in c["description"], c
        strike = next(c for c in state["hand"] if c["id"] == "CARD.STRIKE_IRONCLAD")
        assert "6" in strike["description"]
        inflame = next(c for c in state["hand"] if c["id"] == "CARD.INFLAME")
        state = game.act("play_card", card_index=inflame["index"])
        strike = next(c for c in state["hand"] if c["id"] == "CARD.STRIKE_IRONCLAD")
        assert str(strike["stats"]["damage"]) in strike["description"]
        assert strike["stats"]["damage"] == 8

    def test_keyword_tooltips(self, game):
        game.skip_neow(game.start(seed="txt2", lang="en"))
        game.set_player(deck=["BASH", "TRUE_GRIT"] * 3)
        state = game.enter_room("combat")
        bash = next(c for c in state["hand"] if c["id"] == "CARD.BASH")
        assert any(t["title"] == "Vulnerable" and "50%" in t["description"] for t in bash["tips"])
        grit = next(c for c in state["hand"] if c["id"] == "CARD.TRUE_GRIT")
        assert any(t["title"] == "Exhaust" for t in grit["tips"])

    def test_power_descriptions_filled(self, game):
        game.skip_neow(game.start(seed="txt3", lang="en"))
        game.set_player(deck=["INFLAME"] * 5)
        state = game.enter_room("combat")
        state = game.act("play_card", card_index=0)
        strength = next(p for p in state["player_powers"] if p["name"] == "Strength")
        assert "{" not in strength["description"] and "2" in strength["description"]
