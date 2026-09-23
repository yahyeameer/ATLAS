"""The atlas plugin hooks: audit lines, alerts, token usage (atlas_plugins/hooks.py)."""

import json

import pytest

from atlas_plugins import hooks, store

from opshelp import ops_dir  # noqa: F401

OK = json.dumps({"result": json.dumps({"trading_enabled": False, "already_disabled": False, "source": "simulated"})})
REFUSED = json.dumps({"error": "refused (holdout_refused): the holdout is locked; no agent token can read it"})


def audit():
    return store.read(store.AUDIT)


def alerts():
    return store.read(store.ALERTS)


def test_ignores_non_atlas_tools(ops_dir):
    hooks.on_post_tool_call(tool_name="terminal", args={"command": "ls"}, result="{}")
    hooks.on_post_tool_call(tool_name="mcp__github__create_issue", args={}, result="{}")
    assert audit() == [] and alerts() == []


def test_disable_trading_is_audited_and_alerted(ops_dir, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "operations-monitor")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "atlas-ops")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_42")
    hooks.on_post_tool_call(tool_name="mcp__atlas_operations__disable_trading",
                            args={"reason": "HALT: mt5_disconnected"}, result=OK, session_id="s1", duration_ms=12)
    [a] = audit()
    assert a["profile"] == "operations-monitor" and a["board"] == "atlas-ops" and a["kanban_task"] == "t_42"
    assert a["server"] == "atlas-operations" and a["tool"] == "disable_trading" and a["write"] is True
    assert a["outcome"] == "ok" and a["detail"] == {"trading_enabled": False, "already_disabled": False,
                                                    "source": "simulated"}
    [al] = alerts()
    assert al["severity"] == "critical" and "DISABLED new trading: HALT: mt5_disconnected" in al["text"]


def test_repeat_disable_is_a_warning(ops_dir):
    again = json.dumps({"result": json.dumps({"trading_enabled": False, "already_disabled": True})})
    hooks.on_post_tool_call(tool_name="mcp__atlas_operations__disable_trading", args={"reason": "x"}, result=again)
    assert alerts()[0]["severity"] == "warning" and "already disabled" in alerts()[0]["text"]


def test_reads_are_audited_without_alerts(ops_dir):
    hooks.on_post_tool_call(tool_name="mcp__atlas_backtest__get_run_summary", args={"run_id": "r1"},
                            result=json.dumps({"result": json.dumps({"run_id": "r1", "trades": [1] * 500})}))
    [a] = audit()
    assert a["write"] is False and a["detail"] == {"run_id": "r1"}  # compact: no trade lists in the audit
    assert alerts() == []


def test_refusals_are_audited_and_alerted(ops_dir, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "strategy-researcher")
    hooks.on_post_tool_call(tool_name="mcp__atlas_backtest__run_backtest", args={"strategy": "s", "window": "holdout"},
                            result=REFUSED)
    [a] = audit()
    assert a["outcome"] == "refused" and a["code"] == "holdout_refused" and a["write"] is True
    [al] = alerts()
    assert al["severity"] == "warning" and "strategy-researcher was refused atlas-backtest.run_backtest" in al["text"]


def test_refusal_alerts_can_be_turned_off(ops_dir):
    (ops_dir).mkdir(parents=True, exist_ok=True)
    (ops_dir / "ops.yaml").write_text("alert_on_refusals: false\n")
    hooks.on_post_tool_call(tool_name="mcp__atlas_market__get_bars", args={}, result=REFUSED)
    assert len(audit()) == 1 and alerts() == []


def test_errors_and_huge_args_are_bounded(ops_dir):
    hooks.on_post_tool_call(tool_name="mcp__atlas_market__get_bars", args={"x": "y" * 10_000},
                            result="not json", status="error", error_message="boom")
    [a] = audit()
    assert a["outcome"] == "error" and len(a["args"]) == hooks.MAX_ARGS


def test_hook_never_raises(ops_dir, monkeypatch):
    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(store, "append", broken)
    hooks.on_post_tool_call(tool_name="mcp__atlas_operations__system_status", args={}, result=OK)
    hooks.on_post_api_request(usage={"input_tokens": 1})
    hooks.on_kanban_task_blocked(board="atlas-ops", task_id="t", reason="r")


@pytest.mark.parametrize("result, expected", [
    (OK, ("ok", None)),
    (REFUSED, ("refused", "holdout_refused")),
    (json.dumps({"error": "ATLAS API unreachable at http://x: refused"}), ("error", None)),
    ('<untrusted_tool_result>{"result": "{\\"state\\": \\"HALT\\"}"}</untrusted_tool_result>', ("ok", None)),
])
def test_parse_result(result, expected):
    assert hooks.parse_result(result)[:2] == expected


def test_usage_records_board_and_profile(ops_dir, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "strategy-researcher")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "atlas-research")
    hooks.on_post_api_request(usage={"input_tokens": 1200, "output_tokens": 300, "cache_read_tokens": 800},
                              model="claude-sonnet-5", provider="anthropic", session_id="s")
    hooks.on_post_api_request(usage=None)
    [u] = store.read(store.USAGE)
    assert (u["profile"], u["board"], u["input_tokens"], u["output_tokens"]) == \
        ("strategy-researcher", "atlas-research", 1200, 300)


def test_kanban_alerts_only_for_atlas_ops(ops_dir):
    hooks.on_kanban_task_blocked(task_id="t1", board="atlas-research", assignee="risk-analyst", reason="needs input")
    hooks.on_kanban_task_completed(task_id="t2", board="atlas-research", assignee="x", summary="done")
    assert alerts() == []
    hooks.on_kanban_task_blocked(task_id="t3", board="atlas-ops", assignee="operations-monitor",
                                 reason="Operator: re-enable after MT5 reconnects")
    hooks.on_kanban_task_completed(task_id="t4", board="atlas-ops", assignee="operations-monitor", summary="cleared")
    a = alerts()
    assert [x["severity"] for x in a] == ["warning", "info"]
    assert "t3" in a[0]["text"] and "re-enable after MT5 reconnects" in a[0]["text"]


def test_worker_crash_alerts_on_any_board(ops_dir):
    hooks.on_kanban_worker_exited(task_id="t9", board="atlas-research", assignee="jev-analyst", exit_kind="signal",
                                  exit_code=-9, outcome="crashed", retry_status="retrying")
    assert "jev-analyst worker on atlas-research card t9 exited" in alerts()[0]["text"]


def test_register_wires_every_hook():
    registered = {}

    class Ctx:
        def register_hook(self, name, fn):
            registered[name] = fn

    hooks.register(Ctx())
    assert registered == hooks.HOOKS
    # Every hook the plugin manifest declares is registered, and only those.
    import yaml
    from pathlib import Path
    manifest = yaml.safe_load((Path(hooks.__file__).parent / "hermes-plugin" / "plugin.yaml").read_text())
    assert set(manifest["provides_hooks"]) == set(registered)


def test_hook_names_exist_in_pinned_hermes():
    pytest.importorskip("hermes_cli")
    from hermes_cli.plugins import VALID_HOOKS
    assert set(hooks.HOOKS) <= set(VALID_HOOKS)
