"""Custom search attributes: the case queue is a Visibility query, not one Query per case.

Each case Workflow upserts CaseStatus / Assignee / Severity whenever they change, plus a memo
(title, host) for display. `soc api` then lists cases with a query such as
    WorkflowType="CaseInvestigationWorkflow" AND Assignee="ben" AND CaseStatus="PARKED"
which the Temporal server answers from its visibility store, in one call, at any scale.
"""

from __future__ import annotations

import logging

from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
from temporalio.client import Client
from temporalio.common import SearchAttributeKey
from temporalio.service import RPCError, RPCStatusCode

CASE_STATUS = SearchAttributeKey.for_keyword("CaseStatus")
ASSIGNEE = SearchAttributeKey.for_keyword("Assignee")
SEVERITY = SearchAttributeKey.for_keyword("Severity")
KEYS = (CASE_STATUS, ASSIGNEE, SEVERITY)


async def ensure_search_attributes(client: Client) -> None:
    """Register the keys (idempotent). On Temporal Cloud, add them in the UI or with tcld."""
    keyword = IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD
    request = AddSearchAttributesRequest(
        namespace=client.namespace, search_attributes={k.name: keyword for k in KEYS}
    )
    try:
        await client.operator_service.add_search_attributes(request)
    except RPCError as err:
        if err.status != RPCStatusCode.ALREADY_EXISTS:
            logging.warning("Could not register search attributes (%s); add them manually", err)


def case_query(status: str = "", assignee: str = "") -> str:
    """The Visibility list filter for the case queue."""
    query = 'WorkflowType="CaseInvestigationWorkflow"'
    if status == "OPEN":
        query += ' AND CaseStatus NOT IN ("SUBMITTED", "AUTO_RESOLVED", "NEEDS_REVIEW")'
    elif status:
        query += f' AND CaseStatus="{status}"'
    if assignee:
        query += f' AND Assignee="{assignee}"'
    return query
