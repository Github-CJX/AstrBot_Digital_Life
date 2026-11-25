"""Native game facts reach the real SDK payload without churning its prefix."""

from copy import deepcopy
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from tests import astrbot_data_root as _host_import_root
from astrbot.core.agent.message import Message, TextPart, dump_messages_with_checkpoints
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from digital_life_host.native_extensions.host_events import prepare_native_host_request
from tests.test_native_host_events import HostEvent, LocalGame, context_text, controller, game_room


class CapturingProvider:
    def __init__(self, name, *, failed=False):
        self.provider_config = {"id": name}
        self.calls, self.failed = [], failed

    async def text_chat(self, **kwargs):
        self.calls.append([message.model_dump() for message in kwargs["contexts"]])
        return LLMResponse(role="err" if self.failed else "assistant", completion_text="测试回复")


class NativeRequestCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_game_changes_preserve_prefix_and_only_current_facts_reach_provider(self):
        room = game_room()
        host, active = controller(game=LocalGame([room]))
        history = [{"role": "user", "content": "之前的话"}, {"role": "assistant", "content": "之前的回复"}]
        other = TextPart(text="另一插件的本轮材料")
        req = ProviderRequest(prompt="先前是怎么回事", system_prompt="AstrBot 当前角色",
                              contexts=deepcopy(history), extra_user_content_parts=[other])
        event = HostEvent()
        prepare_native_host_request(host, event, req)
        first_system = req.system_prompt
        first_material = context_text(req)
        prepare_native_host_request(host, event, req)
        self.assertEqual(req.system_prompt, first_system)
        self.assertEqual(context_text(req), first_material)
        self.assertIs(req.extra_user_content_parts[0], other)
        self.assertEqual(req.contexts, history)

        primary, fallback = CapturingProvider("primary", failed=True), CapturingProvider("fallback")
        runner, run = ToolLoopAgentRunner(), NS(context=NS(event=event), messages=[])
        await runner.reset(provider=primary, request=req, run_context=run,
                           tool_executor=None, agent_hooks=None, fallback_providers=[fallback])
        await anext(runner._iter_llm_responses_with_fallback())
        for provider in (primary, fallback):
            sent = provider.calls[0]
            self.assertEqual(sent[0], {"role": "system", "content": first_system})
            self.assertEqual(sent[1:-1], history)
            self.assertIn("提问 3 次", str(sent[-1]))
            self.assertIn("另一插件的本轮材料", str(sent[-1]))
            self.assertNotIn("_no_save", str(sent))

        saved = dump_messages_with_checkpoints(run.messages[1:])
        self.assertNotIn("提问 3 次", str(saved))
        self.assertNotIn("digital_life_core_native_", str(saved))
        self.assertIn(req.prompt, str(saved[-1]))
        self.assertIn("另一插件的本轮材料", str(saved[-1]))

        room.game.question_count = 4
        second = ProviderRequest(prompt="现在呢", system_prompt="AstrBot 当前角色", contexts=deepcopy(saved))
        prepare_native_host_request(host, HostEvent(), second)
        self.assertEqual(second.system_prompt, first_system)
        self.assertEqual(second.contexts, saved)
        second_run, second_runner = NS(context=NS(event=event), messages=[]), ToolLoopAgentRunner()
        await second_runner.reset(provider=fallback, request=second, run_context=second_run,
                                  tool_executor=None, agent_hooks=None)
        await anext(second_runner._iter_llm_responses())
        sent = fallback.calls[-1]
        self.assertEqual(sent[:-1], [{"role": "system", "content": first_system}, *saved])
        self.assertIn("提问 4 次", str(sent[-1]))
        self.assertNotIn("提问 3 次", str(sent))

        second_run.messages.extend([Message(role="assistant", content="正在查询"),
                                    Message(role="user", content="已授权工具的真实结果")])
        await anext(second_runner._iter_llm_responses())
        self.assertEqual(fallback.calls[-1][:-2], sent)

        prepare_native_host_request(host, HostEvent(), second)
        self.assertEqual(second.system_prompt, first_system)
        active.clear()
        prepare_native_host_request(host, HostEvent(), second)
        self.assertEqual(second.system_prompt, "AstrBot 当前角色")
        self.assertFalse(second.extra_user_content_parts)




if __name__ == "__main__":
    unittest.main()
