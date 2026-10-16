"""Tests for map navigation."""
import pytest


class TestMapStructure:
    def test_map_select_fields(self, game):
        state = game.start(seed="ms1")
        state = game.neow(state, 0)
        assert state["decision"] == "map_select"
        assert len(state["choices"]) > 0
        for ch in state["choices"]:
            assert "col" in ch
            assert "row" in ch
            assert "type" in ch

    def test_get_map_full(self, game):
        state = game.start(seed="ms2")
        game.neow(state, 0)
        m = game.get_map()
        assert m["type"] == "map"
        assert "rows" in m
        assert "boss" in m
        assert "current_coord" in m

    def test_context_fields(self, game):
        state = game.start(seed="ms3")
        state = game.neow(state, 0, rewards="proceed")
        ctx = state.get("context", {})
        assert "floor" in ctx
        assert "act_name" in ctx
        assert isinstance(ctx["act_name"], str)

    def test_node_types_valid(self, game):
        state = game.start(seed="ms4")
        state = game.neow(state, 0, rewards="proceed")
        valid = {"Monster", "Elite", "Boss", "RestSite", "Shop",
                 "Treasure", "Event", "Unknown", "Ancient"}
        for ch in state["choices"]:
            assert ch["type"] in valid


class TestMapNavigation:
    def test_select_node(self, game):
        state = game.start(seed="mn1")
        state = game.neow(state, 0, cards=[0])
        pick = state["choices"][0]
        state = game.act("select_map_node", col=pick["col"], row=pick["row"])
        assert state.get("decision") is not None
        assert state["decision"] != "map_select"  # should be in a room now
