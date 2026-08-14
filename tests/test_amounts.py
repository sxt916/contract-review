from decimal import Decimal

from contract_review.amounts import chinese_money_to_decimal, money


def test_round_half_up():
    assert money(Decimal("1.005")) == Decimal("1.01")


def test_chinese_uppercase_amounts():
    assert chinese_money_to_decimal("人民币壹万贰仟叁佰肆拾伍元陆角柒分") == Decimal("12345.67")
    assert chinese_money_to_decimal("壹佰元整") == Decimal("100.00")

