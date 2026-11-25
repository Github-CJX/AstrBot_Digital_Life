"""Internal services enhance real AstrBot requests without owning the reply runner."""

import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

from tests import test_astrbot_staging_host as _host_import_root
from astrbot.api.message_components import Image, Plain, Record
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from digital_life_host.native_extensions.host_events import (
    observe_native_host_response, observe_native_host_result, prepare_native_host_request,
)
from digital_life_host.native_extensions.game.model_methods import GameModelMethods
from digital_life_host.native_extensions.game.rooms_methods import GameRoomsMethods
from digital_life_core.games.models import GameRoom
from digital_life_core.games.turtle_soup import TurtleSoupGame


class HostEvent:
    def __init__(self, *, private=True, sender="200", session="bot:private:200", text="摸摸", result=None):
        self.private, self.sender, self.unified_msg_origin = private, sender, session
        self.message_str, self.result, self.extra = text, result, {}

    def is_private_chat(self):
        return self.private

    def get_sender_id(self):
        return self.sender

    def get_result(self):
        return self.result

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)


def controller(**services):
    registry = NS(active_service=lambda name: services.get(name),
                  service=Mock(side_effect=AssertionError("paused service must not handle host events")))
    return NS(native_extensions=registry), services


def context_text(request):
    return "\n".join(part.text for part in request.extra_user_content_parts)


class LocalGame(GameModelMethods, GameRoomsMethods):
    private_qq_game_context_enabled = True

    @staticmethod
    def _game_label(game_type):
        return "海龟汤"

    def __init__(self, rooms):
        self.manager = NS(for_session=lambda session: [room for room in rooms if room.session_id == session])
        self._recent_private_game_results = {}


def game_room(**changes):
    fields = dict(room_id="room-a", access_token="private-room-token", source="private", session_id="bot:private:200",
                  platform="aiocqhttp", group_id="", creator_qq="200", creator_name="玩家", admin_room=False,
                  game_type="turtle_soup", difficulty="normal", status="active")
    fields.update(changes)
    room = GameRoom(**fields)
    room.game = TurtleSoupGame("normal", 3, "normal", puzzle=NS(solution="隐藏汤底不得进入核心对话"), question_count=3)
    room.messages = [{"content": "网页私聊不进入核心上下文"}]
    return room


class NativeHostEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_paused_services_and_plain_service_lookup_cannot_receive_events(self):
        runtime = LocalGame([game_room()])
        host, active = controller(game=runtime)
        active.clear()
        req = ProviderRequest(system_prompt="原提示")
        event = HostEvent(result=MessageEventResult().message("正常回复"))
        prepare_native_host_request(host, event, req)
        observe_native_host_response(host, event, LLMResponse(role="assistant", completion_text="正常回复"))
        observe_native_host_result(host, event)
        self.assertEqual(req.system_prompt, "原提示")
        host.native_extensions.service.assert_not_called()
        self.assertFalse(context_text(req))


    async def test_optional_domain_failure_preserves_other_domain_and_core_request(self):
        game = NS(_host_event_owner=NS(request=Mock(side_effect=RuntimeError("fixture failure"))))
        host, _ = controller(game=game, reality=NS(_host_event_owner=NS(request=Mock())))
        req = ProviderRequest(system_prompt="AstrBot 基础人格")
        with self.assertLogs("digital_life_host.native_extensions.host_events", level="WARNING"):
            prepare_native_host_request(host, HostEvent(), req)
        self.assertIn("AstrBot 基础人格", req.system_prompt)


    async def test_game_reads_matching_single_player_state_without_hidden_answers(self):
        own = game_room()
        other = game_room(room_id="room-other", creator_qq="300")
        wrong_session = game_room(room_id="room-elsewhere", session_id="bot:private:other")
        runtime = LocalGame([own, other, wrong_session])
        host, _ = controller(game=runtime)
        req = ProviderRequest(system_prompt="AstrBot 基础人格")
        prepare_native_host_request(host, HostEvent(), req)
        self.assertEqual(req.system_prompt, "AstrBot 基础人格")
        self.assertIn("提问 3 次", context_text(req))
        self.assertNotIn(own.game.puzzle.solution, context_text(req))
        self.assertNotIn(own.messages[0]["content"], context_text(req))
        self.assertEqual(context_text(req).count("当前私聊房间"), 1)
        prepared = context_text(req)
        prepare_native_host_request(host, HostEvent(), req)
        self.assertEqual(context_text(req), prepared)
        own.game.question_count = 4
        prepare_native_host_request(host, HostEvent(), req)
        self.assertIn("提问 4 次", context_text(req))
        self.assertNotIn("提问 3 次", context_text(req))
        self.assertEqual(req.system_prompt, "AstrBot 基础人格")
        for event in (HostEvent(sender="999"), HostEvent(private=False)):
            req = ProviderRequest(system_prompt="原提示")
            prepare_native_host_request(host, event, req)
            self.assertEqual(req.system_prompt, "原提示")
            self.assertFalse(req.extra_user_content_parts)

    async def test_game_recent_result_expires_and_does_not_cross_users(self):
        runtime = LocalGame([])
        host, _ = controller(game=runtime)
        runtime._recent_private_game_results["bot:private:200"] = NS(user_qq="200", expires_at=1, summary="过期结果")
        req = ProviderRequest(system_prompt="原提示")
        prepare_native_host_request(host, HostEvent(), req)
        self.assertEqual(req.system_prompt, "原提示")
        self.assertFalse(req.extra_user_content_parts)
        self.assertFalse(runtime._recent_private_game_results)
        runtime._recent_private_game_results["bot:private:200"] = NS(user_qq="300", expires_at=0, summary="其他玩家结果")
        prepare_native_host_request(host, HostEvent(), req)
        self.assertEqual(req.system_prompt, "原提示")
        self.assertFalse(req.extra_user_content_parts)

    async def test_reality_guidance_requires_a_current_private_request_and_a_real_v2_tool(self):
        runtime = NS(_safe_event_is_private=lambda event: event.is_private_chat(),
                     _official_reality_touch_metadata=lambda event: {},
                     _official_reality_touch_matches=lambda reminder, metadata: False,
                     data={"users": {}}, _user=lambda user_id, **kwargs: None,
                     v2_host_api=lambda: NS(_reality_context=NS(target=lambda user_id, **kwargs: None)))
        host, _ = controller(reality=runtime)
        camera = NS(_action="reality.camera.observe", name="digital_life_core_reality_camera_observe")
        for private, text, tools in ((False, "看看摄像头", [camera]), (True, "今天好累", [camera]),
                                     (True, "看看摄像头", [])):
            req = ProviderRequest(system_prompt="原提示", func_tool=NS(tools=tools))
            prepare_native_host_request(host, HostEvent(private=private, text=text), req)
            self.assertEqual(req.system_prompt, "原提示")
        req = ProviderRequest(system_prompt="原提示", func_tool=NS(tools=[camera]))
        prepare_native_host_request(host, HostEvent(text="看看摄像头，我在吃什么"), req)
        self.assertIn(camera.name, context_text(req))
        self.assertIn("final_response_instruction", context_text(req))
        self.assertNotIn("pc_reality_touch_camera_snapshot", context_text(req))
        self.assertNotIn("一句", context_text(req))
        self.assertEqual(req.system_prompt, "原提示")
        original = context_text(req)
        prepare_native_host_request(host, HostEvent(text="看看摄像头，我在吃什么"), req)
        self.assertEqual(context_text(req), original)
        prepare_native_host_request(host, HostEvent(text="今天好累"), req)
        self.assertFalse(req.extra_user_content_parts)
        self.assertEqual(req.system_prompt, "原提示")


if __name__ == "__main__":
    unittest.main()
