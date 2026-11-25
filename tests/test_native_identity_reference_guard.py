"""画角色本人必须带 identity 参考：这条链路有三处静默失效，全部锁在这里。

背景（测试服务器实测）：私聊工具档案不是 full 时，生图授权没有在注入提示词之前补齐，
参考素材读取被拒成 ``scope_not_granted``，模型于是从没拿到 identity 素材 id，改用纯文生图
画出一条橘色胖鱼；随后强制拦截抛出的 TaskError 又被当成「结果未知」，模型只能回
「这张卡住了」。三处修复分别对应下面的四组用例。
"""

import ast
import pathlib
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from tests import astrbot_data_root  # noqa: F401  AstrBot 数据目录隔离
from digital_life_core.conversation import tool_failures
from digital_life_core.execution.models import TaskError
from digital_life_host import native_extensions as native


IDENTITY_CODE = "identity_reference_required"


def _registry():
    """只借方法，不装配扩展注册表。"""
    return object.__new__(native.NativeExtensionRegistry)


class IdentityReferenceGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_reference_failure_is_a_preflight_lease_code(self):
        # 这个码必须留在预检集合里，否则 invoke 会把异常外抛成「结果未知」。
        self.assertIn(IDENTITY_CODE, native._IMAGE_PREFLIGHT_LEASE_CODES)

    def test_guard_rejects_generate_without_identity_and_hands_over_the_id(self):
        registry = _registry()
        registry.application = NS(
            references=NS(list_assets=lambda context, **kwargs: [NS(asset_id="ref_identity")]))
        invocation = NS(arguments_json='{"prompt": "晨光里的自拍"}', context=object())

        with self.assertRaises(TaskError) as caught:
            registry._require_identity_reference(invocation)

        self.assertEqual(caught.exception.code, IDENTITY_CODE)
        self.assertIn("ref_identity", str(caught.exception))
        self.assertIn("image.edit", str(caught.exception))

    def test_guard_allows_arguments_that_already_carry_reference_ids(self):
        registry = _registry()
        registry.application = NS(references=NS(list_assets=lambda context, **kwargs: []))
        invocation = NS(arguments_json='{"prompt": "自拍", "reference_ids": ["ref_identity"]}',
                        context=object())

        self.assertIsNone(registry._require_identity_reference(invocation))

    def test_guard_stays_silent_when_the_role_has_no_identity_asset(self):
        registry = _registry()
        registry.application = NS(references=NS(list_assets=lambda context, **kwargs: []))
        invocation = NS(arguments_json='{"prompt": "一只橘色的鱼"}', context=object())

        self.assertIsNone(registry._require_identity_reference(invocation))

    async def test_invoke_returns_a_preflight_failure_instead_of_unknown_effect(self):
        registry = _registry()
        registry.application = NS(
            references=NS(list_assets=lambda context, **kwargs: [NS(asset_id="ref_identity")]))
        descriptor = NS(tool=NS(action="image.generate"))
        entry = (NS(descriptor=descriptor), NS(port=object()))
        registry._entries = {"companion.image.generate": entry}
        invocation = NS(descriptor=descriptor, arguments_json='{"prompt": "自拍"}',
                        context=object(), invocation_id="inv-identity")
        sentinel = ("preflight", IDENTITY_CODE)

        with patch.object(native.NativeExtensionRegistry, "_role", AsyncMock()), \
                patch.object(native.NativeExtensionRegistry, "_guard", lambda *args, **kwargs: None), \
                patch.object(native.NativeExtensionRegistry, "_preflight_failure",
                             lambda self, invocation, code: sentinel):
            result = await registry.invoke("companion.image.generate", invocation)

        # 预检失败必须是 not_committed 的可重试输入错误，模型才能立刻改参数。
        self.assertEqual(result, sentinel)


class IdentityFailureGuidanceTests(unittest.TestCase):
    def test_failed_state_is_retryable_input_error(self):
        self.assertEqual(tool_failures.retry_disposition_for(IDENTITY_CODE, "failed"), "input_invalid")

    def test_unknown_state_still_reports_unconfirmed_effect(self):
        self.assertEqual(tool_failures.retry_disposition_for(IDENTITY_CODE, "unknown"),
                         "external_effect_unknown")

    def test_guidance_tells_the_model_to_switch_tool(self):
        guidance = tool_failures.failure_guidance(IDENTITY_CODE, "failed", action="image.generate")

        self.assertIn("image.edit", guidance["next_step"])
        self.assertIn("identity", guidance["next_step"])
        self.assertIn("未发起远端生图", guidance["reason"])


class PromptFallbackTests(unittest.TestCase):
    def test_reference_failure_still_writes_the_identity_anchor(self):
        from digital_life_host import core_reply_image_prompt as prompt_module

        def blocked(context, *, local_date):
            raise TaskError("scope_not_granted", "capability is not granted in this exact scope")

        owner = NS(
            prompt_material=blocked,
            assets=NS(list=lambda key, role=None: [NS(asset_id="ref_identity")]),
            owner=lambda context: "role-owner",
        )
        session = NS(state={}, context=object(), prepared={},
                     application=NS(messages=NS(reference_materials=owner), native_tool_port=None))
        captured = {}

        with patch.object(prompt_module, "prompt_instruction", lambda values: "图片工具说明"), \
                patch.object(prompt_module, "set_system_instruction",
                             lambda request, name, text=None: captured.__setitem__(name, text)), \
                patch.object(prompt_module, "_attachment_guidance", lambda session: ""), \
                patch.object(prompt_module, "_reference_date", lambda session: "2026-11-25"):
            prompt_module.inject_core_image_prompt(
                session, object(), (NS(spec=NS(action="image.edit")),), {"trigger_mode": "rules"})

        self.assertIn("ref_identity", session.state["_turn_notes"]["image"])
        # 系统提示词必须逐轮稳定：身份 id 只随本轮用户消息下发。
        self.assertNotIn("ref_identity", captured["image_output"])
        material = session.state["_image_reference_material"]
        self.assertEqual(material["references"][0]["id"], "ref_identity")
        self.assertEqual(material["references"][0]["roles"], ["identity"])


class ImageProvisionOrderTests(unittest.TestCase):
    def test_private_chat_provisions_image_access_before_the_prompt_is_built(self):
        """授权必须早于提示词注入，否则参考素材只会被拒一次。"""
        source = pathlib.Path(native.__file__).parent.parent / "core_reply_tools.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))

        def calls(node):
            found = {}
            for item in ast.walk(node):
                if isinstance(item, ast.Call):
                    name = getattr(item.func, "id", None) or getattr(item.func, "attr", None)
                    found.setdefault(name, item.lineno)
            return found

        holders = [node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef)
                   and {"ensure_image_access", "inject_core_image_prompt"} <= set(calls(node))]
        self.assertTrue(holders, "注入函数里必须同时出现授权补齐与提示注入")
        for holder in holders:
            positions = calls(holder)
            self.assertLess(positions["ensure_image_access"], positions["inject_core_image_prompt"])


if __name__ == "__main__":
    unittest.main()
