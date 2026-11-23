import pytest

from backend.core.cache import TTLCache
from backend.core.config import Settings

LONG = "x" * 24


def test_production_needs_every_apple_setting(tmp_path):
    key = tmp_path / "AuthKey.p8"
    key.write_text("PEM")
    good = dict(env="production", api_key=LONG, apple_team_id="T", apple_key_id="K", apple_private_key_path=str(key), apple_web_origin="https://komute.ca")
    assert Settings(_env_file=None, **good).apple_ready
    for drop in ("api_key", "apple_team_id", "apple_key_id", "apple_private_key_path", "apple_web_origin"):
        with pytest.raises(ValueError):
            Settings(_env_file=None, **{k: v for k, v in good.items() if k != drop})
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{**good, "api_key": "short"})


def test_blank_values_mean_not_set():
    s = Settings(_env_file=None, api_key="", apple_team_id=" ", apple_key_id="", apple_private_key_path="", apple_private_key=" ", apple_web_origin="")
    assert (s.api_key, s.apple_team_id, s.apple_key_id, s.apple_private_key_path, s.apple_private_key, s.apple_web_origin) == (None,) * 6
    assert not s.apple_ready


def test_key_comes_from_the_file_or_inline_and_an_unreadable_file_is_not_ready(tmp_path):
    f = tmp_path / "k.p8"
    f.write_text("FROM-FILE")
    assert Settings(_env_file=None, apple_team_id="T", apple_key_id="K", apple_private_key_path=str(f)).apple_key_pem() == "FROM-FILE"
    assert Settings(_env_file=None, apple_private_key="INLINE", apple_private_key_path=str(f)).apple_key_pem() == "INLINE"
    missing = Settings(_env_file=None, apple_team_id="T", apple_key_id="K", apple_private_key_path=str(tmp_path / "nope.p8"))
    assert missing.apple_key_pem() is None and not missing.apple_ready


def test_secrets_do_not_appear_in_repr():
    s = Settings(_env_file=None, api_key=LONG, apple_private_key="SECRETPEM")
    assert "SECRETPEM" not in repr(s) and LONG not in repr(s)


def test_cache_expires_and_evicts():
    now = [0.0]
    c = TTLCache(2, clock=lambda: now[0])
    c.set("a", 1, 10)
    c.set("b", 2, 10)
    c.set("c", 3, 10)  # evicts "a"
    assert c.get("a") is None and c.get("b") == 2
    now[0] = 11
    assert c.get("b") is None
    c.set("z", 9, 0)  # ttl 0 = do not cache
    assert c.get("z") is None


@pytest.mark.parametrize("bad", ["komute.ca", "localhost:8765", "https://komute.ca/", "https://komute.ca/app", "ftp://komute.ca", "https://"])
def test_web_origin_must_carry_a_scheme_and_no_path(bad):
    with pytest.raises(ValueError):
        Settings(_env_file=None, apple_web_origin=bad)


@pytest.mark.parametrize("good", ["https://komute.ca", "http://localhost:8765", "https://*.komute.ca"])
def test_web_origin_accepts_scheme_host_port(good):
    assert Settings(_env_file=None, apple_web_origin=good).apple_web_origin == good


def test_countries_and_default_bias_defaults_and_validation():
    s = Settings(_env_file=None)
    assert s.countries_list == ["CA", "US"] and s.default_bias_point == (45.4215, -75.6972)
    assert Settings(_env_file=None, countries="ca, us").countries == "CA,US"
    for bad in ("CAN", "C", "CA;US", ""):
        with pytest.raises(ValueError):
            Settings(_env_file=None, countries=bad)
    for bad in ("45.4", "a,b", "95,10", "45,200"):
        with pytest.raises(ValueError):
            Settings(_env_file=None, default_bias=bad)
