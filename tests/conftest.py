import pytest

import parser as _parser


@pytest.fixture(autouse=True)
def reset_parser_state():
    """Сбрасывает модульные счётчики между тестами."""
    _parser._failed_ics_dts = 0
    _parser._failed_lw_dates = 0
    _parser._warned_tzids.clear()
    yield
    _parser._failed_ics_dts = 0
    _parser._failed_lw_dates = 0
    _parser._warned_tzids.clear()