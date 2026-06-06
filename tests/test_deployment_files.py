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

    def test_dockerfile_has_healthcheck_for_api_process(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        # 镜像健康检查必须探测同容器内的 /health，避免服务器只看到进程存活但 API 已不可用。
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn("127.0.0.1", dockerfile)
        self.assertIn("/health", dockerfile)
        self.assertIn("os.getenv('PORT','7860')", dockerfile)
        self.assertIn("--retries=3", dockerfile)

    def test_compose_does_not_publish_novnc_port(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        # 服务器部署只发布统一入口，避免绕过管理员登录直接访问授权浏览器。
        self.assertIn('"7860:7860"', compose)
        self.assertNotIn("6080:6080", compose)
        self.assertNotIn('"6080:6080"', compose)

    def test_dockerignore_excludes_sensitive_runtime_files(self):
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        entries = {
            line.strip()
            for line in dockerignore.splitlines()
            if line.strip() and not line.strip().startswith("#")
        }

        # 构建镜像时也不能把本地真实 Cookie、数据库或 .env 发送进 Docker build context。
        for entry in (
            "data",
            ".env",
            ".env.*",
            "!.env.example",
            "accounts.json",
            "cookies.json",
            "*.db",
            "*.sqlite",
            "*.sqlite3",
            "media-cache",
        ):
            self.assertIn(entry, entries)

    def test_gitignore_excludes_sensitive_runtime_files(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        entries = {
            line.strip()
            for line in gitignore.splitlines()
            if line.strip() and not line.strip().startswith("#")
        }

        # Git 提交层面也要拦住服务器运行时产生的 Cookie、数据库、媒体缓存和派生 .env。
        for entry in (
            "data/*",
            "!data/",
            "!data/accounts.example.json",
            ".env",
            ".env.*",
            "!.env.example",
            "accounts.json",
            "cookies.json",
            "*.cookies",
            "*.db",
            "*.sqlite",
            "*.sqlite3",
            "media-cache/",
        ):
            self.assertIn(entry, entries)

    def test_compose_passes_server_deployment_env(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        # compose 必须把 .env.example 中的服务器部署关键项传入容器，避免文档配置后不生效。
        self.assertIn("ADMIN_USERNAME: ${ADMIN_USERNAME:-admin}", compose)
        self.assertIn(
            "ADMIN_PASSWORD: ${ADMIN_PASSWORD:-change-this-admin-password}",
            compose,
        )
        self.assertIn(
            "ADMIN_SESSION_SECRET: ${ADMIN_SESSION_SECRET:-change-this-to-a-long-random-string}",
            compose,
        )
        self.assertIn("REQUIRE_API_KEY: ${REQUIRE_API_KEY:-true}", compose)
        self.assertIn("API_KEYS: ${API_KEYS:-}", compose)
        self.assertIn(
            "CORS_ALLOW_ORIGINS: ${CORS_ALLOW_ORIGINS:-https://your-panel.example.com}",
            compose,
        )
        self.assertIn("GIT_COMMIT: ${GIT_COMMIT:-}", compose)
        self.assertIn("OBJECT_STORAGE_ENABLED: ${OBJECT_STORAGE_ENABLED:-false}", compose)
        self.assertIn("OBJECT_STORAGE_ENDPOINT: ${OBJECT_STORAGE_ENDPOINT:-}", compose)
        self.assertIn("OBJECT_STORAGE_BUCKET: ${OBJECT_STORAGE_BUCKET:-}", compose)
        self.assertIn(
            "OBJECT_STORAGE_PUBLIC_URL: ${OBJECT_STORAGE_PUBLIC_URL:-}",
            compose,
        )

    def test_env_example_defaults_are_server_safe(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("ADMIN_USERNAME=admin", env_example)
        self.assertIn("ADMIN_PASSWORD=change-this-admin-password", env_example)
        self.assertIn("REQUIRE_API_KEY=true", env_example)
        self.assertIn("API_KEYS=sk-change-this-external-key", env_example)
        self.assertIn("CORS_ALLOW_ORIGINS=https://your-panel.example.com", env_example)
        self.assertIn("GIT_COMMIT=", env_example)
        self.assertIn("OBJECT_STORAGE_ENABLED=false", env_example)
        self.assertIn("OBJECT_STORAGE_PREFIX=gemini-web", env_example)
        self.assertIn("OBJECT_STORAGE_FORCE_PATH_STYLE=true", env_example)

    def test_readme_documents_file_head_probes(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        # 外部 SDK 和 API 网关常用 HEAD 探测文件接口，README 需要和服务端行为保持一致。
        self.assertIn("均支持 `HEAD`", readme)
        self.assertIn("`/v1/gemini/files`", readme)
        self.assertIn("只返回状态和头部", readme)

    def test_readme_documents_server_security_requirements(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        # README 尾部安全说明需要覆盖公网部署的三个关键点：管理员密钥、外部 API Key 和 CORS。
        self.assertIn("ADMIN_SESSION_SECRET", readme)
        self.assertIn("仍使用占位值", readme)
        self.assertIn("REQUIRE_API_KEY=true", readme)
        self.assertIn("CORS_ALLOW_ORIGINS", readme)
        self.assertIn("OBJECT_STORAGE_ENABLED", readme)

    def test_readme_documents_deployment_smoke_test(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        script = ROOT / "scripts" / "smoke_deploy.py"

        self.assertTrue(script.is_file())
        self.assertIn("python scripts/smoke_deploy.py", readme)
        self.assertIn("--api-key", readme)


if __name__ == "__main__":
    unittest.main()
