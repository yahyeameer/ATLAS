"""H2 exit gate, part 1: authorization is enforced in the API by token scope (PRD §6, §11)."""

import pytest

from atlas_api.auth import SCOPES, Principal, ScopeError, TokenStore, hash_token, issue
from atlas_api.http import ROUTES, dispatch

from conftest import token_for
from routes import ROUTE_ARGS, ROUTE_SCOPE, with_run

ALL = Principal("all", frozenset(SCOPES))
TOKEN_SCOPES = [[s] for s in SCOPES] + [sorted(SCOPES), []]


@pytest.fixture()
def run_id(service):
    return service.run_backtest(ALL, "trend_pullback", {"start": "2020-10-01", "end": "2021-01-01"})["run_id"]


def test_every_route_has_a_declared_scope():
    assert set(ROUTE_SCOPE) == set(ROUTES)
    assert set(ROUTE_SCOPE.values()) <= set(SCOPES)


def test_no_scope_can_write_risk_trading_or_holdout():
    for scope in SCOPES:
        assert not scope.startswith(("trading", "risk", "holdout", "operations", "emergency")), scope


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("scopes", TOKEN_SCOPES, ids=lambda s: "+".join(s) or "no-scope")
def test_scope_matrix(service, tokens, run_id, route, scopes):
    status, body = dispatch(service, tokens, route, token_for(scopes), with_run(ROUTE_ARGS[route], run_id))
    if ROUTE_SCOPE[route] in scopes:
        assert status == 200, body
    else:
        assert (status, body["code"]) == (403, "forbidden"), body
        assert ROUTE_SCOPE[route] in body["error"]


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("token", [None, "", "atl_not-a-real-token", "Bearer tok-backtest:run"])
def test_unknown_or_missing_token_is_401(service, tokens, route, token):
    status, body = dispatch(service, tokens, route, token, ROUTE_ARGS[route])
    assert (status, body["code"]) == (401, "unauthorized")


def test_refused_calls_do_no_work(service, tokens, loader):
    """A token without the scope never reaches the data loader or the registry."""
    for route, args in ROUTE_ARGS.items():
        if ROUTE_SCOPE[route] != "backtest:run" and ROUTE_SCOPE[route] != "market:read":
            continue
        status, _ = dispatch(service, tokens, route, token_for(["journal:read"]), args)
        assert status == 403
    assert loader.calls == []
    assert service.registry.entries() == []


def test_unknown_route_and_bad_arguments(service, tokens):
    tok = token_for(sorted(SCOPES))
    assert dispatch(service, tokens, "trading/submit", tok, {})[0] == 404
    assert dispatch(service, tokens, "market/bars", tok, {"symbol": "EURUSD"})[0] == 400
    assert dispatch(service, tokens, "market/bars", tok, {**ROUTE_ARGS["market/bars"], "extra": 1})[0] == 400
    assert dispatch(service, tokens, "market/bars", tok, ["not", "a", "dict"])[0] == 400
    assert dispatch(service, tokens, "backtest/summary", tok, {"run_id": "../../etc"})[0] == 400
    assert dispatch(service, tokens, "backtest/summary", tok, {"run_id": "nope"})[0] == 404


def test_backtest_read_token_cannot_spend_budget(service, tokens, run_id):
    """risk-analyst gets backtest:read: it can inspect runs and Monte Carlo, never start one."""
    tok = token_for(["backtest:read"])
    assert dispatch(service, tokens, "backtest/monte_carlo", tok, {"run_id": run_id, "sims": 200})[0] == 200
    before = len(service.registry.entries())
    assert dispatch(service, tokens, "backtest/run", tok, ROUTE_ARGS["backtest/run"])[0] == 403
    assert dispatch(service, tokens, "backtest/walk_forward", tok, ROUTE_ARGS["backtest/walk_forward"])[0] == 403
    assert len(service.registry.entries()) == before


def test_principal_require():
    p = Principal("x", frozenset({"journal:read"}))
    p.require("journal:read")
    with pytest.raises(ScopeError):
        p.require("backtest:run")


def test_token_file_stores_only_hashes(tmp_path):
    path = tmp_path / "tokens.yaml"
    tok = issue(path, "risk-analyst/atlas-backtest", ["backtest:read"])
    text = path.read_text()
    assert tok not in text and hash_token(tok) in text
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    store = TokenStore.load(path)
    assert store.authenticate(tok).scopes == frozenset({"backtest:read"})
    # Re-issuing under the same name revokes the old token.
    tok2 = issue(path, "risk-analyst/atlas-backtest", ["backtest:read"])
    store = TokenStore.load(path)
    assert store.authenticate(tok2).name == "risk-analyst/atlas-backtest"
    with pytest.raises(PermissionError):
        store.authenticate(tok)


def test_unknown_scopes_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        issue(tmp_path / "t.yaml", "x", ["trading:write"])
    with pytest.raises(ValueError):
        TokenStore([{"name": "x", "sha256": hash_token("t"), "scopes": ["holdout:read"]}])
