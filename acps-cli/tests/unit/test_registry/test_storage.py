from acps_cli.registry.storage import TokenStore


def test_token_store_load_returns_none_for_invalid_json(tmp_path):
    token_file = tmp_path / "registry-user.json"
    token_file.write_text("{not-json", encoding="utf-8")

    store = TokenStore(token_file)

    assert store.load() is None
