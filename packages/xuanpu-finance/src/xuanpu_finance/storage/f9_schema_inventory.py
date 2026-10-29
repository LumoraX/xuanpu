"""支持的F8固定数据库结构清单：80张表680列，供迁移前只读核对。"""
from __future__ import annotations

# 本清单只随显式支持的结构升级更新，启动时禁止通过迁移或建临时表推导。
REQUIRED_F8_SCHEMA: dict[str, dict[str, str]] = {
    'finance_action_plan': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'target_id': 'text', 'digest': 'text', 'expires_at': 'timestamp with time zone',
        'data': 'jsonb',
    },
    'finance_allocation': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'document_id': 'text',
        'invoice_id': 'text', 'invoice_version': 'integer', 'invoice_line_id': 'text',
        'version': 'integer', 'amount': 'numeric(18,2)', 'tax': 'numeric(18,2)', 'state': 'text',
    },
    'finance_archive': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'target_type': 'text', 'target_id': 'text', 'operation_id': 'text', 'version': 'integer',
        'manifest_hash': 'text', 'source_snapshot_hash': 'text', 'parent_manifest_hash': 'text',
        'snapshot': 'jsonb', 'data': 'jsonb', 'created_at': 'timestamp with time zone',
    },
    'finance_archive_audit': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'archive_id': 'text', 'operation_id': 'text', 'data': 'jsonb',
        'created_at': 'timestamp with time zone',
    },
    'finance_archive_health': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'archive_id': 'text',
        'supplement_required': 'boolean', 'last_audit_status': 'text',
    },
    'finance_archive_supplement': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'archive_id': 'text', 'child_archive_id': 'text', 'source_event_id': 'text',
        'created_at': 'timestamp with time zone',
    },
    'finance_business_idempotency': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'actor_id': 'text', 'operation_id': 'text',
        'key': 'text', 'digest': 'text', 'response': 'jsonb',
    },
    'finance_business_operation': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'plan_id': 'text', 'data': 'jsonb', 'created_at': 'timestamp with time zone',
    },
    'finance_company': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'name': 'text', 'tax_id': 'text',
        'received_at': 'timestamp with time zone',
    },
    'finance_compliance_decision': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'workflow_id': 'text',
        'step_id': 'text', 'actor_id': 'text', 'action': 'text', 'plan_digest': 'text', 'data': 'jsonb',
        'created_at': 'timestamp with time zone',
    },
    'finance_compliance_exception': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'operation_id': 'text', 'rule_id': 'text', 'data': 'jsonb',
        'created_at': 'timestamp with time zone',
    },
    'finance_compliance_grant': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'plan_id': 'text', 'plan_digest': 'text',
        'active': 'boolean',
    },
    'finance_compliance_inbox': {
        'provider': 'text', 'configuration_version': 'integer', 'event_id': 'text', 'raw_hash': 'text',
        'raw': 'bytea', 'status': 'text', 'operation_id': 'text',
        'created_at': 'timestamp with time zone',
    },
    'finance_compliance_inbox_conflict': {
        'id': 'text', 'provider': 'text', 'configuration_version': 'integer', 'event_id': 'text',
        'raw_hash': 'text', 'raw': 'bytea', 'created_at': 'timestamp with time zone',
    },
    'finance_compliance_lease': {
        'lease_token': 'text', 'tenant_id': 'text', 'legal_entity_id': 'text', 'operation_id': 'text',
    },
    'finance_compliance_mode': {
        'singleton': 'boolean', 'simulation': 'boolean',
    },
    'finance_compliance_operation': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'target_type': 'text', 'target_id': 'text', 'plan_id': 'text', 'action': 'text',
        'version': 'integer', 'status': 'text', 'business_result': 'text', 'simulation': 'boolean',
        'provider': 'text', 'configuration_version': 'integer', 'adapter_version': 'text',
        'external_request_id': 'text', 'request_template': 'jsonb', 'request': 'jsonb',
        'lease_token': 'text', 'lease_until': 'timestamp with time zone',
        'next_query_at': 'timestamp with time zone', 'query_count': 'integer',
        'query_requested': 'boolean', 'last_error': 'text', 'created_at': 'timestamp with time zone',
    },
    'finance_compliance_plan': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'actor_id': 'text',
        'owner_id': 'text', 'target_type': 'text', 'target_id': 'text', 'target_version': 'integer',
        'digest': 'text', 'expires_at': 'timestamp with time zone', 'data': 'jsonb',
        'created_at': 'timestamp with time zone',
    },
    'finance_compliance_policy': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'version': 'integer', 'enabled': 'boolean',
        'generation': 'bigint',
    },
    'finance_compliance_policy_revision': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'version': 'integer', 'digest': 'text',
        'data': 'jsonb',
    },
    'finance_compliance_provider': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'provider': 'text',
        'configuration_version': 'integer', 'kind': 'text', 'enabled': 'boolean',
        'generation': 'bigint', 'digest': 'text', 'data': 'jsonb',
    },
    'finance_compliance_receipt': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'operation_id': 'text',
        'raw': 'bytea', 'sha256': 'text', 'data': 'jsonb', 'stale': 'boolean',
        'created_at': 'timestamp with time zone',
    },
    'finance_compliance_workflow': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'plan_id': 'text',
        'version': 'integer', 'status': 'text',
    },
    'finance_compliance_workflow_step': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'workflow_id': 'text',
        'position': 'integer', 'approver_user_id': 'text', 'authority_generation': 'bigint',
        'status': 'text', 'version': 'integer',
    },
    'finance_context_replay': {
        'issuer': 'text', 'jti': 'text', 'subject': 'text', 'request_hash': 'text',
        'idempotency_key': 'text', 'expires_at': 'timestamp with time zone',
    },
    'finance_decision': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'workflow_id': 'text',
        'step_id': 'text', 'actor_id': 'text', 'action': 'text', 'data': 'jsonb',
        'created_at': 'timestamp with time zone',
    },
    'finance_event': {
        'sequence': 'bigint', 'tenant_id': 'text', 'legal_entity_id': 'text', 'aggregate_id': 'text',
        'kind': 'text', 'data': 'jsonb', 'created_at': 'timestamp with time zone',
    },
    'finance_exception': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'operation_id': 'text', 'item_position': 'integer', 'version': 'integer', 'rule_id': 'text',
        'status': 'text', 'receipt_id': 'text', 'resolved_invoice_id': 'text',
        'resolved_operation_id': 'text', 'received_at': 'timestamp with time zone', 'severity': 'text',
    },
    'finance_exception_decision': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'exception_id': 'text',
        'version': 'integer', 'actor_id': 'text', 'data': 'jsonb',
    },
    'finance_exception_evidence': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'exception_id': 'text', 'receipt_id': 'text',
    },
    'finance_execution_grant': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'document_id': 'text',
        'workflow_id': 'text', 'scope': 'text', 'target_version': 'integer',
        'policy_version': 'integer', 'policy_digest': 'text', 'active': 'boolean',
        'policy_generation': 'integer', 'plan_id': 'text', 'plan_digest': 'text',
        'amount_limit': 'numeric(18,2)', 'expires_at': 'timestamp with time zone',
    },
    'finance_expense_claim': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'version': 'integer', 'status': 'text', 'total': 'numeric(18,2)', 'content': 'jsonb',
        'created_at': 'timestamp with time zone', 'record_mode': 'text', 'document_kind': 'text',
        'simulation': 'boolean',
    },
    'finance_expense_evidence': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'document_id': 'text',
        'document_version': 'integer', 'receipt_id': 'text',
    },
    'finance_expense_history': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'version': 'integer',
        'data': 'jsonb', 'created_at': 'timestamp with time zone',
    },
    'finance_expense_policy': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'version': 'integer', 'enabled': 'boolean',
        'generation': 'integer',
    },
    'finance_expense_policy_revision': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'version': 'integer', 'digest': 'text',
        'data': 'jsonb',
    },
    'finance_funding_authority': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'kind': 'text',
        'scope_digest': 'text', 'body_digest': 'text', 'evidence_digest': 'text', 'data': 'jsonb',
        'version': 'integer', 'revoked': 'boolean', 'created_at': 'timestamp with time zone',
    },
    'finance_funding_authority_decision': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'authority_id': 'text',
        'actor_id': 'text', 'expected_version': 'integer', 'reason': 'text',
        'created_at': 'timestamp with time zone',
    },
    'finance_funding_case': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'document_id': 'text', 'authority_id': 'text',
        'source_case_id': 'text', 'financial_version': 'integer',
    },
    'finance_funding_original': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'kind': 'text', 'sha256': 'text',
        'authority_id': 'text',
    },
    'finance_funding_policy': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'version': 'integer', 'enabled': 'boolean',
        'generation': 'integer',
    },
    'finance_funding_policy_revision': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'version': 'integer', 'digest': 'text',
        'data': 'jsonb',
    },
    'finance_funding_reservation': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'document_id': 'text', 'authority_id': 'text',
        'source_case_id': 'text', 'kind': 'text', 'amount': 'numeric(18,2)', 'state': 'text',
        'decision_id': 'text',
    },
    'finance_grant': {
        'tenant_id': 'text', 'user_id': 'text', 'legal_entity_id': 'text', 'role': 'text',
    },
    'finance_grant_epoch': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'user_id': 'text', 'role': 'text',
        'generation': 'integer',
    },
    'finance_idempotency': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'owner_id': 'text', 'key': 'text',
        'request_hash': 'text', 'response': 'jsonb',
    },
    'finance_invoice': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'seller_tax_id': 'text', 'number': 'text', 'issue_date': 'text', 'data': 'jsonb',
        'canonical_digest': 'text', 'received_at': 'timestamp with time zone',
        'source_simulation': 'boolean',
    },
    'finance_invoice_authority_revision': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'invoice_id': 'text', 'provider': 'text',
        'configuration_version': 'integer', 'provider_generation': 'bigint',
        'authority_revision': 'bigint',
    },
    'finance_invoice_compliance': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'invoice_id': 'text', 'version': 'integer',
        'authority_revision': 'bigint', 'verification_status': 'text', 'invoice_status': 'text',
        'valid_until': 'timestamp with time zone', 'snapshot': 'jsonb',
        'verification_evidence': 'jsonb', 'provider': 'text', 'configuration_version': 'integer',
        'provider_generation': 'bigint',
    },
    'finance_invoice_field': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'invoice_id': 'text', 'position': 'integer',
        'data': 'jsonb',
    },
    'finance_invoice_line': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'invoice_id': 'text', 'position': 'integer',
        'data': 'jsonb',
    },
    'finance_invoice_receipt': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'invoice_id': 'text', 'receipt_id': 'text',
    },
    'finance_invoice_verification': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'invoice_id': 'text', 'receipt_id': 'text', 'authority_revision': 'bigint', 'data': 'jsonb',
        'applied': 'boolean', 'created_at': 'timestamp with time zone', 'provider': 'text',
        'configuration_version': 'integer', 'provider_generation': 'bigint',
    },
    'finance_item': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'operation_id': 'text', 'position': 'integer',
        'file_ref': 'jsonb', 'state': 'text', 'receipt_id': 'text', 'object_key': 'text',
        'actual_sha256': 'text', 'content_type': 'text', 'filename': 'text', 'size_bytes': 'bigint',
        'invoice_id': 'text', 'exception_id': 'text', 'error_code': 'text',
    },
    'finance_notification': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'recipient_id': 'text',
        'source_type': 'text', 'source_id': 'text', 'source_revision': 'text', 'source_status': 'text',
        'kind': 'text', 'title': 'text', 'target_type': 'text', 'target_id': 'text',
        'version': 'integer', 'created_at': 'timestamp with time zone',
        'read_at': 'timestamp with time zone',
    },
    'finance_notification_scan': {
        'singleton': 'boolean', 'after_key': 'text',
    },
    'finance_operation': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'idempotency_key': 'text', 'request_hash': 'text', 'version': 'integer', 'status': 'text',
        'business_result': 'text', 'trace_id': 'text', 'source_exception_id': 'text',
        'expected_exception_version': 'integer', 'lease_token': 'text',
        'lease_until': 'timestamp with time zone', 'bytes_received': 'bigint',
        'created_at': 'timestamp with time zone', 'purpose': 'text', 'source_simulation': 'boolean',
    },
    'finance_outbox': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'operation_id': 'text',
        'status': 'text', 'data': 'jsonb', 'created_at': 'timestamp with time zone',
        'inbox_diagnostic': 'text',
    },
    'finance_payment': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'document_id': 'text',
        'operation_id': 'text', 'amount': 'numeric(18,2)', 'version': 'integer', 'status': 'text',
        'simulation': 'boolean', 'data': 'jsonb', 'created_at': 'timestamp with time zone',
    },
    'finance_posting': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'document_id': 'text',
        'operation_id': 'text', 'amount': 'numeric(18,2)', 'version': 'integer', 'status': 'text',
        'simulation': 'boolean', 'original_posting_id': 'text', 'data': 'jsonb',
        'created_at': 'timestamp with time zone',
    },
    'finance_receipt': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'operation_id': 'text', 'position': 'integer', 'object_key': 'text', 'sha256': 'text',
        'file_ref': 'jsonb',
    },
    'finance_red_letter_case': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'invoice_id': 'text', 'external_case_id': 'text', 'version': 'integer',
        'authority_revision': 'bigint', 'status': 'text', 'data': 'jsonb', 'red_invoice_id': 'text',
        'created_at': 'timestamp with time zone', 'provider': 'text',
        'configuration_version': 'integer', 'provider_generation': 'bigint',
    },
    'finance_red_letter_link': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'case_id': 'text',
        'blue_invoice_id': 'text', 'red_invoice_id': 'text', 'actor_id': 'text',
        'amount': 'numeric(18,2)', 'tax': 'numeric(18,2)', 'data': 'jsonb',
        'created_at': 'timestamp with time zone',
    },
    'finance_settlement_catalog': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'kind': 'text', 'source': 'text', 'id': 'text',
        'version': 'text', 'generation': 'integer', 'enabled': 'boolean',
    },
    'finance_settlement_catalog_revision': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'kind': 'text', 'source': 'text', 'id': 'text',
        'version': 'text', 'digest': 'text', 'data': 'jsonb',
    },
    'finance_settlement_exception': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'operation_id': 'text', 'version': 'integer', 'rule_id': 'text', 'status': 'text',
        'severity': 'text', 'evidence': 'jsonb', 'created_at': 'timestamp with time zone',
    },
    'finance_settlement_inbox': {
        'provider': 'text', 'configuration_version': 'integer', 'event_id': 'text', 'raw_hash': 'text',
        'raw': 'bytea', 'status': 'text', 'operation_id': 'text',
        'created_at': 'timestamp with time zone',
    },
    'finance_settlement_inbox_conflict': {
        'id': 'text', 'provider': 'text', 'configuration_version': 'integer', 'event_id': 'text',
        'raw_hash': 'text', 'raw': 'bytea', 'created_at': 'timestamp with time zone',
    },
    'finance_settlement_lease': {
        'lease_token': 'text', 'tenant_id': 'text', 'legal_entity_id': 'text', 'operation_id': 'text',
        'created_at': 'timestamp with time zone',
    },
    'finance_settlement_mode': {
        'singleton': 'boolean', 'simulation': 'boolean',
    },
    'finance_settlement_operation': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'document_id': 'text', 'plan_id': 'text', 'action': 'text', 'target_id': 'text',
        'amount': 'numeric(18,2)', 'status': 'text', 'version': 'integer', 'simulation': 'boolean',
        'provider': 'text', 'configuration_version': 'integer', 'adapter_version': 'text',
        'external_request_id': 'text', 'request': 'jsonb', 'result': 'jsonb', 'lease_token': 'text',
        'lease_until': 'timestamp with time zone', 'query_count': 'integer',
        'next_query_at': 'timestamp with time zone', 'query_requested': 'boolean', 'last_error': 'text',
        'created_at': 'timestamp with time zone', 'document_kind': 'text',
    },
    'finance_settlement_policy': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'version': 'integer', 'enabled': 'boolean',
        'generation': 'integer',
    },
    'finance_settlement_policy_revision': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'version': 'integer', 'digest': 'text',
        'data': 'jsonb',
    },
    'finance_settlement_provider': {
        'provider': 'text', 'configuration_version': 'integer', 'tenant_id': 'text',
        'legal_entity_id': 'text', 'digest': 'text', 'data': 'jsonb', 'enabled': 'boolean',
        'generation': 'integer',
    },
    'finance_settlement_receipt': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'operation_id': 'text',
        'raw': 'bytea', 'sha256': 'text', 'data': 'jsonb', 'created_at': 'timestamp with time zone',
    },
    'finance_tax_adjustment': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'invoice_id': 'text', 'external_adjustment_id': 'text', 'version': 'integer', 'status': 'text',
        'data': 'jsonb', 'created_at': 'timestamp with time zone',
    },
    'finance_tax_adjustment_evidence': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'adjustment_id': 'text',
        'actor_id': 'text', 'data': 'jsonb', 'created_at': 'timestamp with time zone',
    },
    'finance_tax_confirmation': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'owner_id': 'text',
        'invoice_id': 'text', 'operation_id': 'text', 'data': 'jsonb',
        'created_at': 'timestamp with time zone',
    },
    'finance_workflow': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'document_id': 'text',
        'target_version': 'integer', 'version': 'integer', 'status': 'text',
        'policy_version': 'integer', 'policy_digest': 'text', 'due_at': 'timestamp with time zone',
        'policy_generation': 'integer', 'target_type': 'text', 'target_digest': 'text',
        'target_id': 'text',
    },
    'finance_workflow_step': {
        'tenant_id': 'text', 'legal_entity_id': 'text', 'id': 'text', 'workflow_id': 'text',
        'position': 'integer', 'approver_user_id': 'text', 'name': 'text', 'status': 'text',
        'version': 'integer', 'authority_generation': 'integer',
    },
}
