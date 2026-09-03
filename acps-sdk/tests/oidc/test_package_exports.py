from __future__ import annotations

import builtins
import importlib
import sys

import pytest


def test_oidc_package_keeps_fastapi_helpers_available_without_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import acps_sdk
    import acps_sdk.oidc as oidc_package

    original_oidc_module = sys.modules["acps_sdk.oidc"]
    original_validator_module = sys.modules.get("acps_sdk.oidc.validator")
    real_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "jwt" or name.startswith("jwt."):
            raise ModuleNotFoundError("No module named 'jwt'", name="jwt")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    sys.modules.pop("acps_sdk.oidc", None)
    sys.modules.pop("acps_sdk.oidc.validator", None)

    try:
        reloaded = importlib.import_module("acps_sdk.oidc")

        assert callable(reloaded.audit_actor_from_principal)
        assert callable(reloaded.require_principal)
        assert callable(reloaded.optional_principal)

        with pytest.raises(ModuleNotFoundError, match=r"acps-sdk\[oidc\]"):
            reloaded.OidcTokenValidator()
    finally:
        sys.modules["acps_sdk.oidc"] = original_oidc_module
        if original_validator_module is not None:
            sys.modules["acps_sdk.oidc.validator"] = original_validator_module
        else:
            sys.modules.pop("acps_sdk.oidc.validator", None)
        acps_sdk.oidc = oidc_package
