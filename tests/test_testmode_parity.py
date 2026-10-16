"""The engine runs with TestMode on; the TestMode branches that are game logic must behave like
the real game (TestMode off): shop potion price jitter, Cauldron / Calling Bell rewards,
discovery-order rooms and act selection from the profile, Rolling Boulder's hits, and reward
sets that are always the player's decision."""

import json
import os
import subprocess

import pytest

from conftest import DOTNET, engine_command  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
FIRST_RUN_PROFILE = os.path.join(HERE, "fixtures", "first_run_progress.save")

# Cauldron/Calling Bell TestMode lists (the hardcoded choices the CLI used to hand out).
CAULDRON_TEST_POTIONS = {"Flex Potion", "Weak Potion", "Vulnerable Potion", "Strength Potion", "Dexterity Potion"}
CALLING_BELL_TEST_RELICS = {"Anchor", "Gremlin Horn", "Mummified Hand"}


def _engine_stderr(env_extra):
    """Start the engine, run one start_run, and return its stderr."""
    env = os.environ.copy()
    env["STS2_GAME_DIR"] = os.path.join(HERE, "..", "lib")
    env.update(env_extra)
    p = subprocess.run(engine_command(DOTNET), input='{"cmd":"start_run","character":"Ironclad","seed":"tm"}\n'
                       '{"cmd":"quit"}\n', capture_output=True, text=True, env=env, timeout=120)
    return p.stderr


class TestPatches:
    def test_every_parity_patch_applied_and_no_unaudited_site(self):
        err = _engine_stderr({"STS2_TESTMODE_AUDIT": "1"})
        assert "[ERROR] TestMode parity" not in err, err
        for target in ("MerchantPotionEntry.CalcCost", "Cauldron.GenerateRewards", "CallingBell.GenerateRewards",
                       "RunManager.ShouldApplyTutorialModifications", "RunManager.TryGetRoomTypeForTutorial",
                       "ActModel.GetRandomList", "RunManager.UpdatePlayerStatsInMapPointHistory",
                       "MerchantRoom.Exit"):
            assert f"[INFO] TestMode off in {target}" in err, target
        unaudited = [l for l in err.splitlines() if "unaudited TestMode branch" in l]
        assert not unaudited, "classify these in src/Sts2Headless/testmode-audited.txt:\n" + "\n".join(unaudited)
        assert "TestMode audit:" in err and ", 0 unaudited" in err


class TestShop:
    def test_potion_prices_are_jittered_from_the_shops_rng(self, game):
        # MerchantPotionEntry.CalcCost: cost * PlayerRng.Shops.NextFloat(0.95, 1.05). TestMode
        # skipped it (flat 50/50/100 here, and no Shops draws).
        game.start(seed="tm-shop")
        state = game.enter_room("shop")
        assert [(p["id"], p["cost"]) for p in state["potions"]] == [
            ("SPEED_POTION", 48), ("ATTACK_POTION", 48), ("FRUIT_JUICE", 100)]
        assert [r["cost"] for r in state["relics"]] == [211, 152, 190]

    @pytest.mark.skipif(not os.path.isfile(os.environ.get("STS2_PARITY_PROFILE", "")),
                        reason="STS2_PARITY_PROFILE (the recorded real-game profile) not set")
    def test_recorded_real_game_shop(self, game):
        # Real game, seed R3B0F9WFJUMG, Ironclad, the recorded profile: Neow Lost Coffer, potion
        # and Shrug It Off, then (1,1) (1,2) (1,3) (0,4) to the floor-6 shop.
        # Replay policy (the recorded run's choices, not "take the first option"): in combat play
        # the hand left to right, then end the turn; claim every reward on a rewards screen; take
        # Shrug It Off when offered; the Self-Help Book page is the recorded "read the back" and
        # Sharp goes on a Strike. Any other decision fails the test.
        state = game.send({"cmd": "start_run", "character": "Ironclad", "seed": "R3B0F9WFJUMG",
                           "profile": os.environ["STS2_PARITY_PROFILE"]})
        state = game.act("choose_option", option_index=1)  # Lost Coffer
        route = [(1, 1), (1, 2), (1, 3), (0, 4), (0, 5)]
        for _ in range(600):
            d = state.get("decision")
            assert state.get("type") != "error", state
            if d == "shop":
                break
            legal = state["legal_actions"]
            if d == "map_select":
                c, r = route.pop(0)
                state = game.act("select_map_node", col=c, row=r)
            elif d == "card_reward":
                names = [c["name"] for c in state["cards"]]
                state = (game.act("select_card_reward", card_index=names.index("Shrug It Off"))
                         if "Shrug It Off" in names else game.act("skip_card_reward"))
            elif d == "combat_play":
                plays = sorted((a for a in legal if a["action"] == "play_card"),
                               key=lambda a: a["args"]["card_index"])
                a = plays[0] if plays else {"action": "end_turn"}
                state = game.act(a["action"], **a.get("args", {}))
            elif d == "rewards":
                claimable = [w for w in state["rewards"]
                             if not (w["type"] == "potion" and state.get("potion_slots_full"))]
                state = (game.act("claim_reward", reward_index=claimable[0]["index"]) if claimable
                         else game.act("proceed"))
            elif d == "event_choice":
                opts = [o for o in state["options"] if not o.get("is_locked")]
                if len(opts) > 1:
                    opts = [o for o in opts if "READ_THE_BACK" in (o.get("text_key") or "")]
                assert len(opts) == 1, state
                state = game.act("choose_option", option_index=opts[0]["index"])
            elif d == "card_select":
                strikes = sorted(c["index"] for c in state["cards"] if c["name"].startswith("Strike"))
                state = game.act("select_cards", indices=str(strikes[0]))  # recorded: a Strike
            else:
                raise AssertionError(f"unexpected decision on the recorded route: {state}")
        assert state["context"]["floor"] == 6
        assert [p["cost"] for p in state["potions"]] == [99, 77, 77]
        assert [c["cost"] for c in state["cards"]] == [36, 74, 143, 49, 74, 82, 173]
        assert [r["cost"] for r in state["relics"]] == [216, 243, 222]
        assert state["card_removal_cost"] == 75

    def test_leaving_a_shop_records_its_unbought_items(self, game, tmp_path):
        # MerchantRoom.Exit (TestMode off): unbought cards/relics/potions go into the map
        # point's history as not picked, like the game's save and run history.
        game.start(seed="tm-shop")
        shop = game.enter_room("shop")
        game.act("leave_room")
        path = str(tmp_path / "run.save")
        game.send({"cmd": "write_continue_save", "path": path})
        with open(path) as f:
            save = json.load(f)
        stats = [p for act in save["map_point_history"] for e in act for p in e["player_stats"]]
        cards = {c["card"]["id"] for p in stats for c in p.get("card_choices", []) if not c["was_picked"]}
        potions = {c["choice"] for p in stats for c in p.get("potion_choices", []) if not c["was_picked"]}
        assert {c["id"] for c in shop["cards"]} <= cards
        assert {"POTION." + p["id"] for p in shop["potions"]} <= potions


