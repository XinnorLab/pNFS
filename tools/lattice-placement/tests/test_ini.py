# SPDX-License-Identifier: MIT
from lattice_placement.ini import IniDocument

SAMPLE = """# pnfs-mds configuration
mds_id = 2
hostname=192.168.65.225
[cluster]
cluster_bind_addr = 192.168.65.225
; a comment with ; and = signs = kept
ds_capacity_poll_ms = 10000
ds_capacity_poll_ms = 10000
placement_policy_enabled = true
placement_policy = wrr
ds_weight.0 = 55
ds_weight.1 = 45
junk line without an equals sign
key_with_eq = a=b=c
"""


def test_round_trip_is_byte_identical():
    doc = IniDocument.parse(SAMPLE)
    assert doc.render() == SAMPLE
    doc2 = IniDocument.parse(SAMPLE.rstrip("\n"))
    assert doc2.render() == SAMPLE.rstrip("\n")
    assert IniDocument.parse("").render() == ""


def test_grammar_matches_config_c():
    doc = IniDocument.parse(SAMPLE)
    assert doc.get("mds_id") == "2"
    assert doc.get("hostname") == "192.168.65.225"           # no spaces around '='
    assert doc.get("cluster_bind_addr") == "192.168.65.225"  # sections are skipped, keys after them still count
    assert doc.get("key_with_eq") == "a=b=c"                 # first '=' splits
    assert doc.get("junk") is None
    assert doc.count("ds_capacity_poll_ms") == 2
    assert doc.get("missing") is None
    kinds = [l.kind for l in doc.lines]
    assert kinds[0] == "comment" and kinds[3] == "section" and kinds[5] == "comment"
    assert "junk" in kinds


def test_last_key_wins_and_prefixed():
    doc = IniDocument.parse("a = 1\na = 2\nds_weight.0 = 5\nds_weight.1 = 7\nds_weight. = 9\n")
    assert doc.get("a") == "2"
    assert doc.effective()["a"] == "2"
    assert doc.prefixed("ds_weight.") == {"0": "5", "1": "7"}   # an empty suffix is not a key


def test_remove_keys_keeps_everything_else():
    doc = IniDocument.parse(SAMPLE)
    removed = doc.remove_keys(["placement_policy", "placement_policy_enabled"], ["ds_weight."])
    assert removed == ["placement_policy_enabled", "placement_policy", "ds_weight.0", "ds_weight.1"]
    out = doc.render()
    assert "placement_policy" not in out and "ds_weight" not in out
    assert "; a comment with ; and = signs = kept" in out
    assert out.count("ds_capacity_poll_ms = 10000") == 2
    assert "junk line without an equals sign" in out


def test_managed_block_append_and_replace():
    doc = IniDocument.parse("mds_id = 1\n")
    b, e = "# lattice-placement managed block", "# end lattice-placement managed block"
    doc.replace_managed_block(b, e, [("placement_mode", "fill"), ("placement_min_free_bytes", "1")])
    out = doc.render()
    assert out == ("mds_id = 1\n\n" + b + "\nplacement_mode = fill\nplacement_min_free_bytes = 1\n" + e + "\n")
    assert doc.managed_pairs(b, e) == [("placement_mode", "fill"), ("placement_min_free_bytes", "1")]
    doc.replace_managed_block(b, e, [("placement_mode", "smart")])
    out = doc.render()
    assert out.count(b) == 1 and out.count(e) == 1
    assert "placement_min_free_bytes" not in out
    assert doc.get("placement_mode") == "smart"
    doc.replace_managed_block(b, e, [])
    assert doc.render() == "mds_id = 1\n"


def test_unterminated_block_runs_to_the_end():
    b, e = "# lattice-placement managed block", "# end lattice-placement managed block"
    doc = IniDocument.parse("x = 1\n" + b + "\nplacement_mode = rr\n")
    assert doc.managed_block(b, e) == (1, 3)
    doc.replace_managed_block(b, e, [("placement_mode", "fill")])
    assert doc.render() == "x = 1\n\n" + b + "\nplacement_mode = fill\n" + e + "\n"
