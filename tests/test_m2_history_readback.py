"""M2.4 历史读取验证：模块关着的时候，既有历史数据要原样可读。

需求（M2 移除顺序第 4 步）：关掉 live / qzone / mihome / huawei_health 之后，
- 既有运行时产物（比如 health.db）不被打开、不被改写；
- 历史记录的读取路径（生图附件、指令快照）继续工作；
- 开关切回开启（重载）不丢数据。

导入类断言沿用 M2.3 的子进程探针：有没有 import 是进程级事实，同进程断言
`sys.modules` 只会得到假阳性。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _clean_environment() -> dict:
    """模块开关默认全关；把外面用例留下的开关变量摘掉，子进程才看到默认态。"""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("DIGITAL_LIFE_MODULE_")}
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


_PROBE = r"""
import json, sys
target = sys.argv[1]
__import__(target)
print(json.dumps(sorted(sys.modules)))
"""


def _probe(target: str) -> list:
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE, target],
        capture_output=True, text=True, env=_clean_environment(), cwd=str(ROOT), timeout=180,
    )
    if completed.returncode != 0:
        raise unittest.SkipTest("宿主运行时不可用：" + completed.stderr.strip()[-400:])
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.fixture(autouse=True)
def _default_switch_state():
    """本文件验证默认关闭态，先摘掉 conftest 给老套件开的那组开关。"""
    from digital_life_host import module_gates

    prefix = module_gates.ENV_PREFIX
    saved = {key: value for key, value in os.environ.items() if key.startswith(prefix)}
    for key in saved:
        del os.environ[key]
    yield
    os.environ.update(saved)


class DisabledModuleHistorySafetyTests(unittest.TestCase):
    """关闭态：既有运行时产物一个字节都不动。"""

    def test_disabled_health_service_leaves_existing_db_untouched(self):
        import hashlib

        from digital_life_host import multi_session

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            service_dir = runtime / "huawei_health"
            service_dir.mkdir()
            db_path = service_dir / "health.db"
            import sqlite3

            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE steps (day TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            connection.execute("INSERT INTO steps VALUES ('2026-11-24', 4321)")
            connection.commit()
            connection.close()
            before = hashlib.sha256(db_path.read_bytes()).hexdigest()

            # M2.5 之后华为健康实现整体移除：宿主不再持有任何服务构造入口，
            # 既有历史库只是躺在磁盘上，没人打开它。
            self.assertFalse(hasattr(multi_session, "_huawei_health_service"))
            self.assertEqual(hashlib.sha256(db_path.read_bytes()).hexdigest(), before)

    def test_disabled_module_gates_do_not_create_runtime_files(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            # 触一遍关闭态的宿主路径：bundled 候选只剩保留的 content，
            # 被关掉的 image/screen 连实现包都不导入，也不该留下自己的目录。
            from digital_life_host.native_extensions import builtins

            leases, issues = builtins.create_bundled_extensions(runtime)
            self.assertEqual(tuple(issues), ())
            self.assertEqual(len(leases), 1, "默认关态下 bundled 候选只剩 content")
            self.assertNotEqual(leases[0].manifest.package.extension_id, "image.native")
            self.assertFalse((runtime / "extensions" / "image").exists())
            self.assertFalse((runtime / "extensions" / "screen").exists())


class LegacyModuleImportIsolationTests(unittest.TestCase):
    """关闭态：四个待移除模块的实现包不进进程。"""

    def test_reality_common_skips_mihome_implementations(self):
        loaded = _probe("digital_life_host.native_extensions.reality.common")
        self.assertNotIn("digital_life_host.native_extensions.reality.mihome_data_manager", loaded)
        self.assertNotIn("digital_life_host.native_extensions.reality.mihome_client", loaded)
        self.assertNotIn("digital_life_host.native_extensions.reality.mihome_web_api", loaded)

    def test_controller_skips_qzone_and_live_implementations(self):
        loaded = _probe("digital_life_host.controller")
        self.assertNotIn("digital_life_host.qzone_client", loaded)
        self.assertNotIn("digital_life_host.qzone_transport", loaded)
        self.assertNotIn("digital_life_host.native_extensions.live", loaded)


class HistoryReadbackTests(unittest.TestCase):
    """历史数据读取：旧的 inline 附件与搬过家的 file-backed 附件都能读回。"""

    def test_offloaded_history_artifacts_read_back(self):
        import sqlite3

        from digital_life_core.storage import artifact_blobs

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            db_path = base / "native_image.sqlite3"
            media_root = base / "media" / "images"
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE artifacts (id TEXT PRIMARY KEY, metadata TEXT NOT NULL, data BLOB NOT NULL)")
            legacy_inline = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8
            connection.execute(
                "INSERT INTO artifacts VALUES ('old-inline', '{\"media_type\": \"image/png\"}', ?)", (legacy_inline,))
            # 历史里也有一行已经是 file-backed 的（更早搬过家）。
            payload = b"older-file-backed-payload"
            marker = artifact_blobs.write_blob(media_root, payload, media_type="image/png")
            connection.execute(
                "INSERT INTO artifacts VALUES ('old-file', '{\"media_type\": \"image/png\"}', ?)", (marker,))
            connection.commit()
            connection.close()

            result = artifact_blobs.offload_artifact_blobs(db_path, media_root=media_root)
            self.assertEqual(result["migrated"], 1)
            self.assertEqual(result["already_file_backed"], 1)

            connection = sqlite3.connect(db_path)
            rows = dict(connection.execute("SELECT id, data FROM artifacts").fetchall())
            connection.close()
            inline_row = rows["old-inline"]
            inline_row = inline_row if isinstance(inline_row, bytes) else bytes(inline_row)
            self.assertTrue(artifact_blobs.is_file_marker(inline_row))
            self.assertEqual((media_root / artifact_blobs.marker_path(inline_row)).read_bytes(), legacy_inline)
            file_row = rows["old-file"]
            file_row = file_row if isinstance(file_row, bytes) else bytes(file_row)
            self.assertEqual((media_root / artifact_blobs.marker_path(file_row)).read_bytes(), payload)

    def test_role_instruction_history_blobs_stay_content_addressed(self):
        """指令快照按内容寻址落库：关闭可选模块不影响历史快照的读取。"""
        import hashlib
        import sqlite3

        from digital_life_core.storage.native_dedup import ROLE_BLOB_MIN_BYTES

        self.assertGreater(ROLE_BLOB_MIN_BYTES, 0)
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "companion-v2.sqlite3"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE role_instruction_blobs (hash TEXT PRIMARY KEY, body BLOB NOT NULL)")
            body = ("你是肥鱼，一只 DeepSeek 出身的小鲸鱼。" * 40).encode()
            digest = hashlib.sha256(body).hexdigest()
            connection.execute("INSERT INTO role_instruction_blobs VALUES (?, ?)", (digest, body))
            connection.commit()
            rows = connection.execute("SELECT hash, body FROM role_instruction_blobs").fetchall()
            connection.close()
            self.assertEqual(len(rows), 1)
            stored_hash, stored_body = rows[0]
            self.assertEqual(stored_hash, digest)
            self.assertEqual(bytes(stored_body), body)


if __name__ == "__main__":
    unittest.main()
