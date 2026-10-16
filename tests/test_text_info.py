"""What a human sees on the card face, the map and the top bar: Replay lines and play counts,
revealed "?" nodes, modifier titles, the run timer, pile-screen card text."""
import random


def _legal(state, action):
    return [a for a in state.get("legal_actions", []) if a["action"] == action]


def _concrete(a, rng):
    """A legal entry as a command: a {"choose": ...} selection becomes a seeded random pick of
    min..max of its indices, a {"choose_cell": ...} a random listed cell and tool."""
    if "choose" in a:
        c = a["choose"]
        k = rng.choice(c["counts"]) if c.get("counts") else rng.randint(c["min"], c["max"])
        picks = sorted(rng.sample(c["from"], k))
        return {"action": a["action"], "args": {"indices": ",".join(map(str, picks))}}
    if "choose_cell" in a:
        x, y = rng.choice(a["choose_cell"]["cells"])
        return {"action": a["action"], "args": {"x": x, "y": y, "tool": rng.choice(a["choose_cell"]["tools"])}}
    return {"action": a["action"], **({"args": a["args"]} if "args" in a else {})}


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
                else:
                    opts = [a for a in s["legal_actions"] if a["action"] != "discard_potion"]
                    a = _concrete(rng.choice(_legal(s, "play_card") or opts), rng)
                s = game.send({"cmd": "action", **a})
                assert s.get("type") != "error", s
        raise AssertionError("never reached a visited ? node")


class TestEnchantedCardLists:
    """NCard.UpdateVisuals refreshes the card's and its enchantment's previews in every card
    list, so an enchantment's numbers (Sharp's +damage, Nimble's +block) show outside combat."""

    def _sharp_perfected_strike(self, game, lang="en"):
        game.start(seed="e1", lang=lang)
        game.set_player(deck=["PERFECTED_STRIKE"] + ["DEFEND_IRONCLAD"] * 3, gold=999)
        game.enter_room("event", event="SELF_HELP_BOOK")
        s = game.act("choose_option", option_index=0)  # enchants Perfected Strike with Sharp 2
        assert any(e["type"] == "deck_changed" for e in s.get("events", [])), s.get("events")
        return s

    @staticmethod
    def _ps(cards):
        return next(c for c in cards if c["id"] == "CARD.PERFECTED_STRIKE")

    def test_deck_views(self, game):
        s = self._sharp_perfected_strike(game, lang="zh")
        for card in (self._ps(game.send({"cmd": "get_deck"})["cards"]), self._ps(s["player"]["deck"])):
            assert card["enchantment"] == "锋利" and card["enchantment_amount"] == 2
            assert card["description"].startswith("造成8点伤害"), card
            assert card["stats"]["calculateddamage"] == 8
        up = self._ps(s["player"]["deck"])["after_upgrade"]
        assert up["stats"]["calculateddamage"] == 8 and up["stats"]["extradamage"] == 3

    def test_smith_and_shop_removal(self, game):
        self._sharp_perfected_strike(game)
        s = game.enter_room("rest_site")
        smith = next(o["index"] for o in s["options"] if "SMITH" in str(o.get("id", "")).upper()
                     or o.get("name") == "Smith")
        s = game.act("choose_option", option_index=smith)
        assert s["decision"] == "card_select"
        ps = self._ps(s["cards"])
        assert ps["description"].startswith("Deal 8 damage") and ps["enchantment"] == "Sharp"
        assert ps["enchantment_amount"] == 2 and ps["stats"]["calculateddamage"] == 8
        self._sharp_perfected_strike(game)
        s = game.enter_room("shop")
        s = game.act("remove_card")
        assert s["decision"] == "card_select"
        ps = self._ps(s["cards"])
        assert ps["description"].startswith("Deal 8 damage") and ps["enchantment"] == "Sharp"

    def test_fresnel_lens_shop_and_card_reward(self, game):
        game.start(seed="f1")
        game.set_player(relics=["FRESNEL_LENS"], gold=999)
        s = game.enter_room("shop")
        iw = next(c for c in s["cards"] if c["id"] == "CARD.IRON_WAVE")
        assert iw["enchantment"] == "Nimble" and iw["enchantment_amount"] == 2
        assert iw["stats"]["block"] == 7 and "Gain 7 Block" in iw["description"]  # 5 + Nimble 2
        game.start(seed="fr4")
        game.set_player(relics=["FRESNEL_LENS"], deck=["STRIKE_IRONCLAD"] * 5, hp=80)
        s = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        for _ in range(40):
            if s["decision"] != "combat_play":
                break
            i = next((c["index"] for c in s["hand"] if c["can_play"]), None)
            s = game.act("play_card", card_index=i, target_index=0) if i is not None else game.act("end_turn")
        s = game.act("claim_reward", reward_index=next(r["index"] for r in s["rewards"] if r["type"] == "card"))
        assert s["decision"] == "card_reward"
        bw = next(c for c in s["cards"] if c["id"] == "CARD.BLOOD_WALL")
        assert bw["enchantment"] == "Nimble" and bw["stats"]["block"] == 18 and "Gain 18 Block" in bw["description"]

    def test_enchantment_extra_text_in_hand(self, game):
        """In hand the enchantment's own vars are previewed too: Adroit's extra 'Gain 3 Block'
        line includes Dexterity, as NCard.UpdateVisuals shows it."""
        game.start(seed="ad1")
        game.set_player(deck=["DEFEND_IRONCLAD"] * 5, potions=["DEXTERITY_POTION"])
        s = game.send({"cmd": "set_player", "obtain_relic": "KIFUDA"})  # Adroit 3 on chosen cards
        assert s["decision"] == "card_select"
        game.act("select_cards", indices="0")
        adroit = next(c for c in game.send({"cmd": "get_deck"})["cards"] if c.get("enchantment"))
        assert adroit["enchantment"] == "Adroit" and adroit["description"] == "Gain 5 Block.\nGain 3 Block."
        game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        s = game.act("use_potion", potion_index=0)  # +2 Dexterity
        card = next(c for c in s["hand"] if c.get("enchantment") == "Adroit")
        assert card["description"] == "Gain 7 Block.\nGain 5 Block.", card
        assert all(c["description"] == "Gain 7 Block." for c in s["hand"] if not c.get("enchantment"))
