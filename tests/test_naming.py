from inspection.naming import is_same_hostname, parse


def test_parse_simple():
    p = parse("SH-MH-401-C11U3-H3CS9825-G0-A04008")
    assert p is not None
    assert p.city == "SH"
    assert p.vendor == "h3c"
    assert p.model == "S9825"
    assert p.suffix == "A04008"


def test_parse_multi_dash_suffix():
    p = parse("SH-MH-601-C02U43-H3CS6850-C0-IntCPU-11212")
    assert p is not None
    assert p.suffix == "IntCPU-11212"
    assert p.vendor == "h3c"
    assert p.model == "S6850"


def test_hostname_compare_truncated():
    assert is_same_hostname("SH-MH-401-C11U3-H3CS9825-G0-A04008", "SH-MH-401-C11U")
    assert not is_same_hostname("foo", "bar")
