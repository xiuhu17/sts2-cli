"""Map / run-level / potion parity with the real game.

Covers: travelability (start node only, Winged Boots free travel, no teleporting out of rooms),
the Ascension 10 double boss, top-bar context (ascension, seed, total floor, boss titles),
get_map completeness, relic counters, out-of-combat potion rules and the mid-room continue save.
"""
import random

from conftest import Game


def _legal(state, action):
    return [a for a in state.get("legal_actions", []) if a["action"] == action]


def _to_map(game, state):
    state = game.neow(state, 0, cards="skip", rewards="proceed")
    assert state["decision"] == "map_select", state
    return state


def _finish_rooms(game, state, max_steps=400, seen=None):
    """Play the rooms out with HeuristicPolicy('tests') (python/policy.py) until the next
    map_select or game_over."""
    policy = game.policy()
    for _ in range(max_steps):
        d = state.get("decision")
        if d in ("map_select", "game_over"):
            return state
        if seen is not None:
            seen.append((d, state.get("event_id")))
        state = game.step(state, policy)
        assert state.get("type") != "error", state
    raise RuntimeError("room did not finish")


class TestTravel:
    def test_start_of_act_offers_only_the_start_node(self, game):
        state = game.start(seed="mr1")
        m = game.get_map()
        start = m["start"]
        assert start["type"] == "Ancient" and start["current"] and start["visited"]
        assert start["children"], "start node is the graph root"
        assert m["visited_path"] == [{"col": start["col"], "row": start["row"]}]

    def test_select_map_node_rejected_outside_map(self, game):
        state = game.start(seed="mr2")
        assert state["decision"] == "event_choice"  # Neow
        r = game.act("select_map_node", col=0, row=9)
        # Rejected by the action validator (illegal_action) before the engine's own map check.
        assert r["type"] == "error" and r.get("code") == "illegal_action" and r["decision"] == "event_choice"
        state = _to_map(game, state)
        c = state["choices"][0]
        state = game.act("select_map_node", col=c["col"], row=c["row"])
        assert state["decision"] == "combat_play"
        r = game.act("select_map_node", col=c["col"], row=c["row"] + 1)
        assert r["type"] == "error"
        m = game.get_map()
        assert not any(n["travelable"] for row in m["rows"] for n in row), "no travel while in a room"

    def test_unreachable_node_rejected(self, game):
        state = _to_map(game, game.start(seed="mr3"))
        r = game.act("select_map_node", col=99, row=1)
        assert r["type"] == "error" and "not reachable" in r["message"]
        m = game.get_map()
        travelable = {(n["col"], n["row"]) for row in m["rows"] for n in row if n["travelable"]}
        assert travelable == {(c["col"], c["row"]) for c in state["choices"]}

    def test_winged_boots_free_travel(self, game):
        state = _to_map(game, game.start(seed="x1"))
        game.set_player(relics=["BURNING_BLOOD", "WINGED_BOOTS"])
        c = state["choices"][0]
        state = _finish_rooms(game, game.act("select_map_node", col=c["col"], row=c["row"]))
        assert state["decision"] == "map_select"
        assert state.get("free_travel_available") is True
        far = [c for c in state["choices"] if c.get("free_travel")]
        assert far, "Winged Boots offers the whole next row"
        boots = next(r for r in state["player"]["relics"] if r["id"] == "WINGED_BOOTS")
        assert boots["counter"] == 3
        state = game.act("select_map_node", col=far[0]["col"], row=far[0]["row"])
        assert state.get("type") != "error", state
        boots = next(r for r in state["player"]["relics"] if r["id"] == "WINGED_BOOTS")
        assert boots["counter"] == 2


class TestContext:
    def test_top_bar_fields(self, game):
        state = game.start(seed="mr4", ascension=3)
        ctx = state["context"]
        assert ctx["ascension"] == 3
        assert ctx["seed"] == "MR4"
        assert ctx["total_floor"] == 1
        assert ctx["boss"]["name"] and not ctx["boss"]["name"].endswith(".name")
        m = game.get_map()
        assert [e["level"] for e in m["ascension_effects"]] == [1, 2, 3]
        assert m["ascension_effects"][1]["name"] == "Weary Traveler"

    def test_boss_name_is_encounter_title(self, game):
        # Every boss id must resolve to its encounter title, never a raw key.
        for seed in ("b1", "b2", "b3", "b4", "b5", "b6"):
            state = game.start(seed=seed)
            boss = state["context"]["boss"]
            assert "." not in boss["name"] and "{" not in boss["name"], boss
            if boss["id"] == "THE_KIN_BOSS":
                assert boss["name"] == "The Kin"


