"""Group text and original images retain the speaker and collective audience."""

from digital_life_core.foundation.refs import ScopeKind
from digital_life_core.identity_policy.group_audience import group_audience
from .native import account_number
from digital_life_core.adapters.astrbot.onebot import OneBotTextSender


class OneBotGroupSender(OneBotTextSender):
    supports_images = True

    def route(self, context):
        scope = context.scope
        sender = account_number(context.actor.principal.native_account_id)
        group = account_number(scope.native_scope_id)
        if (scope.kind is not ScopeKind.GROUP or sender == scope.account_id
                or scope.session_id not in (group, sender + "_" + group)
                or context.audience != group_audience(scope, context.actor.subject)):
            raise ValueError("group delivery requires the original group, speaker session and collective audience")
        return "send_group_msg", {"group_id": int(group)}
