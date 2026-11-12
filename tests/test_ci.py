import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from backend.database import engine, init_db
from backend.main import app


async def run_smoke_tests() -> None:
    try:
        cases_count = await init_db()
        assert cases_count == 8, f"Expected 8 seeded cases, got {cases_count}"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            r_root = await client.get("/")
            assert r_root.status_code == 200

            r_docs = await client.get("/docs")
            assert r_docs.status_code == 200

            r_oa = await client.get("/openapi.json")
            assert r_oa.status_code == 200
            assert "/api/cases" in r_oa.json()["paths"]

            r_health = await client.get("/api/health")
            assert r_health.status_code == 200
            assert r_health.json()["status"] == "ok"

            r_cases = await client.get("/api/cases")
            assert r_cases.status_code == 200
            assert len(r_cases.json()["cases"]) == 8

            r_run = await client.post(
                "/api/cases/1/workflow/run",
                json={"simulate_critic_rejection": True, "tone": "institutional"},
            )
            assert r_run.status_code == 200
            assert r_run.json()["critic_passed"] is True
            assert r_run.json()["workflow_iterations"] == 2

            r_reset = await client.post("/api/cases/1/reset")
            assert r_reset.status_code == 200
            assert r_reset.json()["status"] == "Missing_Docs"

            r_metrics = await client.get("/api/dashboard/metrics")
            assert r_metrics.status_code == 200
            assert "days_per_chase_round" in r_metrics.json()

        print("All CI smoke tests passed!")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_smoke_tests())
