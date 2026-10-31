"""Storefront display-locale cookies (``shopping_locale.py``)."""

import pytest

import fetchaller.shopping_locale as shopping_locale_mod
from fetchaller.shopping_locale import amazon_currency, locale_cookies, shopping_locale


def _pin(monkeypatch, country: str, currency: str) -> None:
    monkeypatch.setenv("SHOPPING_COUNTRY", country)
    monkeypatch.setenv("SHOPPING_CURRENCY", currency)


class TestShoppingLocale:
    def test_unset_is_none(self):
        assert shopping_locale() is None

    def test_pinned_and_normalised(self, monkeypatch):
        _pin(monkeypatch, " gb ", "gbp")
        assert shopping_locale() == ("GB", "GBP")

    @pytest.mark.parametrize(
        ("country", "currency"),
        [("GB", ""), ("", "GBP"), ("GBR", "GBP"), ("GB", "POUND"), ("G1", "GBP"), ("GB", "GB$")],
    )
    def test_partial_or_malformed_is_ignored(self, monkeypatch, country, currency):
        monkeypatch.setattr(shopping_locale_mod, "_warned_invalid", False)
        _pin(monkeypatch, country, currency)
        assert shopping_locale() is None


class TestAmazonCurrency:
    @pytest.mark.parametrize(
        ("url", "currency"),
        [
            ("https://www.amazon.co.uk/dp/B0D9LTCDRR", "GBP"),
            ("https://amazon.co.uk/dp/B0D9LTCDRR", "GBP"),
            ("https://www.amazon.com/dp/B0D9LTCDRR", "USD"),
            ("https://www.amazon.ca/dp/B0D9LTCDRR", "CAD"),
            ("https://www.amazon.de/dp/B0D9LTCDRR", "EUR"),
            ("https://www.amazon.co.jp/dp/B0D9LTCDRR", "JPY"),
        ],
    )
    def test_each_marketplace_reads_in_its_own_currency(self, url, currency):
        assert amazon_currency(url) == currency

    @pytest.mark.parametrize(
        "url",
        ["https://www.amazon.jobs/en/", "https://notamazon.co.uk/", "https://amazon.co.uk.evil.example/"],
    )
    def test_lookalikes_are_not_amazon(self, url):
        assert amazon_currency(url) is None
        assert locale_cookies(url) == []


class TestLocaleCookies:
    def test_amazon_cookie_is_scoped_to_the_marketplace(self):
        assert locale_cookies("https://www.amazon.co.uk/dp/B0D9LTCDRR") == [
            "i18n-prefs=GBP; Domain=amazon.co.uk; Path=/"
        ]

    def test_amazon_ignores_the_shopping_locale(self, monkeypatch):
        _pin(monkeypatch, "US", "USD")
        assert locale_cookies("https://www.amazon.co.uk/dp/X") == ["i18n-prefs=GBP; Domain=amazon.co.uk; Path=/"]

    @pytest.mark.parametrize(
        "url",
        ["https://www.aliexpress.com/w/wholesale-ld2450.html", "https://www.alibaba.com/trade/search?SearchText=x"],
    )
    def test_marketplaces_keep_their_own_choice_when_unset(self, url):
        assert locale_cookies(url) == []

    def test_aliexpress_pinned(self, monkeypatch):
        _pin(monkeypatch, "GB", "GBP")
        assert locale_cookies("https://www.aliexpress.com/w/wholesale-ld2450.html") == [
            "aep_usuc_f=site=glo&c_tp=GBP&region=GB&b_locale=en_US; Domain=.aliexpress.com; Path=/"
        ]

    def test_alibaba_pinned(self, monkeypatch):
        _pin(monkeypatch, "GB", "GBP")
        assert locale_cookies("https://www.alibaba.com/product-detail/_1600935428680.html") == [
            "sc_g_cfg_f=sc_b_site=GB&sc_b_locale=en_US&sc_b_currency=GBP; Domain=alibaba.com; Path=/; Secure"
        ]

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.aliexpress.us/item/1.html",
            "https://www.aliexpress.ru/item/1.html",
            "https://aliexpress.com.evil.example/",
            "https://thepihut.com/products/x",
        ],
    )
    def test_other_hosts_get_nothing(self, monkeypatch, url):
        _pin(monkeypatch, "GB", "GBP")
        assert locale_cookies(url) == []
