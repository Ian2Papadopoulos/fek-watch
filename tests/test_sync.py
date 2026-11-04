from fekwatch.sync import merge_watchlists, watchlist_keys

BASE = '''[settings]
lookback_days = 14

[[law]]
act = "ν. 4495/2017"
name = "Δομημένο περιβάλλον"
aliases = []

[[law]]
act = "Π.Δ. 41/2018"
name = "Κανονισμός πυροπροστασίας"
'''

OTHER = '''[[law]]
act = "ν. 4495/2017"
name = "x"

[[law]]
act = "π.δ. 41/2018"
name = "same act, other spelling"

[[law]]
act = "ν. 4759/2020"
name = "Εκσυγχρονισμός χωροταξικής"
aliases = ["Νόμος χωροταξίας"]
'''


def test_union_keeps_base_and_adds_missing():
    out = merge_watchlists(BASE, OTHER)
    assert out.startswith(BASE.rstrip())
    assert watchlist_keys(out) == ["ν. 4495/2017", "π.δ. 41/2018", "ν. 4759/2020"]
    assert 'aliases = ["Νόμος χωροταξίας"]' in out
    import tomllib
    assert len(tomllib.loads(out)["law"]) == 3


def test_union_noop():
    assert merge_watchlists(BASE, BASE) == BASE
