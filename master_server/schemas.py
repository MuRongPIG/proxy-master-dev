from typing import Optional

from pydantic import BaseModel, Field


class RegisterNodeRequest(BaseModel):
    node_name: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)


class AllocateNodeNameRequest(BaseModel):
    country_code: str = Field(min_length=2, max_length=2)


class AllocateNodeNameResponse(BaseModel):
    node_name: str


class HeartbeatRequest(BaseModel):
    node_name: str = Field(min_length=1)


class WorkerHeartbeatRequest(BaseModel):
    node_name: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)


class NodeInfoView(BaseModel):
    node_name: str
    worker_id: str
    last_heartbeat: str
    registered_at: str


class ImportProxiesRequest(BaseModel):
    proxies: list[str] = Field(default_factory=list)
    max_retries: int = Field(default=2, ge=0, le=10)


class ImportFromUrlRequest(BaseModel):
    url: str
    max_retries: int = Field(default=2, ge=0, le=10)


class ImportFromUrlsRequest(BaseModel):
    urls: list[str] = Field(min_items=1)
    max_retries: int = Field(default=2, ge=0, le=10)


class ImportProxiesResponse(BaseModel):
    imported: int
    ignored: int


class TaskPayload(BaseModel):
    task_id: str
    proxy_id: int
    proxy_url: str
    protocol: str
    attempt: int
    max_retries: int


class PullTaskRequest(BaseModel):
    node_name: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)


class PullTaskResponse(BaseModel):
    found: bool
    task: Optional[TaskPayload] = None


class PushResultRequest(BaseModel):
    node_name: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    success: bool
    latency_ms: Optional[int] = None
    response_status: Optional[int] = None
    error: Optional[str] = None
    country_code: Optional[str] = None


class NodeResultView(BaseModel):
    node_name: str
    worker_id: str
    success: bool
    latency_ms: Optional[int]
    response_status: Optional[int]
    error: Optional[str]
    country_code: Optional[str]
    checked_at: str


class ProxyView(BaseModel):
    id: int
    proxy_url: str
    protocol: str
    status: str
    pool_tier: str
    country_code: Optional[str]
    latency_ms: Optional[int]
    error: Optional[str]
    last_checked_at: Optional[str]
    updated_at: str
    node_results: list[NodeResultView] = Field(default_factory=list)


class TaskView(BaseModel):
    id: int
    task_uuid: str
    proxy_id: int
    status: str
    assigned_to: Optional[str]
    assigned_worker_id: Optional[str]
    attempt: int
    max_retries: int
    created_at: str
    finished_at: Optional[str]


class StatsResponse(BaseModel):
    proxy_total: int
    proxy_alive: int
    proxy_dead: int
    task_pending: int
    task_assigned: int
    task_done: int
    task_failed: int


class PoolTierTrendPoint(BaseModel):
    date: str
    excellent: int
    good: int
    bad: int
    unknown: int
    total_checked: int


class ProtocolDistributionItem(BaseModel):
    protocol: str
    count: int


class AliveProtocolDistributionResponse(BaseModel):
    total_alive: int
    distribution: list[ProtocolDistributionItem]