class TestRelicRewards:
    def test_cauldron_offers_random_potions_as_a_decision(self, game):
        game.start(seed="tm-cauldron")
        state = game.set_player(obtain_relic="CAULDRON")
        assert state["decision"] == "rewards", state
        names = [w["name"] for w in state["rewards"]]
        assert len(names) == 5 and all(w["type"] == "potion" for w in state["rewards"])
        assert set(names) != CAULDRON_TEST_POTIONS
        assert state["player"]["potions"] == []  # nothing taken for the player
        assert {"action": "proceed"} in [{"action": a["action"]} for a in state["legal_actions"]]
        state = game.act("claim_reward", reward_index=state["rewards"][0]["index"])
        assert [p["name"] for p in state["player"]["potions"]] == [names[0]]

    def test_calling_bell_offers_relics_from_the_pools(self, game):
        game.start(seed="tm-bell")
        state = game.set_player(obtain_relic="CALLING_BELL")
        assert state["decision"] == "rewards", state
        assert [w["type"] for w in state["rewards"]] == ["relic"] * 3
        assert {w["name"] for w in state["rewards"]} != CALLING_BELL_TEST_RELICS
        assert "Curse of the Bell" in [c["name"] for c in game.send({"cmd": "get_deck"})["cards"]]


class TestProfileProgress:
    def test_first_run_profile_applies_discovery_order(self, game):
        # RunManager.ShouldApplyTutorialModifications (TestMode off): the first unseen boss of
        # BossDiscoveryOrder, and a first-ever run's fixed Overgrowth order (Nibbits first).
        # No Neow epoch revealed: the run starts on the map, like the game's first run.
        for seed in ("tm-p1", "tm-p2", "tm-p3"):
            state = game.send({"cmd": "start_run", "character": "Ironclad", "seed": seed,
                               "profile": FIRST_RUN_PROFILE})
            assert state["decision"] == "map_select", state
            assert state["context"]["boss"]["id"] == "VANTOM_BOSS"
        c = state["choices"][0]
        state = game.act("select_map_node", col=c["col"], row=c["row"])
        assert state["decision"] == "combat_play"
        assert [e["monster_id"] for e in state["enemies"]] == ["NIBBIT"]

    def test_no_profile_keeps_random_bosses_and_neow(self, game):
        bosses = set()
        for seed in ("p1", "p2", "p3"):
            state = game.start(seed=seed)
            assert state["decision"] == "event_choice"  # Neow
            bosses.add(state["context"]["boss"]["id"])
        assert len(bosses) > 1


class TestRollingBoulder:
    def test_hits_every_enemy(self, game):
        game.start(seed="boulder")
        game.set_player(deck=["ROLLING_BOULDER"] * 5)
        state = game.enter_room("combat", encounter="TOADPOLES_WEAK")
        before = [e["hp"] for e in state["enemies"]]
        state = game.act("play_card", card_index=0)
        state = game.act("end_turn")
        assert state["decision"] == "combat_play"
        assert [e["hp"] for e in state["enemies"]] == [hp - 5 for hp in before]
