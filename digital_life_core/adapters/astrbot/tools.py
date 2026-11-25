"""Map trusted AstrBot catalog declarations to host-neutral supervised tools.

The production host supplies this port after authenticating/loading the package.
An arbitrary function-manager entry is not sufficient permission to expose a tool.
Identity, role and routing come exclusively from invocation.context, never arguments.
"""

from dataclasses import dataclass, replace
import json
import inspect
from typing import Protocol

from digital_life_core.conversation.external_tools import (
    ExtensionToolDescriptor, ExternalToolExecutor, ExternalToolInvocation, ExternalToolProvider, ExternalToolResponse,
)
from digital_life_core.execution.models import TaskError, digest, encode
from digital_life_core.conversation.tool_failures import retry_disposition_for
from digital_life_core.foundation.refs import SourceRef, VersionRef
from digital_life_core.identity_policy.models import _opaque
from digital_life_core.sources.models import SourceRecord


@dataclass(frozen=True)
class AstrBotToolDeclaration:
    host_tool_id: str
    descriptor: ExtensionToolDescriptor

    def __post_init__(self):
        _opaque(self.host_tool_id, field="host tool ID")
        if not isinstance(self.descriptor, ExtensionToolDescriptor):
            raise ValueError("host tool requires an attested extension descriptor")


class AstrBotToolPort(Protocol):
    def declarations(self) -> tuple[AstrBotToolDeclaration, ...]:
        """Return bounded, trusted, fully loaded cooperative providers."""

    def current_declaration(self, host_tool_id: str) -> AstrBotToolDeclaration | None:
        """Bounded synchronous check; unloading or replacing a provider changes this result."""

    async def invoke(self, host_tool_id: str, invocation: ExternalToolInvocation) -> ExternalToolResponse:
        """Use the explicit authenticated context and retain original effect receipts."""


class CompositeAstrBotToolPort:
    """Expose several independently attested tool ports through one catalog.

    The core receives one executor, while each provider keeps its own declaration,
    binding and lifecycle.  A missing optional provider therefore contributes no
    declaration at all and cannot create a half-installed tool.
    This installation directory is independent of any one model request's budget.
    """

    def __init__(self, *ports):
        self.ports = tuple(port for port in ports if port is not None)
        declarations = tuple(item for port in self.ports for item in port.declarations())
        if len({item.host_tool_id for item in declarations}) != len(declarations):
            raise ValueError("composed host tool declarations must have unique identities")
        self._declarations = declarations

    def declarations(self):
        return self._declarations

    def current_declaration(self, host_tool_id):
        for port in self.ports:
            current = port.current_declaration(host_tool_id)
            if current is not None:
                return current
        return None

    def provider_admission(self, host_tool_id):
        for port in self.ports:
            if any(item.host_tool_id == host_tool_id for item in port.declarations()):
                factory = getattr(port, "provider_admission", None)
                return factory(host_tool_id) if callable(factory) else None
        return None

    async def invoke(self, host_tool_id, invocation):
        for port in self.ports:
            if any(item.host_tool_id == host_tool_id for item in port.declarations()):
                return await port.invoke(host_tool_id, invocation)
        raise RuntimeError("composed host tool declaration is unavailable")

    def attach(self, application):
        for port in self.ports:
            attach = getattr(port, "attach", None)
            if callable(attach):
                attach(application)
        return self


def _same_declaration(declaration, current):
    """The host may rebind the same tool after a provider registry refresh.

    Only the version/generation part of ``ProviderBinding`` is allowed to move;
    every other declared field, including provider ID and capability, still has
    to match the original declaration.
    """
    if current is None:
        return False
    if current == declaration:
        return True
    original, replacement = declaration.descriptor.binding, current.descriptor.binding
    return ((original.provider, original.capability) == (replacement.provider, replacement.capability)
            and replace(current.descriptor, binding=original) == declaration.descriptor)


def map_tools(runtime, port: AstrBotToolPort) -> ExternalToolExecutor:
    if (not inspect.iscoroutinefunction(port.invoke) or not callable(port.declarations)
            or not callable(port.current_declaration) or inspect.iscoroutinefunction(port.declarations)
            or inspect.iscoroutinefunction(port.current_declaration)):
        raise ValueError("host port requires synchronous attestations and cooperative asynchronous invocation")
    declarations = port.declarations()
    if (not isinstance(declarations, tuple) or not declarations
            or any(not isinstance(item, AstrBotToolDeclaration) for item in declarations)
            or len({item.host_tool_id for item in declarations}) != len(declarations)):
        raise ValueError("host declarations require bounded unique trusted tool identities")
    providers = []
    for declaration in declarations:
        def current(declaration=declaration):
            replacement = port.current_declaration(declaration.host_tool_id)
            return replacement.descriptor.binding if _same_declaration(declaration, replacement) else None

        async def invoke(invocation, declaration=declaration):
            if invocation.before_dispatch is not None:
                invocation.before_dispatch()
            if not _same_declaration(declaration, port.current_declaration(declaration.host_tool_id)):
                # The core has already crossed its dispatch barrier; an exception remains unknown.
                raise RuntimeError("original host declaration is no longer loaded")
            return await port.invoke(declaration.host_tool_id, invocation)

        admission_factory = getattr(port, "provider_admission", None)
        admit = admission_factory(declaration.host_tool_id) if callable(admission_factory) else None
        providers.append(ExternalToolProvider(declaration.descriptor, invoke, current, admit))
    return ExternalToolExecutor(runtime, tuple(providers))
