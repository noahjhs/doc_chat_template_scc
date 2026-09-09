from pydantic import BaseModel, Field


class SignupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthResponse(BaseModel):
    username: str
    token: str


class VerifyResponse(BaseModel):
    valid: bool
    username: str | None = None


class RevokeResponse(BaseModel):
    revoked: bool


class HostPairRequest(BaseModel):
    routing_key: str
    hostname: str | None = None
    label: str | None = None


class HostPairResponse(BaseModel):
    host_id: int
    device_token: str
    command_key: str
    label: str


class HostVerifyResponse(BaseModel):
    valid: bool


class HostPresenceReport(BaseModel):
    local_agent_url: str
    # A set of addressable directories, not one fixed workspace -- may be
    # empty (nothing added on that host yet).
    workspace: list[str] = []


class HostInfo(BaseModel):
    host_id: int
    label: str
    hostname: str | None = None
    connected: bool
    local_agent_url: str | None = None
    workspace: list[str] = []
    command_key: str | None = None
    environment_ids: list[int] = []


class HostListResponse(BaseModel):
    hosts: list[HostInfo]


class HostRenameRequest(BaseModel):
    label: str = Field(min_length=1, max_length=64)


class EnvironmentCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class EnvironmentRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class EnvironmentInfo(BaseModel):
    id: int
    name: str
    host_ids: list[int] = []


class EnvironmentListResponse(BaseModel):
    environments: list[EnvironmentInfo]


class SignOutAllResponse(BaseModel):
    signed_out_hosts: int


class StorageFileInfo(BaseModel):
    filename: str
    size: int
    uploaded_at: str


class StorageListResponse(BaseModel):
    files: list[StorageFileInfo]
    total_bytes: int
    cap_bytes: int


class StorageUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content: str  # base64


class StorageDownloadResponse(BaseModel):
    filename: str
    content: str  # base64


class StorageDeleteResponse(BaseModel):
    deleted: bool
