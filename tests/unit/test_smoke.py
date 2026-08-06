from crossfoot import __version__


def test_version_is_three_part() -> None:
    assert len(__version__.split(".")) == 3
