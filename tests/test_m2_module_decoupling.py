"""M2.3 依赖解耦：模块关着的时候不该把它的实现导入进来。

这里用子进程做导入检查，因为「有没有 import」是进程级事实：同一个 pytest
进程里别的用例早就把包装过了，在进程内断言 `sys.modules` 只会得到假阳性。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from digital_life_core.storage import artifact_blobs  # noqa: E402


def _clean_environment() -> dict:
    """模块开关默认全关；把外面用例留下的开关变量摘掉，子进程才看到默认态。"""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("DIGITAL_LIFE_MODULE_")}
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


_PROBE = r"""
import json, sys
import digital_life_host.module_gates as gates
target = sys.argv[1]
action = sys.argv[2]
if action == "import":
    __import__(target)
else:
    import pathlib, tempfile
    import digital_life_host.native_extensions.builtins as builtins_module
    root = pathlib.Path(tempfile.mkdtemp())
    leases, issues = builtins_module.create_bundled_extensions(root)
    print(json.dumps({"issues": list(issues), "names": [lease.port and True for lease in leases]}), file=sys.stderr)
print(json.dumps(sorted(sys.modules)), file=sys.stdout)
"""


def _probe(target: str, action: str = "import") -> list:
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE, target, action],
        capture_output=True, text=True, env=_clean_environment(), cwd=str(ROOT), timeout=180,
    )
    if completed.returncode != 0:
        raise unittest.SkipTest("宿主运行时不可用：" + completed.stderr.strip()[-400:])
    return json.loads(completed.stdout.strip().splitlines()[-1])


class CommandImportTests(unittest.TestCase):
    """保留能力必须能在「除 retained 以外全关」的状态下导入。"""

    def test_controller_import_does_not_pull_qzone_implementation(self):
        loaded = _probe("digital_life_host.controller")
        self.assertNotIn("digital_life_host.qzone_client", loaded)
        self.assertNotIn("digital_life_host.qzone_transport", loaded)

    def test_multi_api_import_does_not_pull_qzone_or_health_apis(self):
        loaded = _probe("digital_life_host.multi_api")
        self.assertNotIn("digital_life_host.qzone_api", loaded)
        self.assertNotIn("digital_life_host.huawei_health.api", loaded)

    def test_web_api_import_does_not_pull_optional_apis(self):
        loaded = _probe("digital_life_host.web_api")
        self.assertNotIn("digital_life_host.qzone_api", loaded)
        self.assertNotIn("digital_life_host.huawei_health.api", loaded)

    def test_multi_session_import_does_not_pull_health_service(self):
        loaded = _probe("digital_life_host.multi_session")
        self.assertNotIn("digital_life_host.huawei_health.service", loaded)

    def test_bundled_extensions_skip_image_and_screen_implementations(self):
        loaded = _probe("digital_life_host.native_extensions.builtins", action="call")
        self.assertNotIn("digital_life_host.native_extensions.image_native", loaded)
        self.assertNotIn("digital_life_host.native_extensions.screen_provider", loaded)

    def test_native_extension_package_skips_image_tool_surface(self):
        loaded = _probe("digital_life_host.native_extensions")
        self.assertNotIn("digital_life_host.native_extensions.image_native", loaded)
        self.assertNotIn("digital_life_host.native_extensions.images", loaded)
        self.assertNotIn("digital_life_host.native_extensions.image_settings", loaded)


class BlobOffloadTests(unittest.TestCase):
    """生图摘除之后，附件搬家能力留在 core，不再反向依赖宿主扩展。"""

    def test_marker_roundtrip(self):
        digest = "ab" * 32
        marker = artifact_blobs.marker_for(f"ab/{digest}.png")
        self.assertTrue(artifact_blobs.is_file_marker(marker))
        self.assertEqual(artifact_blobs.marker_path(marker), f"ab/{digest}.png")
        self.assertFalse(artifact_blobs.is_file_marker(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(artifact_blobs.marker_path(b"\x89PNG"), "")
        # 只认内容寻址的形状：随便一段相对路径不会被当成标记。
        self.assertEqual(artifact_blobs.marker_path(artifact_blobs.marker_for("../../etc/passwd")), "")

    def test_write_blob_creates_file_and_is_content_addressed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = artifact_blobs.write_blob(root, b"payload", media_type="image/png")
            relative = artifact_blobs.marker_path(marker)
            self.assertTrue(relative)
            self.assertEqual((root / relative).read_bytes(), b"payload")
            self.assertEqual(artifact_blobs.write_blob(root, b"payload", media_type="image/png"), marker)

    def test_offload_moves_inline_rows_to_files(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "native_image.sqlite3"
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE artifacts (id TEXT PRIMARY KEY, metadata TEXT NOT NULL, data BLOB NOT NULL)")
            inline = bytes(range(256)) * 4
            connection.execute("INSERT INTO artifacts VALUES ('a1', '{\"media_type\": \"image/png\"}', ?)", (inline,))
            connection.commit()
            connection.close()

            self.assertEqual(artifact_blobs.count_inline_blobs(db_path), 1)
            result = artifact_blobs.offload_artifact_blobs(db_path, media_root=Path(directory) / "media")
            self.assertEqual(result["migrated"], 1)
            self.assertEqual(result["already_file_backed"], 0)
            # 记的是腾出来的那部分：原行长减去标记行长度。
            self.assertGreater(result["bytes_offloaded"], len(inline) - 128)
            self.assertLess(result["bytes_offloaded"], len(inline))
            self.assertEqual(artifact_blobs.count_inline_blobs(db_path), 0)

            connection = sqlite3.connect(db_path)
            stored = connection.execute("SELECT data FROM artifacts WHERE id='a1'").fetchone()[0]
            connection.close()
            stored = stored if isinstance(stored, bytes) else bytes(stored)
            self.assertTrue(artifact_blobs.is_file_marker(stored))
            relative = artifact_blobs.marker_path(stored)
            self.assertEqual((Path(directory) / "media" / relative).read_bytes(), inline)

            again = artifact_blobs.offload_artifact_blobs(db_path, media_root=Path(directory) / "media")
            self.assertEqual(again["migrated"], 0)
            self.assertEqual(again["already_file_backed"], 1)

    def test_offload_tolerates_missing_table(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "empty.sqlite3"
            sqlite3.connect(db_path).close()
            self.assertEqual(artifact_blobs.count_inline_blobs(db_path), 0)
            self.assertEqual(artifact_blobs.offload_artifact_blobs(db_path, media_root=Path(directory))["migrated"], 0)


if __name__ == "__main__":
    unittest.main()
