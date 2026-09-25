import pytest

from lattice_placement.profiles import MAX_PROFILES, format_pins, parse_pins


def test_parse_and_format():
    assert parse_pins(" zfs-mvp=sha256:z, xinas-mvp=sha256:p ") == {"xinas-mvp": "sha256:p", "zfs-mvp": "sha256:z"}
    assert format_pins({"zfs-mvp": "z", "xinas-mvp": "p"}) == "xinas-mvp=p,zfs-mvp=z"
    assert format_pins({}) == "-"
    assert MAX_PROFILES == 8


@pytest.mark.parametrize("bad", ["", "x", "a b=d", "a=d,a=e", "a=", "x" * 64 + "=d",
                                 "a\nb=d",
                                 ",".join("p%d=d" % i for i in range(9))])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        parse_pins(bad)