class TestRelicState:
    def test_counters(self, game):
        state = _to_map(game, game.start(seed="mr5"))
        r = game.set_player(relics=["BURNING_BLOOD", "PEN_NIB", "NUNCHAKU"])
        relics = {x["id"]: x for x in r["player"]["relics"]}
        assert relics["PEN_NIB"]["counter"] == 0
        assert "counter" not in relics["BURNING_BLOOD"]
        c = state["choices"][0]
        state = game.act("select_map_node", col=c["col"], row=c["row"])
        state = _finish_rooms(game, state)
        relics = {x["id"]: x for x in state["player"]["relics"]}
        assert relics["NUNCHAKU"]["counter"] > 0


    def test_wax_relic_name(self, tmp_path):
        """Wax relics (Toy Box) carry the game's "Wax {Title}" prefix and the wax flag."""
        import json
        path, wax_path = tmp_path / "w.save", tmp_path / "wax.save"
        g = Game()
        try:
            _to_map(g, g.start(seed="w1"))
            g.set_player(relics=["BURNING_BLOOD", "ANCHOR"])
            assert g.send({"cmd": "write_continue_save", "path": str(path)})["success"]
        finally:
            g.close()
        save = json.loads(path.read_text())
        for r in save["players"][0]["relics"]:
            if r["id"] == "RELIC.ANCHOR":
                r["props"] = {"bools": [{"name": "IsWax", "value": True}]}
        wax_path.write_text(json.dumps(save))
        for lang, name in (("en", "Wax Anchor"), ("zh", "蜡制锚")):
            g = Game()
            try:
                state = g.send({"cmd": "load_save", "path": str(wax_path), "lang": lang})
                anchor = next(r for r in state["player"]["relics"] if r["id"] == "ANCHOR")
                assert anchor["name"] == name and anchor["wax"] is True, anchor
            finally:
                g.close()


class TestPotionsOutOfCombat:
    def test_anytime_listed_combat_only_rejected(self, game):
        state = _to_map(game, game.start(seed="mr6"))
        r = game.set_player(potions=["BLOOD_POTION", "FIRE_POTION"], hp=30)
        pots = r["player"]["potions"]
        assert pots[0]["can_use"] is True and pots[1]["can_use"] is False
        # Fire Potion at the map: an error, and the potion stays.
        r = game.act("use_potion", potion_index=1)
        assert r["type"] == "error" and "combat" in r["message"]
        state = game.act("use_potion", potion_index=0)
        assert state["decision"] == "map_select"
        assert state["player"]["hp"] > 30
        assert [p["name"] for p in state["player"]["potions"]] == ["Fire Potion"]
        assert not _legal(state, "use_potion"), "Fire Potion isn't usable at the map"

    def test_use_potion_in_legal_actions_at_map(self, game):
        state = _to_map(game, game.start(seed="mr7"))
        game.set_player(potions=["FRUIT_JUICE", "FIRE_POTION"])
        c = state["choices"][0]
        state = _finish_rooms(game, game.act("select_map_node", col=c["col"], row=c["row"]))
        assert state["decision"] == "map_select"
        uses = _legal(state, "use_potion")
        assert uses == [{"action": "use_potion", "args": {"potion_index": 0}}]
        max_hp = state["player"]["max_hp"]
        state = game.act(**{"action": "use_potion", **uses[0]["args"]})
        assert state["player"]["max_hp"] == max_hp + 5

    def test_foul_potion_at_merchant(self, game):
        _to_map(game, game.start(seed="mr8"))
        game.set_player(potions=["FOUL_POTION"], gold=50)
        r = game.act("use_potion", potion_index=0)
        assert r["type"] == "error", "Foul Potion only works at the merchant out of combat"
        state = game.enter_room("shop")
        assert state["decision"] == "shop"
        assert _legal(state, "use_potion")
        state = game.act("use_potion", potion_index=0)
        assert state["player"]["gold"] == 150
        assert state["player"]["potions"] == []

    def test_event_locks_potions(self, game):
        _to_map(game, game.start(seed="mr9"))
        game.set_player(potions=["FIRE_POTION", "BLOOD_POTION"])
        state = game.enter_room("event", event="THE_FUTURE_OF_POTIONS")
        assert state["decision"] == "event_choice"
        assert state["player"]["can_use_or_remove_potions"] is False
        assert not _legal(state, "discard_potion") and not _legal(state, "use_potion")
        assert game.act("discard_potion", potion_index=0)["type"] == "error"
        assert game.act("use_potion", potion_index=1)["type"] == "error"


