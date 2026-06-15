"""Default routing for secretary-created tasks."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .backends import BackendError, IssueBackend, IssueRef

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoutedTask:
    parent: IssueRef
    child: IssueRef | None = None
    warning: str | None = None

    @property
    def coordinated(self) -> bool:
        return self.child is not None and self.warning is None

    @property
    def degraded(self) -> bool:
        return self.warning is not None


def should_use_two_squad_routing(
    *,
    explicit_assignee: str | None,
    default_assignee: str,
    execution_assignee: str,
) -> bool:
    return (
        not explicit_assignee
        and bool(default_assignee.strip())
        and bool(execution_assignee.strip())
    )


def _ref_text(ref: IssueRef) -> str:
    return ref.identifier or ref.id


def build_execution_description(
    *,
    title: str,
    description: str | None,
    parent: IssueRef,
    lead_assignee: str,
) -> str:
    parts = [
        f"Execution task for parent {_ref_text(parent)}: {title}",
        "",
        "Original request:",
        description.strip() if description and description.strip() else title,
        "",
        "Collaboration contract:",
        f"- Lead/audit squad: {lead_assignee}",
        "- Executor squad: this issue assignee",
        "- Executor posts commands, actions, findings, and evidence back to the parent issue.",
        "- Lead/audit squad reviews the execution result and owns the final user-facing closeout.",
    ]
    return "\n".join(parts)


def build_coordination_comment(
    *,
    parent: IssueRef,
    child: IssueRef,
    lead_assignee: str,
    execution_assignee: str,
) -> str:
    return "\n".join(
        [
            "[secretary-two-squad-routing]",
            f"Lead/audit squad: {lead_assignee}",
            f"Execution squad: {execution_assignee}",
            f"Execution issue: {_ref_text(child)}",
            "",
            "Workflow:",
            "- Lead/audit squad owns the parent task, reviews progress, and closes out to the user.",
            "- Execution squad performs the work in the child issue.",
            "- Commands, actions, findings, blockers, and evidence must be posted back here.",
        ]
    )


def build_routing_failure_comment(*, step: str, detail: str) -> str:
    return "\n".join(
        [
            "[secretary-two-squad-routing]",
            f"Routing failed at step: {step}",
            f"Reason: {detail}",
            "The parent task was created, but two-squad coordination is degraded.",
        ]
    )


async def create_secretary_task(
    backend: IssueBackend,
    *,
    title: str,
    description: str | None,
    priority: str | None,
    explicit_assignee: str | None,
    default_assignee: str,
    execution_assignee: str,
    on_behalf_of: str | None,
) -> RoutedTask:
    """Create a secretary task, optionally splitting lead and execution work.

    Parent creation errors intentionally propagate so callers can reuse their
    existing tracker-error handling. Child/comment errors are converted to a
    visible degraded result; a created parent without routing proof must not be
    reported as fully successful.
    """
    use_two_squad = should_use_two_squad_routing(
        explicit_assignee=explicit_assignee,
        default_assignee=default_assignee,
        execution_assignee=execution_assignee,
    )
    lead_assignee = explicit_assignee or (default_assignee.strip() if default_assignee else None)
    parent = await backend.create_issue(
        title=title,
        description=description,
        priority=priority,
        assignee=lead_assignee,
        on_behalf_of=on_behalf_of,
    )
    if not use_two_squad:
        return RoutedTask(parent=parent)

    lead_ref = default_assignee.strip()
    executor_ref = execution_assignee.strip()
    try:
        child = await backend.create_issue(
            title=f"Execute: {title}",
            description=build_execution_description(
                title=title,
                description=description,
                parent=parent,
                lead_assignee=lead_ref,
            ),
            priority=priority,
            assignee=executor_ref,
            parent=parent.id,
            on_behalf_of=on_behalf_of,
        )
    except BackendError as exc:
        detail = str(exc)
        logger.warning(
            "two-squad routing failed",
            extra={"step": "child_create", "parent": parent.id, "detail": detail},
        )
        await _try_add_failure_comment(
            backend,
            parent=parent,
            step="child_create",
            detail=detail,
            on_behalf_of=on_behalf_of,
        )
        return RoutedTask(parent=parent, warning="execution child task was not created")

    try:
        await backend.add_comment(
            parent.id,
            build_coordination_comment(
                parent=parent,
                child=child,
                lead_assignee=lead_ref,
                execution_assignee=executor_ref,
            ),
            on_behalf_of=on_behalf_of,
        )
    except BackendError as exc:
        detail = str(exc)
        logger.warning(
            "two-squad routing failed",
            extra={"step": "coordination_comment", "parent": parent.id, "detail": detail},
        )
        return RoutedTask(parent=parent, child=child, warning="coordination comment was not posted")

    return RoutedTask(parent=parent, child=child)


async def _try_add_failure_comment(
    backend: IssueBackend,
    *,
    parent: IssueRef,
    step: str,
    detail: str,
    on_behalf_of: str | None,
) -> None:
    try:
        await backend.add_comment(
            parent.id,
            build_routing_failure_comment(step=step, detail=detail),
            on_behalf_of=on_behalf_of,
        )
    except BackendError as exc:
        logger.warning(
            "two-squad routing failure comment failed",
            extra={"step": step, "parent": parent.id, "detail": str(exc)},
        )
