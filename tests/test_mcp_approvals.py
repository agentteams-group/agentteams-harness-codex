"""Only typed, current SDK tool approvals use the dynamic AT server policy."""
import os
from pathlib import Path
import sys
import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
               str(Path(os.environ['AT_SOURCE']) / 'shared/lib')]
from agentteams_codex.mcp_approvals import automatic_native_response, automatic_response, decision


def test_agent_automatic_mode_only_grants_current_native_tool_requests(monkeypatch):
    monkeypatch.delenv('TEAMHARNESS_RUNTIME_CONFIG', raising=False)
    config = {'desired': {'approvalMode': 'allow'}}
    params = {'threadId': 'thread', 'turnId': 'turn'}
    for method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
        assert automatic_native_response(config, method, params, 'thread', 'turn') == {'decision': 'accept'}
        assert automatic_native_response(config, method, params, 'other', 'turn') is None
    for method in ('item/tool/requestUserInput', 'item/permissions/requestApproval', 'mcpServer/elicitation/request'):
        assert automatic_native_response(config, method, params, 'thread', 'turn') is None
    config['desired']['approvalMode'] = 'ask'
    assert automatic_native_response(config, 'item/fileChange/requestApproval', params, 'thread', 'turn') is None


def request(**changes):
    return {'mode':'form', '_meta':{'codex_approval_kind':'mcp_tool_call'},
            'threadId':'thread', 'turnId':'turn', 'serverName':'github', **changes}


def test_live_policy_changes_apply_without_a_new_native_turn(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime.yaml'
    monkeypatch.setenv('TEAMHARNESS_RUNTIME_CONFIG', str(runtime))
    config = {'member':{'runtime':'codex','runtimeName':'developer'}}
    def mode(value):
        runtime.write_text('member:\n  runtime: codex\n  runtimeName: developer\ndesired:\n  mcpServers:\n    - name: github\n      approvals:\n        tools: "*"\n        mode: '+value+'\n')
    mode('ask')
    assert automatic_response(config, request(), 'thread','turn') is None
    mode('allow')
    assert automatic_response(config, request(), 'thread','turn') == {'action':'accept','content':None}
    mode('ask')
    assert automatic_response(config, request(), 'thread','turn') is None


@pytest.mark.parametrize('changes', [{'mode':'url'}, {'_meta':{}}, {'threadId':'other'},
                                    {'turnId':'other'}, {'serverName':'unassigned'}])
def test_other_elicitations_and_foreign_turns_are_not_auto_approved(monkeypatch, changes):
    monkeypatch.delenv('TEAMHARNESS_RUNTIME_CONFIG', raising=False)
    config = {'desired':{'mcpServers':[{'name':'github','approvals':{'tools':'*','mode':'allow'}}]}}
    assert automatic_response(config, request(**changes), 'thread','turn') is None


def test_accept_and_reject_keep_native_elicitation_semantics():
    assert decision(True) == {'action':'accept','content':None}
    assert decision(False) == {'action':'decline','content':None}
