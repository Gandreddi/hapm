"""Caller identity carried by every LLM, memory and tool call."""

from pydantic import BaseModel, ConfigDict, Field

HEADER_AGENT = "X-HAPM-Agent-Id"
HEADER_RUN = "X-HAPM-Run-Id"
HEADER_TASK = "X-HAPM-Task-Id"
HEADER_TEAM = "X-HAPM-Team-Id"


class CallerContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_\-]+$")
    team_id: str = Field(min_length=1, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)
    task_id: str | None = Field(default=None, max_length=128)
    mode: str = "live"

    @property
    def actor(self) -> str:
        return f"agent:{self.agent_id}"

    def headers(self) -> dict[str, str]:
        headers = {HEADER_AGENT: self.agent_id, HEADER_TEAM: self.team_id}
        if self.run_id:
            headers[HEADER_RUN] = self.run_id
        if self.task_id:
            headers[HEADER_TASK] = self.task_id
        return headers
