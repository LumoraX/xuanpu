"""F12真实真人并发、冲突裁决及确认响应丢失后的幂等重试。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import httpx

from .f11_process_helpers import observed_process, port_closed, read_original, signed_origin
from .f12_process_helpers import (
    EXECUTIONS,
    approval_plan,
    assert_manager_advance,
    document_decisions,
    execution_body,
    external_counts,
    final_human_review,
    lost_response_proxy,
    preserved_record,
    process_case,
    request_identity,
    single_execution,
    workflow_facts,
)
from .test_f0_e2e import SignedClient
from .test_f4_maintenance import maintenance_database as maintenance_database  # noqa: F401
from .test_f7_process import receive_and_submit
from .test_process import wait_for_ready
from .test_storage import WHO

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


async def test_eight_human_confirmations_same_plan_execute_once(
    maintenance_database: Any, tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    """八个唯一nonce真人签名同时执行同计划同键，换键再执行也只产生一次决定。"""
    async with process_case(maintenance_database, tmp_path/'concurrent') as case:
        async with observed_process(case.path, case.outcomes):
            await wait_for_ready(case.signed.client)
            doc = await receive_and_submit(case.signed, case.raw)
            case.payloads.clear()
            plan = await approval_plan(case, doc['id'])
            before = await workflow_facts(case, doc['id'])
            responses = await asyncio.gather(*[
                case.signed.call('POST', EXECUTIONS, body=execution_body(plan),
                                 user='manager', origin='human', idem='eight-human') for _ in range(8)])
            assert [response.status_code for response in responses] == [202]*8
            identities = [request_identity(response, case.signed) for response in responses]
            assert {identity['origin'] for identity in identities} == {'human'}
            assert len({identity['jti'] for identity in identities}) == 8
            assert len({identity['call_id'] for identity in identities}) == 8
            assert {identity['idempotency_key'] for identity in identities} == {'eight-human'}
            assert len({identity['request_sha256'] for identity in identities}) == 1
            operations = {response.json()['id'] for response in responses}
            assert len(operations) == 1
            operation = next(iter(operations))
            replay = await case.signed.call('POST', EXECUTIONS, body=execution_body(plan),
                                            user='manager', origin='human', idem='different-human-key')
            assert replay.status_code == 202 and replay.json()['id'] == operation
            assert signed_origin(replay, case.signed) == 'human'
            after = await workflow_facts(case, doc['id'])
            assert_manager_advance(before, after)
            facts = await single_execution(case, doc['id'], plan, operation)
            final = await final_human_review(case, doc['id'])
            assert case.downloads == ['pdf']
    request.node.user_properties.append(('f12_hitl', json.dumps({
        'scenario': 'eight_humans_same_plan', 'http_statuses': [r.status_code for r in responses],
        'signed_requests': identities, 'same_operation_id': operation,
        'changed_idempotency_replay': replay.json()['id'], 'before': before, 'after': after,
        'database': facts, 'final_review': final, 'download_count': len(case.downloads),
        'processes': case.outcomes, 'finance_port_closed': port_closed(case.config.port),
        'gateway_port_closed': port_closed(case.gateway_port)}, ensure_ascii=False)))


async def test_human_approve_and_reject_same_version_have_one_winner(
    maintenance_database: Any, tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    """两个有效预检计划争用同一步骤版本，失败动作不重试且PG只接受一个决定。"""
    async with process_case(maintenance_database, tmp_path/'opposite') as case:
        async with observed_process(case.path, case.outcomes):
            await wait_for_ready(case.signed.client)
            doc = await receive_and_submit(case.signed, case.raw)
            case.payloads.clear()
            plans = [await approval_plan(case, doc['id'], name) for name in ['approve', 'reject']]
            assert plans[0]['id'] != plans[1]['id']
            assert plans[0]['target_version'] == plans[1]['target_version']
            before = await workflow_facts(case, doc['id'])
            responses = await asyncio.gather(*[
                case.signed.call('POST', EXECUTIONS, body=execution_body(plan),
                                 user='manager', origin='human', idem='opposite-'+plan['id'])
                for plan in plans])
            statuses = [response.status_code for response in responses]
            assert statuses.count(202) == 1
            assert next(status for status in statuses if status != 202) in (409, 404)
            identities = [request_identity(response, case.signed) for response in responses]
            assert {identity['origin'] for identity in identities} == {'human'}
            winner = statuses.index(202)
            decisions = await document_decisions(case.store, doc['id'])
            assert len(decisions) == 1
            assert decisions[0]['action'] == ['approve', 'reject'][winner]
            assert decisions[0]['data']['plan_id'] == plans[winner]['id']
            after = await workflow_facts(case, doc['id'])
            assert after['workflow']['version'] == before['workflow']['version']+1
            if winner == 0:
                assert_manager_advance(before, after)
            else:
                assert after['claim']['status'] == after['workflow']['status'] == 'rejected'
                assert after['claim']['version'] == before['claim']['version']+1
                assert [step['status'] for step in after['steps']] == ['rejected', 'cancelled']
            facts = await single_execution(case, doc['id'], plans[winner], responses[winner].json()['id'])
            loser_count = await case.store.pool.fetchval(
                'SELECT count(*) FROM finance_business_operation WHERE plan_id=$1', plans[1-winner]['id'])
            assert loser_count == 0
            prefix = '/v1/finance/expense-claims/'+doc['id']
            retained = await read_original(case.signed, prefix, doc, WHO.user_id, case.raw)
            preserved = await preserved_record(case, doc['id'])
            counts = await external_counts(case.store)
            assert case.downloads == ['pdf']
    request.node.user_properties.append(('f12_hitl', json.dumps({
        'scenario': 'opposite_human_decisions', 'http_statuses': statuses,
        'signed_requests': identities, 'winner_action': decisions[0]['action'],
        'before': before, 'after': after, 'database': facts, 'loser_operation_count': loser_count,
        'original': retained, 'record': preserved, 'external_table_counts': counts, 'processes': case.outcomes,
        'finance_port_closed': port_closed(case.config.port),
        'gateway_port_closed': port_closed(case.gateway_port)}, ensure_ascii=False)))


async def test_lost_human_confirmation_response_retries_original_operation_after_restart(
    maintenance_database: Any, tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    """上游真人确认真实202但客户端503，同键新签名及CLI重启后均回原操作且不重决定。"""
    observations: list[dict[str, Any]] = []
    async with process_case(maintenance_database, tmp_path/'lost-response') as case:
        async with observed_process(case.path, case.outcomes):
            await wait_for_ready(case.signed.client)
            doc = await receive_and_submit(case.signed, case.raw)
            case.payloads.clear()
            plan = await approval_plan(case, doc['id'])
            body = execution_body(plan)
            before = await workflow_facts(case, doc['id'])
            agent = await case.signed.call('POST', EXECUTIONS, body=body, user='manager',
                                           origin='agent', idem='lost-human')
            assert agent.status_code == 403 and agent.json()['code'] == 'HUMAN_REQUIRED'
            assert signed_origin(agent, case.signed) == 'agent'
            assert await workflow_facts(case, doc['id']) == before
            async with lost_response_proxy(str(case.signed.client.base_url), observations) as proxy:
                async with httpx.AsyncClient(base_url=proxy) as http:
                    lost = await SignedClient(http, case.signed.key).call(
                        'POST', EXECUTIONS, body=body, user='manager', origin='human', idem='lost-human')
            assert lost.status_code == 503 and observations[0]['upstream_status'] == 202
            assert signed_origin(lost, case.signed) == 'human'
            operation = observations[0]['operation_id']
            retry = await case.signed.call('POST', EXECUTIONS, body=body, user='manager',
                                           origin='human', idem='lost-human')
            assert retry.status_code == 202 and retry.json()['id'] == operation
            after = await workflow_facts(case, doc['id'])
            assert_manager_advance(before, after)
            first = await single_execution(case, doc['id'], plan, operation)
        assert port_closed(case.config.port)
        async with observed_process(case.path, case.outcomes):
            await wait_for_ready(case.signed.client)
            restart = await case.signed.call('POST', EXECUTIONS, body=body, user='manager',
                                             origin='human', idem='lost-human')
            assert restart.status_code == 202 and restart.json()['id'] == operation
            assert await workflow_facts(case, doc['id']) == after
            second = await single_execution(case, doc['id'], plan, operation)
            assert first == second
            final = await final_human_review(case, doc['id'])
            identities = [request_identity(response, case.signed) for response in [lost, retry, restart]]
            assert {identity['origin'] for identity in identities} == {'human'}
            assert len({identity['jti'] for identity in identities}) == 3
            assert case.downloads == ['pdf']
    request.node.user_properties.append(('f12_hitl', json.dumps({
        'scenario': 'human_response_delivery_lost', 'proxy': observations[0],
        'client_status': lost.status_code, 'retry_statuses': [retry.status_code, restart.status_code],
        'signed_requests': identities, 'agent_status': agent.status_code,
        'agent_origin': signed_origin(agent, case.signed), 'database_before_restart': first,
        'database_after_restart': second, 'before': before, 'after': after, 'final_review': final,
        'download_count': len(case.downloads), 'processes': case.outcomes,
        'finance_port_closed': port_closed(case.config.port),
        'gateway_port_closed': port_closed(case.gateway_port)}, ensure_ascii=False)))
