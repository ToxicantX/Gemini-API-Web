import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploymentFileTests(unittest.TestCase):
    def test_dockerfile_only_exposes_admin_api_port(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        exposed_ports = [
            line.strip()
            for line in dockerfile.splitlines()
            if line.strip().startswith("EXPOSE ")
        ]

        # noVNC 授权浏览器必须走 /novnc 管理端代理，不能在镜像或 compose 中鼓励暴露 6080。
        self.assertEqual(exposed_ports, ["EXPOSE 7860"])
        self.assertNotIn("EXPOSE 6080", dockerfile)

    def test_compose_does_not_publish_novnc_port(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        # 服务器部署只发布统一入口，避免绕过管理员登录直接访问授权浏览器。
        self.assertIn('"7860:7860"', compose)
        self.assertNotIn("6080:6080", compose)
        self.assertNotIn('"6080:6080"', compose)

    def test_env_example_defaults_are_server_safe(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("ADMIN_PASSWORD=change-this-admin-password", env_example)
        self.assertIn("REQUIRE_API_KEY=true", env_example)
        self.assertIn("API_KEYS=sk-change-this-external-key", env_example)
        self.assertIn("CORS_ALLOW_ORIGINS=https://your-panel.example.com", env_example)


if __name__ == "__main__":
    unittest.main()
