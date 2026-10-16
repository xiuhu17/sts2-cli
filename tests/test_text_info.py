"""What a human sees on the card face, the map and the top bar: Replay lines and play counts,
revealed "?" nodes, modifier titles, the run timer, pile-screen card text."""
import random


def _legal(state, action):
    return [a for a in state.get("legal_actions", []) if a["action"] == action]


class TestReplay:
    def test_soldiers_stew_adds_replay_line_and_play_count(self, game):
        for lang, line in (("en", "Replay 1."), ("zh", "重放1。")):
            game.start(seed="stew", lang=lang)
            game.set_player(potions=["SOLDIERS_STEW"], deck=["STRIKE_IRONCLAD"] * 5 + ["DEFEND_IRONCLAD"] * 5)
            s = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
            strike = next(c for c in s["hand"] if c["id"] == "CARD.STRIKE_IRONCLAD")
            assert "play_count" not in strike
            assert "plays" not in strike["damage_by_target"][0]
            s = game.act("use_potion", potion_index=0)
            strike = next(c for c in s["hand"] if c["id"] == "CARD.STRIKE_IRONCLAD")
            assert strike["description"].endswith(line), strike["description"]
            assert strike["play_count"] == 2
            row = strike["damage_by_target"][0]
            assert row["plays"] == 2 and row["total_damage"] == row["damage"] * 2
            defend = next(c for c in s["hand"] if c["id"] == "CARD.DEFEND_IRONCLAD")
            assert "play_count" not in defend


class TestModifiers:
    def test_character_cards_uses_the_characters_title(self, game):
        for lang, name in (("en", "Defect Cards"), ("zh", "故障机器人卡牌")):
            s = game.send({"cmd": "start_run", "character": "Ironclad", "seed": "cc", "lang": lang,
                           "modifiers": ["CHARACTER_CARDS:DEFECT"]})
            mod = s["context"]["modifiers"][0]
            assert mod == {"id": "CHARACTER_CARDS", "name": name, "character": "DEFECT"}, mod
            m = game.get_map()["modifiers"][0]
            assert m["name"] == name and "CHARACTER_CARDS" not in m["description"], m


class TestTopBar:
    def test_context_has_seed_floor_ascension_and_timer(self, game):
        s = game.start(seed="tb", ascension=2)
        ctx = s["context"]
        assert ctx["seed"] == "TB" and ctx["ascension"] == 2 and ctx["total_floor"] == 1
        assert isinstance(ctx["run_time_seconds"], int) and ctx["run_time_seconds"] >= 0


class TestPileText:
    def test_pile_cards_show_calculated_values_like_the_pile_screen(self, game):
        game.start(character="Defect", seed="cd")
        game.set_player(deck=["ZAP"] * 6 + ["COMPILE_DRIVER"] * 4)
        s = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        zap = next(a for a in _legal(s, "play_card") if s["hand"][a["args"]["card_index"]]["id"] == "CARD.ZAP")
        s = game.send({"cmd": "action", **zap})
        assert s.get("orbs")
        texts = [c["description"] for p in ("hand", "draw_pile") for c in s[p] if c["id"] == "CARD.COMPILE_DRIVER"]
        assert texts and all(t.endswith("(Draw 1 card)") for t in texts), texts


class TestRevealedUnknown:
    def test_visited_unknown_nodes_show_their_room_type(self, game):
        """Play into a "?" node (seeded random policy over legal actions, preferring play_card
        in combat and an Unknown node on the map) and check the map reveals it."""
        rng = random.Random(7)
        for seed in ("u2", "u3", "u1"):
            s = game.start(seed=seed)
            game.set_player(max_hp=999, hp=999)
            entered = {}  # (col, row) of a "?" we walked into -> the room_type we landed in
            for _ in range(600):
                d = s["decision"]
                if d == "game_over":
                    break
                if d == "map_select":
                    m = game.get_map()
                    seen = [n for r in m["rows"] for n in r if n["type"] == "Unknown" and n["visited"]
                            and not n["current"]]
                    if seen:
                        for n in seen:
                            assert n["revealed_type"] in ("Monster", "Elite", "Treasure", "Shop", "Event"), n
                            if (n["col"], n["row"]) in entered:
                                assert n["revealed_type"] == entered[(n["col"], n["row"])], (n, entered)
                        assert all("revealed_type" not in n for r in m["rows"] for n in r
                                   if not n["visited"] or n["type"] != "Unknown")
                        return
                    unk = {(c["col"], c["row"]) for c in s["choices"] if c["type"] == "Unknown"}
                    opts = _legal(s, "select_map_node")
                    pick = [a for a in opts if (a["args"]["col"], a["args"]["row"]) in unk] or opts
                    a = rng.choice(pick)
                    s = game.send({"cmd": "action", **a})
                    assert s.get("type") != "error", s
                    coord = (a["args"]["col"], a["args"]["row"])
                    if coord in unk:
                        entered[coord] = s["context"]["room_type"]
                    continue
                elif d == "crystal_sphere":
                    x, y = rng.choice(s["hidden_cells"])
                    a = {"action": "crystal_sphere_click", "args": {"x": x, "y": y, "tool": "big"}}
                else:
                    opts = [a for a in s["legal_actions"] if a["action"] != "discard_potion"]
                    a = rng.choice(_legal(s, "play_card") or opts)
                s = game.send({"cmd": "action", **a})
                assert s.get("type") != "error", s
        raise AssertionError("never reached a visited ? node")
