"""--map default plumbing: the fix for `reaperd.py groove` forwarding --map to
groovegen. DSL @map always wins; --map only fills in when @map is omitted."""
from drumgen.groovekit import build

NO_MAP = "@tempo 144\n[v] bars=1 feel=mf\ngrid 16\nkick | x...x...x...x... |\n"
WITH_MAP = "@tempo 144\n@map GM Standard\n[v] bars=1 feel=mf\ngrid 16\nkick | x...x...x...x... |\n"


def test_default_map_used_when_dsl_omits_at_map():
    events, info = build(NO_MAP, seed=1, default_map="RS Monarch")
    assert info["map"] == "RS Monarch"
    assert events


def test_dsl_at_map_wins_over_default():
    events, info = build(WITH_MAP, seed=1, default_map="RS Monarch")
    assert info["map"] == "GM Standard"      # explicit @map beats the default