class TestContinueSave:
    def test_mid_combat_save_resumes_same_encounter(self, tmp_path):
        path = tmp_path / "mid.save"
        g = Game()
        try:
            state = _to_map(g, g.start(seed="e1"))
            c = state["choices"][0]
            state = g.act("select_map_node", col=c["col"], row=c["row"])
            assert state["decision"] == "combat_play"
            enemies = [(e["name"], e["max_hp"]) for e in state["enemies"]]
            r = g.send({"cmd": "write_continue_save", "path": str(path)})
            assert r["success"] is True
        finally:
            g.close()
        g = Game()
        try:
            state = g.send({"cmd": "load_save", "path": str(path)})
            assert state["decision"] == "combat_play", state
            assert [(e["name"], e["max_hp"]) for e in state["enemies"]] == enemies
            assert state["context"]["floor"] == 2
        finally:
            g.close()

    def test_save_after_victory_keeps_rewards(self, tmp_path):
        path = tmp_path / "won.save"
        g = Game()
        try:
            state = _to_map(g, g.start(seed="e2"))
            c = state["choices"][0]
            state = g.fight(g.act("select_map_node", col=c["col"], row=c["row"]))
            assert state["decision"] == "rewards"
            rewards = [(r["type"], r.get("amount"), r.get("id")) for r in state["rewards"]]
            state = g.open_card_reward(state)
            assert state["decision"] == "card_reward"
            cards = [x["name"] for x in state["cards"]]
            state = g.act("skip_card_reward")  # back to the rewards screen, nothing taken
            assert state["decision"] == "rewards"
            gold = state["player"]["gold"]
            assert g.send({"cmd": "write_continue_save", "path": str(path)})["success"]
        finally:
            g.close()
        g = Game()
        try:
            state = g.send({"cmd": "load_save", "path": str(path)})
            assert state["decision"] == "rewards", state
            assert [(r["type"], r.get("amount"), r.get("id")) for r in state["rewards"]] == rewards
            assert state["player"]["gold"] == gold
            state = g.open_card_reward(state)
            assert [x["name"] for x in state["cards"]] == cards
            state = g.act("skip_card_reward")
            assert state["decision"] == "rewards"
            state = g.act("proceed")
            assert state["decision"] == "map_select"
        finally:
            g.close()


class TestDoubleBoss:
    def test_a10_fights_both_final_bosses(self, game):
        """Ascension 10: after the first act-3 boss the map opens for the second boss, and the
        run is only won once that one dies. Ancient start nodes can't be skipped and heal once."""
        rng = random.Random(1)
        state = game.start(seed="x1", ascension=10)
        state = _to_map(game, state)
        game.set_player(max_hp=2000, hp=2000, deck=["BLUDGEON"] * 6 + ["DEMON_FORM"] * 2)
        bosses, seen = [], []
        for _ in range(80):
            if state["decision"] == "game_over":
                break
            assert state["decision"] == "map_select", state
            ctx = state["context"]
            choices = state["choices"]
            if ctx["floor"] == 0:
                # Start of acts 2/3: only the Ancient; it heals 80% of missing HP (A2+), once.
                assert [c["type"] for c in choices] == ["Ancient"]
                kid = game.get_map()["start"]["children"][0]
                assert game.act("select_map_node", col=kid["col"], row=kid["row"])["type"] == "error"
                r = game.set_player(hp=1000)
                mx = r["player"]["max_hp"]
            if [c["type"] for c in choices] == ["Boss"]:
                bosses.append((ctx["act"], choices[0]["row"]))
            pick = rng.choice([c for c in choices if c["type"] != "Elite"] or choices)
            state = game.act("select_map_node", col=pick["col"], row=pick["row"])
            if ctx["floor"] == 0:
                assert abs(state["player"]["hp"] - (1000 + (mx - 1000) * 0.8)) <= 1, state["player"]["hp"]
            state = _finish_rooms(game, state, seen=seen)
        assert state["decision"] == "game_over" and state["victory"] is True, state
        # The run is won through The Architect's event after the second boss (EnterNextAct).
        assert seen[-1] == ("event_choice", "THE_ARCHITECT"), seen[-5:]
        assert state["context"]["second_boss"]["id"]
        act3 = [row for act, row in bosses if act == 3]
        assert len(act3) == 2 and act3[1] == act3[0] + 1, bosses
